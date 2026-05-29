from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
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

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import database
from scraper import scrape_google_maps, extract_place_details_sync

app = FastAPI(title="LeadGen Pro API")

# Setup Banco de Dados
database.init_db()

# Serve arquivos estáticos
app.mount("/static", StaticFiles(directory="static"), name="static")

# Controle de Varredura Concorrente e Logs
is_scraping_active = False
scraping_lock = threading.Lock()
scraping_logs = deque(maxlen=200) # Mantém apenas os últimos 200 logs na memória
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
    return leads

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
def extract_details(req: ExtractRequest, background_tasks: BackgroundTasks):
    try:
        zip_path = extract_place_details_sync(req.name, req.address)
        if not zip_path or not os.path.exists(zip_path):
            raise HTTPException(status_code=404, detail="Não foi possível extrair dados ou criar o arquivo.")
            
        background_tasks.add_task(cleanup_zip, zip_path)
        
        # Limpa o nome do arquivo para o header
        safe_name = "".join([c for c in req.name if c.isalpha() or c.isdigit() or c==' ']).rstrip()
        filename = f"{safe_name}_Dados.zip".replace(" ", "_")
        
        return FileResponse(
            zip_path, 
            filename=filename,
            media_type="application/zip"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

def scrape_and_save(search_term: str, max_results: int):
    """Função core que raspa e salva um termo no BD. Segura para threads."""
    db = database.SessionLocal()
    
    def log_cb(msg):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        add_global_log(f"[{now}] {msg}")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(scrape_google_maps(search_term, max_results, log_cb))
        
        for r in results:
            name = r.get("name")
            phone = r.get("phone")
            
            # Deduplicação: verifica se já existe loja com mesmo Nome OU mesmo Telefone
            if phone:
                existing = db.query(database.Lead).filter(
                    or_(database.Lead.name == name, database.Lead.phone == phone)
                ).first()
            else:
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

def run_scraper_single_bg(search_term: str, max_results: int):
    global is_scraping_active
    try:
        scrape_and_save(search_term, max_results)
    finally:
        with scraping_lock:
            is_scraping_active = False

def run_scraper_multi_bg(city: str, categories: list, max_results_per_cat: int):
    """Roda a raspagem com threads (max_workers=3) para não sobrecarregar o PC"""
    global is_scraping_active
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for cat in categories:
                search_term = f"{cat} em {city}"
                add_global_log(f"⏳ Enfileirando varredura para: {search_term}")
                futures.append(executor.submit(scrape_and_save, search_term, max_results_per_cat))
            # Esperar todas as threads terminarem
            concurrent.futures.wait(futures)
    except Exception as e:
        add_global_log(f"❌ Erro crítico no controlador de threads: {e}")
    finally:
        with scraping_lock:
            is_scraping_active = False
            add_global_log("🚀 Varredura Profunda Concluída!")

@app.post("/api/scrape")
def start_scraping(search_term: str, background_tasks: BackgroundTasks, max_results: int = 999, db: Session = Depends(database.get_db)):
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
        scraping_progress["current_action"] = f"Varrendo nicho específico: {search_term}"
        
    background_tasks.add_task(run_scraper_single_bg, search_term, max_results)
    return {"message": "Varredura iniciada em segundo plano."}

from pydantic import BaseModel
class AutoPilotRequest(BaseModel):
    city: str
    categories: list[str]

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
        scraping_progress["current_action"] = f"Piloto Automático ({len(req.categories)} nichos) em {req.city}"
        
    # max_results 999 para varrer tudo na varredura profunda
    background_tasks.add_task(run_scraper_multi_bg, req.city, req.categories, 999)
    return {"message": "Piloto Automático ativado! O robô vai varrer todas as categorias da cidade em segundo plano."}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, access_log=False)
