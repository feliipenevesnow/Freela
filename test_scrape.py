import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1280, 'height': 800})
        page = await context.new_page()
        
        search_query = "ICC FISIOTERAPIA Av. Cel. José Soares Marcondes, 3300 - Jardim Bongiovani, Pres. Prudente - SP, 19050-230".replace(' ', '+')
        url_maps = f"https://www.google.com/maps/search/{search_query}"
        
        all_photo_urls = set()
        all_reviews = {}
        
        print("Going to Maps...")
        await page.goto(url_maps, wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)
        
        if await page.locator('.hfpxzc').count() > 0:
            await page.locator('.hfpxzc').first.click()
            await page.wait_for_timeout(3000)
            
        print("Maps Imagens:")
        photo_btn = page.locator('button:has-text("Ver fotos"), button.aoYtNb, button.Dx2nRe').first
        if await photo_btn.count() > 0:
            print("Found photo button, clicking...")
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
            if src: all_photo_urls.add(src)
            
        print(f"URLs from Maps: {len(all_photo_urls)}")
        
        url_search = f"https://www.google.com/search?q={search_query}"
        print("Going to Search...")
        await page.goto(url_search, wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)
        
        print("Search Imagens:")
        # Tenta extrair imagens do Search
        for img in await page.locator('img[src*="googleusercontent"]').all():
            src = await img.get_attribute('src')
            if src: all_photo_urls.add(src)
        
        # Test also the google search page generic image nodes
        for img in await page.locator('img').all():
            src = await img.get_attribute('src')
            if src and 'lh3.googleusercontent' in src:
                all_photo_urls.add(src)
                
        print(f"Total URLs from Maps+Search: {len(all_photo_urls)}")

        # Try downloading one
        if all_photo_urls:
            url_img = list(all_photo_urls)[0]
            if '/p/' in url_img or 'gps-cs-s' in url_img or '=w' in url_img:
                base_url = url_img.split('=')[0]
                high_res_url = base_url + '=s1200'
            else:
                high_res_url = url_img
            print("Downloading:", high_res_url)
            resp = await context.request.get(high_res_url, timeout=5000)
            print("Response ok:", resp.ok, "Status:", resp.status)
            if resp.ok:
                print("Size:", len(await resp.body()))
        
        await browser.close()

if __name__ == '__main__':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run())
