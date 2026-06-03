from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import or_
from pydantic import BaseModel
import uvicorn
import asyncio
import os
import shutil
import threading
import concurrent.futures
from collections import deque
import datetime
import sys

# Política de loop de evento não é mais necessária aqui
import database
from scraper import scrape_google_maps, scrape_google_maps_api, extract_place_details_sync

app = FastAPI(title="LeadGen Pro API")

# Setup Banco de Dados
database.init_db()

# Serve arquivos estáticos
app.mount("/static", StaticFiles(directory="static"), name="static")

# Controle de Varredura Concorrente e Logs
is_scraping_active = False
scraping_lock = threading.Lock()
extraction_lock = threading.Lock() # Impede que duas threads abram o Chrome persistente do Playwright juntas
scraping_logs = deque(maxlen=2000) # Mantém os últimos 2000 logs na memória
scraping_progress = {
    "total_categories": 0,
    "completed_categories": 0,
    "current_action": ""
}

def add_global_log(msg):
    with scraping_lock:
        scraping_logs.append(msg)

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/leads")
def get_leads(db: Session = Depends(database.get_db)):
    leads = db.query(database.Lead).order_by(database.Lead.id.desc()).all()
    
    result = []
    for lead in leads:
        lead_dict = {
            "id": lead.id, "name": lead.name, "phone": lead.phone, 
            "address": lead.address, "website": lead.website,
            "status": lead.status, "site_status": lead.site_status,
            "search_term": lead.search_term, "rating": lead.rating,
            "created_at": lead.created_at.isoformat() if lead.created_at else None
        }
        
        # --- Cálculo de Score (Algoritmo IA movido do frontend) ---
        score = 0
        if lead.site_status == 'Site Fora do Ar': score += 50
        elif lead.site_status == 'Apenas Rede Social': score += 40
        elif lead.site_status == 'Sem Site': score += 30
        
        term = (lead.search_term or "").lower()
        highTicket = ['odontol', 'médic', 'estética', 'arquitet', 'construtora', 'energia', 'imobiliária', 'advoga', 'pilates', 'fisioterapia']
        midTicket = ['mecânica', 'pet shop', 'contabilidade', 'móveis', 'veterinária', 'psicólogo', 'corretora', 'despachante', 'ótica', 'marcenaria', 'vidraçaria', 'serralheria', 'dedetizadora']
        
        if any(t in term for t in highTicket): score += 30
        elif any(t in term for t in midTicket): score += 20
        else: score += 10
        
        if lead.rating and lead.rating.strip() != "": score += 10
        
        if lead.phone and lead.phone.strip() != "": score += 10
        else: score -= 50
        
        lead_dict["score"] = score
        result.append(lead_dict)
        
    return result

@app.get("/api/status")
def get_status():
    with scraping_lock:
        return {"is_scraping": is_scraping_active}

class ExtractRequest(BaseModel):
    name: str
    address: str

def cleanup_zip(file_path: str):
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            # Remove a pasta temporária original se ainda existir
            base_dir = file_path.replace('.zip', '')
            if os.path.exists(base_dir):
                shutil.rmtree(base_dir, ignore_errors=True)
        except Exception as e:
            print(f"Erro ao limpar arquivos temporários: {e}")

