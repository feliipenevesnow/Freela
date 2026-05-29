import asyncio
from playwright.async_api import async_playwright
import os
import tempfile
import shutil
import json
import urllib.parse
import sys

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
                    # Clicar no lugar
                    await place.click()
                    await asyncio.sleep(2) # Esperar o painel carregar
                    
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
                            # Testa se o domínio está realmente online e funcionando
                            try:
                                # Um timeout curto de 5s para não atrasar a varredura
                                resp = await context.request.get(website, timeout=5000, ignore_https_errors=True)
                                if resp.ok:
                                    text = await resp.text()
                                    # Verifica textos comuns de páginas de erro de plataformas como Wix/Hostgator
                                    if "Site not found" in text or "does not have a domain assigned" in text or "Account Suspended" in text:
                                        site_status = "Site Fora do Ar"
                                    else:
                                        site_status = "Com Site"
                                else:
                                    site_status = "Site Fora do Ar"
                            except:
                                # Se der timeout ou erro de DNS (domínio expirado)
                                site_status = "Site Fora do Ar"
                    
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
                    
        except Exception as e:
            emit_log(f"Erro na busca geral: {e}")
            
        await browser.close()
        
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
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1280, 'height': 800})
        page = await context.new_page()
        
        search_query = f"{name} {address}".replace(' ', '+')
        url_maps = f"https://www.google.com/maps/search/{search_query}"
        
        # Estruturas de Memória para Fusão (Fase 1)
        all_about_texts = set()
        all_horarios = set()
        all_reviews = {}
        all_keywords = set()
        all_photo_urls = set()
        
        # =======================================
        # FASE 1: GOOGLE MAPS
        # =======================================
        print(f"[DEBUG] [Maps] Iniciando navegação: {url_maps}")
        await page.goto(url_maps, wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)
        
        if await page.locator('.hfpxzc').count() > 0:
            await page.locator('.hfpxzc').first.click()
            await page.wait_for_timeout(3000)
            
        try:
            print("[DEBUG] [Maps] Extraindo Visão Geral...")
            for el in await page.locator('.Io6YTe').all():
                t = await el.inner_text()
                if t and len(t) > 3: all_about_texts.add(t.replace('\n', ' ').strip())
            for post in await page.locator('div[data-post-id]').all():
                pt = await post.inner_text()
                if pt: all_about_texts.add("Post: " + pt.replace('\n', ' ').strip())
        except Exception as e: print(f"[DEBUG] Erro Visão Geral Maps: {e}")

        try:
            print("[DEBUG] [Maps] Extraindo Horários...")
            expand_btn = page.locator('div[data-item-id="oh"]').first
            if await expand_btn.count() > 0:
                await expand_btn.click()
                await page.wait_for_timeout(2000)
            table = page.locator('table.eK4R0e')
            if await table.count() > 0:
                for row in await table.locator('tr').all():
                    cols = await row.locator('td').all()
                    if len(cols) >= 2:
                        dia = await cols[0].inner_text()
                        horario = await cols[1].inner_text()
                        all_horarios.add(f"{dia}: {horario.replace(chr(10), ' ').strip()}")
        except Exception as e: print(f"[DEBUG] Erro Horários Maps: {e}")

        try:
            print("[DEBUG] [Maps] Extraindo Avaliações...")
            import re
            tab_reviews = page.locator('button[role="tab"]').filter(has_text=re.compile(r"Avalia", re.IGNORECASE)).first
            if await tab_reviews.count() == 0:
                tab_reviews = page.locator('div[role="tablist"] button').nth(1)
                
            if await tab_reviews.count() > 0:
                await tab_reviews.click()
                await page.wait_for_timeout(3000)
                scrollable = page.locator('div.m6QErb[aria-label*="Avaliações"]').first
                if await scrollable.count() > 0:
                    await scrollable.evaluate('(element) => { element.scrollTop = 2000; }')
                    await page.wait_for_timeout(2000)
                
                for el in (await page.locator('div.jftiEf').all())[:10]:
                    try:
                        author = await el.locator('.d4r55').inner_text()
                        text_el = el.locator('.wiI7pd')
                        text = await text_el.inner_text() if await text_el.count() > 0 else ""
                        rating = await el.locator('.kvMYJc').get_attribute('aria-label')
                        date = await el.locator('.rsqaWe').inner_text()
                        all_reviews[f"{author}_{date}"] = {"autor": author, "nota": rating, "data": date, "texto": text}
                    except: pass
                for tag in await page.locator('.t3A4Ke').all():
                    all_keywords.add(await tag.inner_text())
        except Exception as e: print(f"[DEBUG] Erro Avaliações Maps: {e}")

        try:
            print("[DEBUG] [Maps] Extraindo Imagens...")
            photo_btn = page.locator('button:has-text("Ver fotos"), button.aoYtNb, button.Dx2nRe').first
            if await photo_btn.count() > 0:
                await photo_btn.click()
                await page.wait_for_timeout(4000)
            for div in await page.locator('div[style*="background-image"]').all():
                bg = await div.get_attribute('style')
                if bg and 'url(' in bg and 'googleusercontent' in bg:
                    try: all_photo_urls.add(bg.split('url("')[1].split('")')[0])
                    except:
                        try: all_photo_urls.add(bg.split('url(')[1].split(')')[0])
                        except: pass
            for img in await page.locator('img[src*="googleusercontent"]').all():
                src = await img.get_attribute('src')
                # Filtrar avatares de usuário - queremos apenas fotos do estabelecimento
                if src and ('gps-cs-s' in src or 'glsgmb' in src or '/p/' in src):
                    all_photo_urls.add(src)
        except Exception as e: print(f"[DEBUG] Erro Imagens Maps: {e}")

        # =======================================
        # FASE 2: GOOGLE SEARCH
        # =======================================
        url_search = f"https://www.google.com/search?q={search_query}"
        print(f"[DEBUG] [Search] Iniciando navegação: {url_search}")
        await page.goto(url_search, wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)
        
        # Scroll para garantir carregamento preguiçoso (lazy load) de fotos e reviews
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)

        try:
            print("[DEBUG] [Search] Complementando Horários...")
            for tb in await page.locator('table').all():
                text = await tb.inner_text()
                if 'segunda-feira' in text.lower() or 'fechado' in text.lower() or 'aberto' in text.lower():
                    for row in await tb.locator('tr').all():
                        cols = await row.locator('td').all()
                        if len(cols) >= 2:
                            dia = await cols[0].inner_text()
                            horario = await cols[1].inner_text()
                            all_horarios.add(f"{dia}: {horario.replace(chr(10), ' ').strip()}")
        except Exception as e: print(f"[DEBUG] Erro Horários Search: {e}")

        try:
            print("[DEBUG] [Search] Complementando Avaliações...")
            # Pega as avaliações que aparecem no painel direito (Knowledge Panel)
            review_divs = await page.locator('div:has-text("Comentários do Google")').locator('xpath=following-sibling::div').all()
            for idx, rev in enumerate(review_divs):
                try:
                    text = await rev.inner_text()
                    if text and len(text) > 10 and "comentários do Google" not in text.lower():
                        all_reviews[f"Search_{idx}"] = {"autor": "Usuário Google", "nota": "5.0", "data": "Recente", "texto": text.replace('\n', ' ').strip()}
                except: pass
        except Exception as e: print(f"[DEBUG] Erro Avaliações Search: {e}")

        try:
            print("[DEBUG] [Search] Complementando Imagens...")
            for div in await page.locator('div[style*="background-image"]').all():
                bg = await div.get_attribute('style')
                if bg and 'url(' in bg and 'googleusercontent' in bg:
                    try: all_photo_urls.add(bg.split('url("')[1].split('")')[0])
                    except:
                        try: all_photo_urls.add(bg.split('url(')[1].split(')')[0])
                        except: pass
            
            # Pegar todas as imagens, incluindo base64 e data-src (lazy load)
            for img in await page.locator('img').all():
                src = await img.get_attribute('src')
                data_src = await img.get_attribute('data-src')
                for s in [src, data_src]:
                    if s and ('glsgmb' in s or 'gps-cs-s' in s or s.startswith('data:image/')):
                        all_photo_urls.add(s)
        except Exception as e: print(f"[DEBUG] Erro Imagens Search: {e}")
        
        print(f"[DEBUG] Total de URLs válidas de imagens coletadas: {len(all_photo_urls)}")

        # =======================================
        # FASE 3: MERGE E SALVAMENTO (DESDUPLICAÇÃO)
        # =======================================
        print("[DEBUG] [Merge] Processando dados e salvando arquivos únicos...")
        
        if all_about_texts:
            with open(os.path.join(paths['empresa'], "comodidades_e_servicos.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(list(all_about_texts))))
        
        if all_horarios:
            with open(os.path.join(paths['horarios'], "grade_semanal.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(list(all_horarios))))
                
        if all_reviews:
            with open(os.path.join(paths['avaliacoes'], "avaliacoes_detalhadas.json"), "w", encoding="utf-8") as f:
                json.dump(list(all_reviews.values()), f, ensure_ascii=False, indent=2)
        if all_keywords:
            with open(os.path.join(paths['avaliacoes'], "resumo_palavras_chave.txt"), "w", encoding="utf-8") as f:
                f.write(", ".join(all_keywords))

        import base64, urllib.request
        count = 0
        for url_img in list(all_photo_urls):
            if count >= 15: break
            
            # Ignorar URLs relativas (sem protocolo) - são inválidas
            if url_img.startswith('//'):
                print(f"[DEBUG] URL relativa ignorada: {url_img[:60]}")
                continue
            
            # Processa imagens base64 capturadas diretamente do HTML
            if url_img.startswith('data:image/'):
                try:
                    header, b64_data = url_img.split(',', 1)
                    img_bytes = base64.b64decode(b64_data)
                    if len(img_bytes) > 2000:
                        with open(os.path.join(paths['imagens'], f"foto_{count+1}.jpg"), "wb") as f:
                            f.write(img_bytes)
                        print(f"[DEBUG] Foto base64 {count+1} salva ({len(img_bytes)} bytes).")
                        count += 1
                    else:
                        print(f"[DEBUG] Base64 muito pequena ({len(img_bytes)} bytes), ignorada.")
                except Exception as e:
                    print(f"[DEBUG] Erro ao decodificar base64: {e}")
                continue
                
            # Normaliza URL para alta resolução
            if '/p/' in url_img or 'gps-cs-s' in url_img or '=w' in url_img or '=s' in url_img:
                high_res_url = url_img.split('=')[0] + '=s1200'
            else:
                high_res_url = url_img
                
            print(f"[DEBUG] Tentando baixar: {high_res_url[:80]}...")
            
            # Tenta 1: Playwright context.request (mesma sessão do browser)
            img_bytes = None
            try:
                resp = await context.request.get(high_res_url, timeout=8000)
                print(f"[DEBUG] Playwright status: {resp.status}, ok: {resp.ok}")
                if resp.ok:
                    img_bytes = await resp.body()
            except Exception as e:
                print(f"[DEBUG] Playwright falhou: {e}")
                
            # Tenta 2: urllib nativo como fallback
            if not img_bytes:
                try:
                    req = urllib.request.Request(high_res_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        img_bytes = r.read()
                    print(f"[DEBUG] urllib OK, {len(img_bytes)} bytes.")
                except Exception as e:
                    print(f"[DEBUG] urllib falhou: {e}")
            
            if img_bytes and len(img_bytes) > 10000:
                with open(os.path.join(paths['imagens'], f"foto_{count+1}.jpg"), "wb") as f:
                    f.write(img_bytes)
                print(f"[DEBUG] Foto {count+1} salva ({len(img_bytes)} bytes).")
                count += 1
            elif img_bytes:
                print(f"[DEBUG] Imagem muito pequena ({len(img_bytes)} bytes), ignorada.")
            
        print(f"[DEBUG] [Merge] {len(all_about_texts)} infos, {len(all_horarios)} horários, {len(all_reviews)} avaliações, {count} fotos unicas salvas.")
        await browser.close()
        
    zip_path = shutil.make_archive(target_dir, 'zip', target_dir)
    return zip_path

def extract_place_details_sync(name: str, address: str) -> str:
    # Cria um novo event loop totalmente limpo na thread do FastAPI
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(extract_place_details(name, address))
