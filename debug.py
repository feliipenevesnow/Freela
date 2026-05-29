import asyncio
from playwright.async_api import async_playwright

async def debug_maps():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        url = "https://www.google.com/maps/search/oficina+mecanica+em+Presidente+Prudente"
        print(f"Buscando: {url}")
        
        await page.goto(url, wait_until="networkidle", timeout=60000)
        
        await page.screenshot(path="screenshot.png")
        print("Screenshot salvo.")
        
        html = await page.content()
        with open("page.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("HTML salvo.")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(debug_maps())
