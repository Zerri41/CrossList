"""
CrossList Pro — Importer
Importa anúncios existentes de cada plataforma para o CrossList.
"""

import aiohttp, asyncio, json, re
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

# ── VINTED ────────────────────────────────────────────────────────────────────

async def import_vinted(email: str, password: str, cookie: str = "") -> list[dict]:
    """Importa anúncios activos do utilizador na Vinted."""
    results = []
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie
        headers["Referer"] = "https://www.vinted.pt/"

    async with aiohttp.ClientSession() as s:
        # Primeiro obter o user ID via login/session
        try:
            async with s.get("https://www.vinted.pt/api/v2/users/current",
                             headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    user = await r.json()
                    user_id = user.get("user", {}).get("id")
                    if user_id:
                        async with s.get(
                            f"https://www.vinted.pt/api/v2/users/{user_id}/items",
                            params={"page": 1, "per_page": 96},
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10)
                        ) as r2:
                            if r2.status == 200:
                                data = await r2.json()
                                for item in data.get("items", []):
                                    p = item.get("price", {})
                                    results.append(_vinted_item(item))
        except Exception as e:
            print(f"[importer:vinted] {e}")

    return results

def _vinted_item(item: dict) -> dict:
    p = item.get("price", {})
    return {
        "platform": "vinted",
        "platform_id": str(item.get("id", "")),
        "title": item.get("title", ""),
        "price": float(p.get("amount", 0)) if p else 0,
        "condition": item.get("status", ""),
        "brand": item.get("brand_title", ""),
        "description": item.get("description", ""),
        "photo": (item.get("photos") or [{}])[0].get("url", ""),
        "url": f"https://www.vinted.pt/items/{item.get('id', '')}",
        "published_at": item.get("created_at_ts", ""),
        "status": "active" if item.get("can_be_sold") else "inactive",
    }

# ── WALLAPOP ──────────────────────────────────────────────────────────────────

async def import_wallapop(session_token: str) -> list[dict]:
    """Importa anúncios activos do utilizador no Wallapop."""
    results = []
    headers = {**HEADERS, "X-Auth-Token": session_token}

    async with aiohttp.ClientSession() as s:
        try:
            # Obter user info
            async with s.get("https://api.wallapop.com/api/v3/users/me",
                             headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    user = await r.json()
                    user_id = user.get("data", {}).get("id", "")
                    # Obter items do utilizador
                    async with s.get(
                        f"https://api.wallapop.com/api/v3/users/{user_id}/items",
                        params={"status": "active"},
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as r2:
                        if r2.status == 200:
                            data = await r2.json()
                            for item in data.get("data", {}).get("items", []):
                                results.append(_wallapop_item(item))
        except Exception as e:
            print(f"[importer:wallapop] {e}")

    return results

def _wallapop_item(item: dict) -> dict:
    c = item.get("content", item)
    return {
        "platform": "wallapop",
        "platform_id": str(c.get("id", "")),
        "title": c.get("title", ""),
        "price": float(c.get("price", 0)),
        "condition": c.get("condition", ""),
        "brand": c.get("brand", ""),
        "description": c.get("description", ""),
        "photo": (c.get("images") or [{}])[0].get("urls", {}).get("medium", ""),
        "url": f"https://es.wallapop.com/item/{c.get('web_slug', c.get('id', ''))}",
        "published_at": c.get("creation_date", ""),
        "status": "active",
    }

# ── OLX (browser) ─────────────────────────────────────────────────────────────

async def import_olx(email: str, password: str, headless: bool = True) -> list[dict]:
    """Importa anúncios da conta OLX via browser."""
    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="pt-PT"
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.olx.pt/myaccount/", wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # Login se necessário
            if "login" in page.url or "myaccount" in page.url:
                try:
                    await page.fill('input[type="email"]', email)
                    await page.fill('input[type="password"]', password)
                    await page.click('button[type="submit"]')
                    await asyncio.sleep(3)
                except Exception:
                    pass

            # Navegar para os meus anúncios
            await page.goto("https://www.olx.pt/myaccount/ads/", wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # Extrair anúncios
            items = await page.evaluate("""() => {
                const cards = document.querySelectorAll('[data-cy="myads-card"], .offer-wrapper, [class*="listing"]');
                return Array.from(cards).map(c => ({
                    title: c.querySelector('h3, h4, [class*="title"]')?.innerText?.trim() || '',
                    price: c.querySelector('[class*="price"]')?.innerText?.trim() || '',
                    url: c.querySelector('a')?.href || '',
                    photo: c.querySelector('img')?.src || '',
                    status: c.querySelector('[class*="status"], [class*="badge"]')?.innerText?.trim() || 'active',
                }));
            }""")

            for item in items:
                if item.get("title"):
                    price_text = item.get("price", "0").replace("€", "").replace(".", "").replace(",", ".").strip()
                    try:
                        price = float(re.search(r"[\d.]+", price_text).group())
                    except Exception:
                        price = 0
                    results.append({
                        "platform": "olx",
                        "platform_id": "",
                        "title": item["title"],
                        "price": price,
                        "condition": "",
                        "brand": "",
                        "description": "",
                        "photo": item.get("photo", ""),
                        "url": item.get("url", ""),
                        "published_at": datetime.now().isoformat(),
                        "status": "active" if "activ" in item.get("status", "").lower() else "inactive",
                    })
        except Exception as e:
            print(f"[importer:olx] {e}")
        finally:
            await browser.close()

    return results