@app.post("/api/extract-details")
def extract_details(req: ExtractRequest):
    if not extraction_lock.acquire(blocking=False):
        raise HTTPException(status_code=400, detail="Aguarde. Já existe uma extração profunda em andamento e ela não pode ser feita em paralelo por segurança do sistema.")
        
    try:
        zip_path = extract_place_details_sync(req.name, req.address)
        if not zip_path or not os.path.exists(zip_path):
            raise HTTPException(status_code=404, detail="Não foi possível extrair dados ou criar o arquivo.")
            
        # O BackgroundTask atrelado ao FileResponse só executa após a conexão HTTP fechar com segurança
        task = BackgroundTask(cleanup_zip, zip_path)
        
        # Limpa o nome do arquivo para o header
        safe_name = "".join([c for c in req.name if c.isalpha() or c.isdigit() or c==' ']).rstrip()
        filename = f"{safe_name}_Dados.zip".replace(" ", "_")
        
        return FileResponse(
            zip_path, 
            filename=filename,
            media_type="application/zip",
            background=task
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        extraction_lock.release()

@app.get("/api/progress")
def get_progress():
    with scraping_lock:
        return {
            "is_scraping": is_scraping_active,
            "progress": scraping_progress,
            "logs": list(scraping_logs)
        }

@app.post("/api/leads/{lead_id}/status")
def update_lead_status(lead_id: int, status: str, db: Session = Depends(database.get_db)):
    lead = db.query(database.Lead).filter(database.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    lead.status = status
    db.commit()
    return {"message": "Status atualizado"}

def scrape_and_save(search_term: str, max_results: int, mode: str = "scraper"):
    """Função core que raspa e salva um termo no BD. Segura para threads."""
    db = database.SessionLocal()
    
    def log_cb(msg):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        add_global_log(f"[{now}] {msg}")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        if mode == "api":
            results = loop.run_until_complete(scrape_google_maps_api(search_term, max_results, log_cb))
        else:
            results = loop.run_until_complete(scrape_google_maps(search_term, max_results, log_cb))
        
        for r in results:
            name = r.get("name")
            phone = r.get("phone")
            
            # Deduplicação restrita: verifica APENAS pelo nome exato para não misturar filiais/clínicas
            existing = db.query(database.Lead).filter(database.Lead.name == name).first()
                
            if not existing:
                new_lead = database.Lead(
                    name=name,
                    phone=phone,
                    address=r.get("address"),
                    website=r.get("website"),
                    rating=r.get("rating"),
                    site_status=r.get("site_status"),
                    search_term=r.get("search_term")
                )
                db.add(new_lead)
        db.commit()
        log_cb("✅ Salvo no banco de dados.")
    except Exception as e:
        log_cb(f"❌ Erro fatal no scraper: {e}")
    finally:
        db.close()
        with scraping_lock:
            scraping_progress["completed_categories"] += 1

def run_scraper_single_bg(search_term: str, max_results: int, mode: str = "scraper"):
    global is_scraping_active
    try:
        scrape_and_save(search_term, max_results, mode)
    finally:
        with scraping_lock:
            is_scraping_active = False

def run_scraper_multi_bg(city: str, categories: list, max_results_per_cat: int, mode: str = "scraper"):
    """Roda a raspagem com threads (max_workers=3) para não sobrecarregar o PC"""
    global is_scraping_active
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for cat in categories:
                search_term = f"{cat} em {city}"
                add_global_log(f"⏳ Enfileirando varredura para: {search_term}")
                futures.append(executor.submit(scrape_and_save, search_term, max_results_per_cat, mode))
            # Esperar todas as threads terminarem
            concurrent.futures.wait(futures)
    except Exception as e:
        add_global_log(f"❌ Erro crítico no controlador de threads: {e}")
    finally:
        with scraping_lock:
            is_scraping_active = False
            add_global_log("🚀 Varredura Profunda Concluída!")

@app.post("/api/scrape")
def start_scraping(search_term: str, background_tasks: BackgroundTasks, max_results: int = 999, mode: str = "scraper", db: Session = Depends(database.get_db)):
    global is_scraping_active
    
    if not search_term:
        raise HTTPException(status_code=400, detail="search_term is required")
        
    with scraping_lock:
        if is_scraping_active:
            raise HTTPException(status_code=400, detail="Uma varredura já está em andamento.")
        
        is_scraping_active = True
        scraping_logs.clear()
        scraping_progress["total_categories"] = 1
        scraping_progress["completed_categories"] = 0
        mode_label = "⚡ Modo Turbo (API)" if mode == "api" else "🤖 Modo Scraper"
        scraping_progress["current_action"] = f"{mode_label}: {search_term}"
        
    background_tasks.add_task(run_scraper_single_bg, search_term, max_results, mode)
    return {"message": "Varredura iniciada em segundo plano."}

from pydantic import BaseModel
class AutoPilotRequest(BaseModel):
    city: str
    categories: list[str]
    mode: str = "scraper"

@app.post("/api/scrape/autopilot")
def start_autopilot(req: AutoPilotRequest, background_tasks: BackgroundTasks, db: Session = Depends(database.get_db)):
    global is_scraping_active
    
    if not req.city or not req.categories:
        raise HTTPException(status_code=400, detail="city and categories are required")
        
    with scraping_lock:
        if is_scraping_active:
            raise HTTPException(status_code=400, detail="Uma varredura já está em andamento.")
            
        is_scraping_active = True
        scraping_logs.clear()
        scraping_progress["total_categories"] = len(req.categories)
        scraping_progress["completed_categories"] = 0
        mode_label = "⚡ Modo Turbo (API)" if req.mode == "api" else "🤖 Modo Scraper"
        scraping_progress["current_action"] = f"Piloto Automático {mode_label} ({len(req.categories)} nichos) em {req.city}"
        
    background_tasks.add_task(run_scraper_multi_bg, req.city, req.categories, 999, req.mode)
    return {"message": "Piloto Automático ativado! O robô vai varrer todas as categorias da cidade em segundo plano."}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, access_log=False)
