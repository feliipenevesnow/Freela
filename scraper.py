import asyncio
import concurrent.futures
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import os
import tempfile
import shutil
import json
import urllib.parse
import urllib.request
import sys
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

class Avaliacao(BaseModel):
    autor: str
    nota: str
    data: str
    texto: str

class PlaceData(BaseModel):
    sobre: List[str] = Field(description="Lista de comodidades e avisos do local", default_factory=list)
    horarios: List[str] = Field(description="Lista de horários de funcionamento. Ex: 'segunda-feira: 08:00–18:00'", default_factory=list)
    avaliacoes: List[Avaliacao] = Field(description="Lista com as avaliações mais recentes (até 10)", default_factory=list)
    fotos_urls: List[str] = Field(description="Lista de URLs grandes das fotos do local", default_factory=list)
    
    # Novos campos para capturar todas as informacoes da Visão Geral
    endereco_completo: Optional[str] = Field(description="Endereço completo listado", default="")
    telefone: Optional[str] = Field(description="Número de telefone", default="")
    website: Optional[str] = Field(description="Website principal listado", default="")
    plus_code: Optional[str] = Field(description="Plus code (ex: PGC3+89 Pirapozinho)", default="")
    resultados_web: List[str] = Field(description="Textos e links da seção 'Resultados da Web' ou Perfis Sociais (ex: links de instagram, facebook, CNPJ)", default_factory=list)
    lugares_similares: List[str] = Field(description="Nomes dos 'Lugares também pesquisados' (concorrentes)", default_factory=list)
    informacoes_adicionais: Optional[str] = Field(description="Qualquer outra informação útil (acessibilidade, descrições longas)", default="")

def clean_html_for_llm(html_content: str) -> str:
    soup = BeautifulSoup(html_content, 'html.parser')
    for tag in soup(['script', 'style', 'svg', 'path', 'iframe', 'noscript', 'meta', 'link']):
        tag.decompose()
    # Limpa atributos para economizar milhares de tokens
    for tag in soup.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in ['src', 'href', 'aria-label', 'role']}
    return str(soup).replace('\n', '').replace('  ', ' ')

def ask_llm_to_parse(html_content: str) -> dict:
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = '''Você é um extrator de dados de alta precisão. Eu vou te passar o HTML minificado de uma página do Google Maps de um estabelecimento comercial. Extraia as informações.'''
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, "HTML:\n" + html_content[:800000]],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PlaceData,
                temperature=0.1
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[DEBUG] Erro ao consultar a LLM: {e}")
        return {"sobre": [], "horarios": [], "avaliacoes": [], "fotos_urls": []}

