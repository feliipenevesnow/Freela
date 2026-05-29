import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1280, 'height': 800})
        page = await context.new_page()
        
        search_query = 'ICC+FISIOTERAPIA+Av.+Cel.+José+Soares+Marcondes,+3300'
        all_photo_urls = set()
        
        # Maps
        print("Navegando no Maps...")
        await page.goto(f'https://www.google.com/maps/search/{search_query}', wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)
        if await page.locator('.hfpxzc').count() > 0:
            await page.locator('.hfpxzc').first.click()
            await page.wait_for_timeout(3000)
        photo_btn = page.locator('button.aoYtNb, button.Dx2nRe').first
        if await photo_btn.count() > 0:
            print("Clicando no botão de fotos...")
            await photo_btn.click()
            await page.wait_for_timeout(4000)
        
        for img in await page.locator('img').all():
            src = await img.get_attribute('src')
            if src and ('gps-cs-s' in src or 'glsgmb' in src or '/p/' in src):
                all_photo_urls.add(src)
        print(f'After Maps: {len(all_photo_urls)} valid photo URLs')
        for u in list(all_photo_urls)[:3]:
            print(f'  Sample: {u[:80]}')
        
        # Search
        print("Navegando no Search...")
        await page.goto(f'https://www.google.com/search?q={search_query}', wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)
        await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        await page.wait_for_timeout(2000)
        for img in await page.locator('img').all():
            src = await img.get_attribute('src')
            if src and ('glsgmb' in src or 'gps-cs-s' in src):
                all_photo_urls.add(src)
        print(f'After Search: {len(all_photo_urls)} valid photo URLs')
        
        # Try downloading
        print("Tentando baixar imagens...")
        count = 0
        for url_img in list(all_photo_urls):
            if count >= 3: break
            if url_img.startswith('//'):
                print(f'  URL relativa ignorada: {url_img[:60]}')
                continue
            high_res = url_img.split('=')[0] + '=s1200'
            try:
                resp = await context.request.get(high_res, timeout=8000)
                print(f'Status {resp.status}: {high_res[:60]}')
                if resp.ok:
                    body = await resp.body()
                    if len(body) > 10000:
                        print(f'  Tamanho: {len(body)} bytes -> OK!')
                        count += 1
                    else:
                        print(f'  Muito pequena: {len(body)} bytes')
            except Exception as e:
                print(f'  Erro: {e}')
        
        print(f"Total baixado com sucesso: {count}")
        await browser.close()

if __name__ == '__main__':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run())