async def scrape_google_maps(search_term: str, max_results: int = 10, on_log=None):
    def emit_log(msg):
        print(msg)
        if on_log:
            on_log(msg)

    leads = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        
        # Bloqueio de recursos para acelerar o carregamento
        async def block_resources(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", block_resources)
        
        encoded_term = urllib.parse.quote_plus(search_term)
        url = f"https://www.google.com/maps/search/{encoded_term}"
        emit_log(f"Iniciando pesquisa no Google Maps: {search_term}")
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Esperar a lista de resultados carregar
            await page.wait_for_selector('a[href*="/maps/place/"]', timeout=15000)
            
            # Rolar a lista até o fim para carregar TODOS os resultados possíveis
            previous_count = 0
            retries = 0
            while True:
                places_elements = await page.locator('a[href*="/maps/place/"]').all()
                if len(places_elements) == previous_count:
                    retries += 1
                    if retries >= 3: # Se tentou rolar 3 vezes e não vieram itens novos, chegou ao fim
                        emit_log(f"Busca completa! Encontrados {len(places_elements)} locais para extrair.")
                        break
                else:
                    retries = 0
                    if previous_count == 0:
                        emit_log("Primeiros locais encontrados, expandindo lista (Varredura Profunda ativada)...")
                    else:
                        emit_log(f"Carregando mais resultados... (Atualmente: {len(places_elements)})")
                
                previous_count = len(places_elements)
                
                if places_elements:
                    try:
                        # Rolar para o último elemento força o Google a renderizar a próxima página
                        await places_elements[-1].scroll_into_view_if_needed()
                        # Pequeno empurrão extra com o scroll
                        await page.mouse.wheel(0, 1000)
                    except:
                        pass
                await asyncio.sleep(2.5) # Tempo generoso para a requisição do Google Maps carregar
            
            # Pegar todos os links de lugares na tela
            places = await page.locator('a[href*="/maps/place/"]').all()
            
            count = 0
            for place in places:
                if count >= max_results:
                    break
                    
                try:
                    # Nome
                    name = ""
                    try:
                        name_el = await place.get_attribute('aria-label')
                        if name_el:
                            name = name_el.strip()
                    except:
                        pass
                    
                    if not name:
                        continue

                    # Clicar no lugar
                    await place.click()
                    
                    # Espera inteligente pelo painel carregar com o titulo da loja (H1)
                    try:
                        safe_name_js = json.dumps(name)
                        await page.wait_for_function(
                            f"() => {{ const h1 = document.querySelector('h1'); return h1 && h1.innerText.includes({safe_name_js}); }}",
                            timeout=3000
                        )
                        await asyncio.sleep(0.3) # Pequeno sleep garantido pro resto do DOM estabilizar
                    except:
                        await asyncio.sleep(1.5) # Fallback se não detectar
                        
                    # Endereço
                    address = ""
                    try:
                        addr_el = await page.locator('button[data-item-id="address"]').first.inner_text()
                        address = addr_el.split('\n')[-1].strip()
                    except:
                        pass
                        
                    # Telefone
                    phone = ""
                    try:
                        phone_el = await page.locator('button[data-tooltip="Copiar número de telefone"]').first.inner_text()
                        phone = phone_el.split('\n')[-1].strip()
                    except:
                        pass
                        
                    # Website
                    website = ""
                    try:
                        website_el = await page.locator('a[data-item-id="authority"]').first.get_attribute('href')
                        website = website_el
                    except:
                        pass
                        
                    # Avaliação
                    rating = ""
                    try:
                        rating_el = await page.locator('div[role="img"][aria-label*="estrelas"]').first.get_attribute('aria-label')
                        rating = rating_el
                    except:
                        pass
                        
                    site_status = "Sem Site"
                    if website:
                        # Identifica se é apenas uma rede social em vez de site próprio
                        social_domains = ['instagram.com', 'facebook.com', 'wa.me', 'whatsapp.com', 'linktr.ee', 'api.whatsapp', 'youtube.com', 'tiktok.com']
                        if any(domain in website.lower() for domain in social_domains):
                            site_status = "Apenas Rede Social"
                        else:
                            site_status = "Pendente" # Será verificado em paralelo no final
                    
                    if name:
                        leads.append({
                            "name": name,
                            "address": address,
                            "phone": phone,
                            "website": website,
                            "rating": rating,
                            "site_status": site_status,
                            "search_term": search_term
                        })
                        safe_name = name.encode('ascii', 'ignore').decode('ascii')
                        safe_phone = phone if phone else "Sem Telefone"
                        emit_log(f"Lead Extraído: {safe_name} | {safe_phone}")
                    
                    count += 1
                except Exception as e:
                    emit_log(f"Erro ao extrair um local: {e}")
                    continue
                    
            # --- Início do Teste em Paralelo dos Sites ---
            site_sem = asyncio.Semaphore(10)  # Máximo 10 conexões simultâneas
            async def check_site(lead):
                if lead["site_status"] != "Pendente":
                    return
                async with site_sem:
                    try:
                        resp = await asyncio.wait_for(
                            context.request.get(lead["website"], timeout=5000, ignore_https_errors=True),
                            timeout=7
                        )
                        if resp.ok:
                            text = await resp.text()
                            if "Site not found" in text or "does not have a domain assigned" in text or "Account Suspended" in text:
                                lead["site_status"] = "Site Fora do Ar"
                            else:
                                lead["site_status"] = "Com Site"
                        else:
                            lead["site_status"] = "Site Fora do Ar"
                    except:
                        lead["site_status"] = "Site Fora do Ar"
                    safe = lead['name'].encode('ascii','ignore').decode('ascii')
                    emit_log(f"Site [{lead['site_status']}]: {safe}")

            leads_to_check = [lead for lead in leads if lead["site_status"] == "Pendente"]
            if leads_to_check:
                emit_log(f"Verificando status de {len(leads_to_check)} sites em paralelo...")
                await asyncio.gather(*(check_site(lead) for lead in leads_to_check))
            # --- Fim do Teste em Paralelo ---

        except Exception as e:
            emit_log(f"Erro na busca geral: {e}")
            
        await browser.close()
        
    return leads

async def scrape_google_maps_api(search_term: str, max_results: int = 999, on_log=None):
    """Modo Turbo: usa a Google Places API v1 para varredura ultra-rápida."""
    def emit_log(msg):
        print(msg)
        if on_log:
            on_log(msg)

    if not GOOGLE_PLACES_API_KEY:
        emit_log("❌ GOOGLE_PLACES_API_KEY não encontrada no arquivo .env!")
        return []

    social_domains = ['instagram.com', 'facebook.com', 'wa.me', 'whatsapp.com', 'linktr.ee', 'api.whatsapp', 'youtube.com', 'tiktok.com']
    leads = []
    places_raw = []
    emit_log(f"⚡ [API] Varredura Turbo iniciada: {search_term}")

    def _do_api_request(body_dict):
        """Faz a chamada HTTP à Places API (síncrona, rodará em thread)."""
        body_bytes = json.dumps(body_dict).encode('utf-8')
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.nationalPhoneNumber,places.websiteUri,places.rating,places.userRatingCount,nextPageToken"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8'))

    next_page_token = None
    while len(places_raw) < max_results:
        body = {
            "textQuery": search_term,
            "languageCode": "pt-BR",
            "maxResultCount": min(20, max_results - len(places_raw))
        }
        if next_page_token:
            body["pageToken"] = next_page_token

        try:
            data = await asyncio.to_thread(_do_api_request, body)
        except Exception as e:
            emit_log(f"❌ Erro na chamada à API: {e}")
            if "HTTP Error 403" in str(e) or "HTTP Error 400" in str(e):
                emit_log("⚠️ Verifique se a 'Places API (New)' está ativada no Google Cloud Console e se a chave está correta no .env")
            break

        if "error" in data:
            msg = data['error'].get('message', str(data['error']))
            emit_log(f"❌ Erro retornado pela API: {msg}")
            if data['error'].get('status') in ['REQUEST_DENIED', 'PERMISSION_DENIED']:
                emit_log("⚠️ Verifique se a 'Places API (New)' está ativada no Google Cloud Console.")
            break

        batch = data.get("places", [])
        if not batch:
            break

        places_raw.extend(batch)
        next_page_token = data.get("nextPageToken")
        emit_log(f"📍 {len(places_raw)} locais carregados via API...")

        if not next_page_token:
            emit_log("✅ Todos os resultados da API foram carregados.")
            break

    emit_log(f"🔍 Processando {len(places_raw)} locais e verificando sites em paralelo...")

    for place in places_raw[:max_results]:
        name = place.get("displayName", {}).get("text", "")
        if not name:
            continue
        phone = place.get("nationalPhoneNumber", "")
        address = place.get("formattedAddress", "")
        website = place.get("websiteUri", "")
        rating_val = place.get("rating", "")
        rating_count = place.get("userRatingCount", "")
        rating = f"{rating_val} estrelas ({rating_count} avaliações)" if rating_val else ""

        site_status = "Sem Site"
        if website:
            if any(d in website.lower() for d in social_domains):
                site_status = "Apenas Rede Social"
            else:
                site_status = "Pendente"

        leads.append({
            "name": name, "phone": phone, "address": address,
            "website": website, "rating": rating,
            "site_status": site_status, "search_term": search_term
        })
        safe_name = name.encode('ascii', 'ignore').decode('ascii')
        emit_log(f"Lead Extraído: {safe_name} | {phone if phone else 'Sem Telefone'}")

    # Verificação paralela dos sites via ThreadPoolExecutor dedicado
    # (evita o bug de orphaned threads que esgota o pool do asyncio.to_thread)
    def _verificar_sites_em_batch(lista_leads, log_fn):
        """Roda verificação de sites num executor próprio, com timeout real por site."""
        def checar_um(lead):
            if lead["site_status"] != "Pendente":
                return lead
            try:
                req = urllib.request.Request(lead["website"], headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=6) as r:
                    code = r.getcode()
                    text = r.read(5000).decode('utf-8', errors='ignore')
                if code < 400 and not any(x in text for x in ["Site not found", "does not have a domain assigned", "Account Suspended"]):
                    lead["site_status"] = "Com Site"
                else:
                    lead["site_status"] = "Site Fora do Ar"
            except:
                lead["site_status"] = "Site Fora do Ar"
            return lead

        # max_workers=10: verifica 10 sites em paralelo; timeout=120s no total
        with concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="site_chk") as ex:
            future_to_lead = {ex.submit(checar_um, lead): lead for lead in lista_leads}
            try:
                for future in concurrent.futures.as_completed(future_to_lead, timeout=120):
                    try:
                        result = future.result(timeout=8)
                    except Exception:
                        result = future_to_lead[future]
                        result["site_status"] = "Site Fora do Ar"
                    safe = result['name'].encode('ascii', 'ignore').decode('ascii')
                    log_fn(f"Site [{result['site_status']}]: {safe}")
            except concurrent.futures.TimeoutError:
                # Se o batch todo demorar mais de 2 min, cancela o resto
                for future, lead in future_to_lead.items():
                    if not future.done():
                        future.cancel()
                        if lead["site_status"] == "Pendente":
                            lead["site_status"] = "Site Fora do Ar"

    leads_to_check = [l for l in leads if l["site_status"] == "Pendente"]
    if leads_to_check:
        emit_log(f"🌐 Verificando {len(leads_to_check)} sites (10 em paralelo, timeout 6s/site)...")
        # Um único asyncio.to_thread para o batch todo (sem orphan threads)
        await asyncio.to_thread(_verificar_sites_em_batch, leads_to_check, emit_log)

    emit_log(f"🎉 [API Turbo] Concluído! {len(leads)} leads captados.")
    return leads


async def extract_place_details(name: str, address: str) -> str:
    # 1. Create temp directory
    temp_dir = tempfile.mkdtemp()
    base_name = "".join([c for c in name if c.isalpha() or c.isdigit() or c==' ']).rstrip().replace(" ", "_")
    target_dir = os.path.join(temp_dir, base_name)
    
    # Subpastas
    paths = {
        "avaliacoes": os.path.join(target_dir, "Avaliações"),
        "horarios": os.path.join(target_dir, "Horários"),
        "imagens": os.path.join(target_dir, "Imagens"),
        "empresa": os.path.join(target_dir, "Empresa"),
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
        
    async with async_playwright() as p:
        # Usando um contexto persistente para salvar o cookie do Google
        # Assim, se você resolver o CAPTCHA uma vez, ele não vai pedir nos próximos.
        user_data_dir = os.path.join(os.getcwd(), "chrome_session")
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await Stealth().apply_stealth_async(page)
        
        async def check_captcha(p_page):
            if await p_page.locator('form#captcha-form, form#challenge-form').count() > 0 or 'sorry/index' in p_page.url:
                print("\n" + "="*50)
                print("🚨 CAPTCHA DETECTADO! 🚨")
                print("O Google bloqueou a busca. Vá para a janela do Chrome que abriu e marque 'Não sou um robô'.")
                print("O script vai esperar até você resolver...")
                print("="*50 + "\n")
                while 'sorry/index' in p_page.url or await p_page.locator('form#captcha-form').count() > 0:
                    await p_page.wait_for_timeout(2000)
                print("[DEBUG] CAPTCHA resolvido! Continuando a extração...")
                await p_page.wait_for_timeout(2000)
        
        # Otimizando a busca para evitar bloqueios por query longa
        short_address = address.split('-')[0].split(',')[0].strip() if address else ""
        search_query_maps = f"{name} {address}".replace(' ', '+')
        search_query_search = f"{name} {short_address}".strip().replace(' ', '+')
        
        url_maps = f"https://www.google.com/maps/search/{search_query_maps}"
        
        # Estruturas de Memória Separadas (Maps e Search)
        maps_about_texts = set()
        maps_horarios = set()
        maps_reviews = {}
        maps_keywords = set()
        maps_photo_urls = set()
        
        search_about_texts = set()
        search_horarios = set()
        search_reviews = {}
        search_keywords = set()
        search_photo_urls = set()
        
        # =======================================
        # FASE 1: GOOGLE MAPS
        # =======================================
        print(f"[DEBUG] [Maps] Iniciando navegação: {url_maps}")
        try:
            await page.goto(url_maps, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            print(f"[DEBUG] [Maps] Aviso de timeout ou erro ao carregar página: {e} - tentando continuar a extração...")
        await check_captcha(page)
        await page.wait_for_timeout(3000)
        

        
        if await page.locator('.hfpxzc').count() > 0:
            await page.locator('.hfpxzc').first.click()
            await page.wait_for_timeout(3000)
            
        print("[DEBUG] [Maps] Coletando HTML da Visão Geral e rolando a página...")
        try:
            # Rola a visão geral para baixo para carregar os widgets (como o carrossel de fotos no fim)
            scrollable = page.locator('div.m6QErb.DxyBCb').first
            if await scrollable.count() > 0:
                for _ in range(6):
                    await scrollable.evaluate('el => el.scrollTop += 1500')
                    await page.wait_for_timeout(500)
            else:
                for _ in range(6):
                    await page.mouse.wheel(0, 1500)
                    await page.wait_for_timeout(500)

            expand_btn = page.locator('div[data-item-id="oh"]').first
            if await expand_btn.count() > 0:
                await expand_btn.click()
                await page.wait_for_timeout(1000)
        except: pass
        html_overview = await page.content()
        
        # Salva todo o texto cru da página para não perder absolutamente nada!
        try:
            overview_text = await page.evaluate("() => document.body.innerText")
            with open(os.path.join(paths['empresa'], "Maps_Visao_Geral_Completa_Crua.txt"), "w", encoding="utf-8") as f:
                f.write(overview_text)
        except Exception as e:
            print(f"[DEBUG] Erro ao extrair texto cru: {e}")
            
        print("[DEBUG] [Maps] Coletando HTML das Avaliações (todas)...")
        direct_reviews = []
        try:
            import re
            tab_reviews = page.locator('button[role="tab"]').filter(has_text=re.compile(r"Avalia", re.IGNORECASE)).first
            if await tab_reviews.count() == 0:
                tab_reviews = page.locator('div[role="tablist"] button').nth(1)
            if await tab_reviews.count() > 0:
                await tab_reviews.click()
                await page.wait_for_timeout(2500)

                # Força o carregamento iterativo das avaliações rolando até a última disponível
                print("[DEBUG] [Maps] Rolando avaliações para forçar o carregamento de todas...")
                for _ in range(15):
                    try:
                        revs = await page.locator('div.jftiEf').all()
                        if revs:
                            await revs[-1].scroll_into_view_if_needed()
                            await page.mouse.wheel(0, 800) # Empurrão extra
                    except: pass
                    await page.wait_for_timeout(800)

                # Clica em todos os botões "Mais" para expandir avaliações truncadas
                mais_buttons = page.locator('button.w8nwRe, button[aria-label="Ver mais"]')
                count_mais = await mais_buttons.count()
                print(f"[DEBUG] Expandindo {count_mais} avaliações truncadas...")
                for i in range(count_mais):
                    try:
                        btn = mais_buttons.nth(i)
                        if await btn.is_visible():
                            await btn.click()
                            await page.wait_for_timeout(200)
                    except: pass

                html_reviews = await page.content()

                # Extração direta das avaliações via BeautifulSoup
                soup_rev = BeautifulSoup(html_reviews, 'html.parser')
                for card in soup_rev.find_all('div', class_='jftiEf'):
                    # Nome do avaliador
                    name_el = card.find('div', class_='d4r55')
                    name = name_el.get_text(strip=True) if name_el else "Anônimo"
                    
                    # Nível / Local Guide (métricas)
                    metrics_el = card.find('div', class_='RfnDt')
                    user_metrics = metrics_el.get_text(strip=True) if metrics_el else ""
                    
                    # Link do Perfil e Avatar URL
                    avatar_btn = card.find('button', class_='WEBjve')
                    profile_url = avatar_btn.get('data-href', '') if avatar_btn else ""
                    
                    avatar_img = card.find('img', class_='NBa7we')
                    avatar_url = avatar_img.get('src', '') if avatar_img else ""

                    # Estrelas
                    stars_el = card.find('span', class_='kvMYJc')
                    stars = ""
                    if stars_el:
                        label = stars_el.get('aria-label', '')
                        stars_match = re.search(r'(\d[\d,\.]*)\s+estrela', label)
                        stars = stars_match.group(1) if stars_match else ""

                    # Data
                    date_el = card.find('span', class_='rsqaWe')
                    date = date_el.get_text(strip=True) if date_el else ""

                    # Texto completo (após expansão)
                    text_el = card.find('span', class_='wiI7pd')
                    text = text_el.get_text(strip=True) if text_el else ""

                    if text:
                        direct_reviews.append({
                            "autor": name,
                            "perfil_url": profile_url,
                            "nivel_usuario": user_metrics,
                            "avatar_url": avatar_url,
                            "estrelas": stars,
                            "data": date,
                            "texto": text
                        })

                print(f"[DEBUG] [Maps] {len(direct_reviews)} avaliações extraídas diretamente do HTML.")
            else:
                html_reviews = await page.content()
        except Exception as e:
            print(f"[DEBUG] Erro ao coletar avaliações: {e}")
            html_reviews = await page.content()

        print("[DEBUG] [Maps] Coletando HTML das Fotos (Galeria Completa)...")
        try:
            import re as _re
            
            # 1. Volta para a aba Visão Geral (pois estávamos na aba de Avaliações)
            tab_overview = page.locator('button[role="tab"]').first
            if await tab_overview.count() > 0:
                await tab_overview.click()
                await page.wait_for_timeout(1500)
                
            # 2. Rola a visão geral para baixo para garantir que o carrossel de fotos (K4UgGe) apareça na DOM
            print("[DEBUG] [Maps] Rolando Visão Geral para encontrar o carrossel de fotos...")
            scrollable = page.locator('div.m6QErb.DxyBCb').first
            if await scrollable.count() > 0:
                for _ in range(6):
                    await scrollable.evaluate('el => el.scrollTop += 1500')
                    await page.wait_for_timeout(400)
            else:
                for _ in range(6):
                    await page.mouse.wheel(0, 1500)
                    await page.wait_for_timeout(400)

            # Estratégia: clicar na primeira miniatura do carrossel de fotos para abrir a galeria
            # O botão com class K4UgGe abre a tela cheia de fotos
            photo_carousel_btn = page.locator('button.K4UgGe').first
            if await photo_carousel_btn.count() > 0:
                await photo_carousel_btn.click()
                # Aguarda a galeria completa carregar (role="main" com aria-label contendo "Fotos")
                await page.wait_for_selector('div[role="main"][aria-label*="Foto"]', timeout=8000)
                await page.wait_for_timeout(1500)

                # Rola a galeria focando sempre na última foto carregada
                print("[DEBUG] [Maps] Rolando galeria de fotos para forçar o carregamento em alta resolução...")
                for _ in range(12):
                    try:
                        fotos = await page.locator('a.MIgS0d').all()
                        if fotos:
                            await fotos[-1].scroll_into_view_if_needed()
                            await page.mouse.wheel(0, 800) # Empurrão extra
                    except: pass
                    await page.wait_for_timeout(800)

                html_photos = await page.content()

                # Extração direta: URLs das divs aHpZye e gCPOGf (background-image style)
                # Preferimos gCPOGf vCWwFf que têm resolução maior
                soup_gallery = BeautifulSoup(html_photos, 'html.parser')
                gallery_urls = set()

                # Prioridade 1: div.gCPOGf.vCWwFf (resolução s567 ou maior)
                for div in soup_gallery.find_all('div', class_=lambda c: c and 'gCPOGf' in c and 'vCWwFf' in c):
                    style = div.get('style', '')
                    match = _re.search(r'url\(["\']?(https://lh3\.googleusercontent\.com/[^"\')\s]+)["\']?\)', style)
                    if match:
                        # Eleva para resolução máxima trocando o sufixo de tamanho
                        url_hq = _re.sub(r'=w\d+-h\d+-k-no|=s\d+-k-no', '=s1200-k-no', match.group(1))
                        gallery_urls.add(url_hq)

                # Fallback: div.aHpZye (thumbnail, caso vCWwFf não carregue)
                if len(gallery_urls) < 3:
                    for div in soup_gallery.find_all('div', class_=lambda c: c and 'aHpZye' in c):
                        style = div.get('style', '')
                        match = _re.search(r'url\(["\']?(https://lh3\.googleusercontent\.com/[^"\')\s]+)["\']?\)', style)
                        if match:
                            url_hq = _re.sub(r'=w\d+-h\d+-k-no|=s\d+-k-no', '=s1200-k-no', match.group(1))
                            gallery_urls.add(url_hq)

                # Ignora Street View (pixelspa.googleapis.com)
                gallery_urls = {u for u in gallery_urls if 'lh3.googleusercontent.com' in u}
                maps_photo_urls = gallery_urls
                print(f"[DEBUG] [Maps] Galeria completa: {len(maps_photo_urls)} fotos únicas encontradas.")

                # Volta para a página principal para o LLM processar o HTML geral
                await page.go_back()
                await page.wait_for_timeout(1500)
                html_photos = await page.content()
            else:
                # Sem carrossel: tenta o botão "Ver fotos" antigo
                photo_btn = page.locator('button:has-text("Ver fotos"), button.aoYtNb, button.Dx2nRe').first
                if await photo_btn.count() > 0:
                    await photo_btn.click()
                    await page.wait_for_timeout(4000)
                html_photos = await page.content()
                maps_photo_urls = set()
        except Exception as e:
            print(f"[DEBUG] Erro ao abrir galeria de fotos: {e}")
            html_photos = await page.content()
            maps_photo_urls = set()
        
        print("[DEBUG] [LLM] Limpando HTML e chamando o Gemini...")
        combined_html = html_overview + html_reviews + html_photos
        clean_html = clean_html_for_llm(combined_html)
        
        llm_data = ask_llm_to_parse(clean_html)
        
        maps_about_texts = set(llm_data.get("sobre", []))
        maps_horarios = set(llm_data.get("horarios", []))
        # Usa avaliações diretas do HTML (muito mais precisas e completas) como prioridade
        if direct_reviews:
            maps_reviews = {f"rev_{i}": rev for i, rev in enumerate(direct_reviews)}
        else:
            maps_reviews = {f"rev_{i}": rev for i, rev in enumerate(llm_data.get("avaliacoes", []))}
        maps_keywords = set()
        # Mescla: URLs da galeria direta (alta qualidade) + o que o LLM eventualmente pegou
        llm_photo_urls = set(llm_data.get("fotos_urls", []))
        maps_photo_urls = maps_photo_urls | llm_photo_urls

        print(f"[DEBUG] Total de URLs válidas de imagens coletadas Maps: {len(maps_photo_urls)}")

        # =======================================
        # FASE 2: SALVAMENTO DOS DADOS DO MAPS
        # =======================================
        print("[DEBUG] Processando dados e salvando arquivos...")
        
        async def salvar_dados(prefix, about_texts, horarios, reviews, keywords, photo_urls, llm_full_data):
            import base64, urllib.request
            
            # Salva o JSON completo (muito mais robusto) retornado pelo LLM
            if llm_full_data:
                with open(os.path.join(paths['empresa'], f"{prefix}_Informacoes_Completas_Estruturadas.json"), "w", encoding="utf-8") as f:
                    json.dump(llm_full_data, f, ensure_ascii=False, indent=2)

            if about_texts:
                with open(os.path.join(paths['empresa'], f"{prefix}_comodidades_e_servicos.txt"), "w", encoding="utf-8") as f:
                    f.write("\n".join(sorted(list(about_texts))))
            
            if horarios:
                with open(os.path.join(paths['horarios'], f"{prefix}_grade_semanal.txt"), "w", encoding="utf-8") as f:
                    f.write("\n".join(sorted(list(horarios))))
                    
            if reviews:
                for rev_key, rev in reviews.items():
                    avatar_url = rev.get('avatar_url')
                    if avatar_url:
                        try:
                            # Baixa o avatar
                            req = urllib.request.Request(avatar_url, headers={'User-Agent': 'Mozilla/5.0'})
                            with urllib.request.urlopen(req, timeout=8) as r:
                                avatar_bytes = r.read()
                            if avatar_bytes:
                                # Nome limpo para o arquivo
                                safe_name = "".join([c for c in rev.get('autor', 'Anonimo') if c.isalpha() or c.isdigit()]).rstrip()
                                safe_name = safe_name.lower()
                                avatar_filename = f"{prefix}_avatar_{safe_name}.jpg"
                                with open(os.path.join(paths['avaliacoes'], avatar_filename), "wb") as f:
                                    f.write(avatar_bytes)
                                rev['imagem_arquivo'] = avatar_filename
                        except Exception as e:
                            print(f"[DEBUG] [{prefix}] Falha ao baixar avatar de {rev.get('autor')}: {e}")
                            
                with open(os.path.join(paths['avaliacoes'], f"{prefix}_avaliacoes_detalhadas.json"), "w", encoding="utf-8") as f:
                    json.dump(list(reviews.values()), f, ensure_ascii=False, indent=2)
                    
            if keywords:
                with open(os.path.join(paths['avaliacoes'], f"{prefix}_resumo_palavras_chave.txt"), "w", encoding="utf-8") as f:
                    f.write(", ".join(keywords))

            count = 0
            for url_img in list(photo_urls):
                if count >= 15: break
                
                # Ignorar URLs relativas (sem protocolo) - são inválidas
                if url_img.startswith('//'):
                    print(f"[DEBUG] [{prefix}] URL relativa ignorada: {url_img[:60]}")
                    continue
                
                # Processa imagens base64 capturadas diretamente do HTML
                if url_img.startswith('data:image/'):
                    try:
                        header, b64_data = url_img.split(',', 1)
                        img_bytes = base64.b64decode(b64_data)
                        if len(img_bytes) > 2000:
                            with open(os.path.join(paths['imagens'], f"{prefix}_foto_{count+1}.jpg"), "wb") as f:
                                f.write(img_bytes)
                            print(f"[DEBUG] [{prefix}] Foto base64 {count+1} salva ({len(img_bytes)} bytes).")
                            count += 1
                        else:
                            print(f"[DEBUG] [{prefix}] Base64 muito pequena ({len(img_bytes)} bytes), ignorada.")
                    except Exception as e:
                        print(f"[DEBUG] [{prefix}] Erro ao decodificar base64: {e}")
                    continue
                    
                # Normaliza URL para alta resolução
                if '/p/' in url_img or 'gps-cs-s' in url_img or '=w' in url_img or '=s' in url_img:
                    high_res_url = url_img.split('=')[0] + '=s1200'
                else:
                    high_res_url = url_img
                    
                print(f"[DEBUG] [{prefix}] Tentando baixar: {high_res_url[:80]}...")
                
                # Tenta 1: Playwright context.request (mesma sessão do browser)
                img_bytes = None
                try:
                    resp = await context.request.get(high_res_url, timeout=8000)
                    if resp.ok:
                        img_bytes = await resp.body()
                except Exception as e:
                    print(f"[DEBUG] [{prefix}] Playwright falhou: {e}")
                    
                # Tenta 2: urllib nativo como fallback
                if not img_bytes:
                    try:
                        req = urllib.request.Request(high_res_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, timeout=8) as r:
                            img_bytes = r.read()
                        print(f"[DEBUG] [{prefix}] urllib OK, {len(img_bytes)} bytes.")
                    except Exception as e:
                        print(f"[DEBUG] [{prefix}] urllib falhou: {e}")
                
                if img_bytes and len(img_bytes) > 10000:
                    with open(os.path.join(paths['imagens'], f"{prefix}_foto_{count+1}.jpg"), "wb") as f:
                        f.write(img_bytes)
                    print(f"[DEBUG] [{prefix}] Foto {count+1} salva ({len(img_bytes)} bytes).")
                    count += 1
                elif img_bytes:
                    print(f"[DEBUG] [{prefix}] Imagem muito pequena ({len(img_bytes)} bytes), ignorada.")
                
            print(f"[DEBUG] [{prefix}] Concluído: {len(about_texts)} infos, {len(horarios)} horários, {len(reviews)} avaliações, {count} fotos unicas salvas.")

        # Executando salvamento de cada origem
        await salvar_dados("Maps", maps_about_texts, maps_horarios, maps_reviews, maps_keywords, maps_photo_urls, llm_data)

        await context.close()
        
    zip_path = shutil.make_archive(target_dir, 'zip', target_dir)
    return zip_path

def extract_place_details_sync(name: str, address: str) -> str:
    # Cria um novo event loop totalmente limpo na thread do FastAPI
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(extract_place_details(name, address))
