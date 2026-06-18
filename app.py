"""
CrossList Pro — Servidor Principal (Cloud)
Deploy: Railway.app
"""
import asyncio, json, os, socket
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

BASE  = Path(__file__).parent
DATA  = BASE / "data";   DATA.mkdir(exist_ok=True)
STORE = BASE / "store";  STORE.mkdir(exist_ok=True)
SESS  = BASE / "sessions"; SESS.mkdir(exist_ok=True)

CREDS_FILE = DATA / "credentials.json"
PORT = int(os.environ.get("PORT", 8080))

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

feed_clients = set()
feed_items   = []
feed_running = False
publish_log  = []
monitor_cache = {"running": False, "items": [], "last_run": None}

# ── Frontend ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse((BASE / "static" / "index.html").read_text(encoding="utf-8"))

@app.get("/manifest.json")
async def manifest():
    return {"name":"CrossList Pro","short_name":"CrossList","start_url":"/",
            "display":"standalone","background_color":"#0A1628","theme_color":"#1A56DB"}

# ── Listings ──────────────────────────────────────────────────────────────────
class Listing(BaseModel):
    id: str = ""; title: str; price: float; condition: str = "good"
    description: str = ""; brand: str = ""; category: str = "Componentes mobilidade"
    location: str = "Porto"; tags: list[str] = []; images: list[str] = []
    sku: str = ""; status: str = "draft"; platforms: dict = {}; created_at: str = ""

@app.get("/api/listings")
async def get_listings():
    return [json.loads(f.read_text()) for f in sorted(STORE.glob("*.json"), reverse=True)]

@app.post("/api/listings")
async def create_listing(l: Listing):
    l.id = f"lst_{datetime.now().strftime('%Y%m%d_%H%M%S%f')[:18]}"
    l.created_at = datetime.now().isoformat()
    (STORE / f"{l.id}.json").write_text(l.model_dump_json(indent=2))
    return l

@app.put("/api/listings/{lid}")
async def update_listing(lid: str, l: Listing):
    f = STORE / f"{lid}.json"
    if not f.exists(): raise HTTPException(404)
    l.id = lid
    f.write_text(l.model_dump_json(indent=2))
    return l

@app.delete("/api/listings/{lid}")
async def delete_listing(lid: str):
    f = STORE / f"{lid}.json"
    if f.exists(): f.unlink()
    return {"ok": True}

# ── Credenciais ───────────────────────────────────────────────────────────────
@app.get("/api/credentials")
async def get_creds():
    if not CREDS_FILE.exists(): return {}
    data = json.loads(CREDS_FILE.read_text())
    return {p: {k: ("••••••••" if any(x in k for x in ["password","secret","token","key"]) else v)
                for k,v in c.items()} for p,c in data.items()}

@app.post("/api/credentials")
async def save_creds(data: dict):
    existing = json.loads(CREDS_FILE.read_text()) if CREDS_FILE.exists() else {}
    for p, cred in data.items():
        existing.setdefault(p, {})
        for k, v in cred.items():
            if v and v != "••••••••": existing[p][k] = v
    CREDS_FILE.write_text(json.dumps(existing, indent=2))
    return {"ok": True}

# ── Publisher (com Playwright headless) ───────────────────────────────────────
class PublishRequest(BaseModel):
    listing_id: str; platforms: list[str]; headless: bool = True

@app.post("/api/publish")
async def publish(req: PublishRequest):
    f = STORE / f"{req.listing_id}.json"
    if not f.exists(): raise HTTPException(404)
    listing_data = json.loads(f.read_text())
    job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    log_entry = {"id": job_id, "listing": listing_data["title"],
                 "platforms": req.platforms, "status": "running",
                 "started": datetime.now().isoformat(), "results": {}}
    publish_log.insert(0, log_entry)
    asyncio.create_task(_run_publish(req, listing_data, log_entry))
    return {"job_id": job_id, "status": "started"}

async def _run_publish(req: PublishRequest, listing_data: dict, log_entry: dict):
    creds = json.loads(CREDS_FILE.read_text()) if CREDS_FILE.exists() else {}
    from playwright.async_api import async_playwright
    import random, re

    for platform in req.platforms:
        plat_creds = creds.get(platform, {})
        email    = plat_creds.get("email", "")
        password = plat_creds.get("password", "")
        if not email or not password:
            log_entry["results"][platform] = {"success": False, "error": "Credenciais em falta — configurar em Definições"}
            continue

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox","--disable-setuid-sandbox",
                          "--disable-blink-features=AutomationControlled",
                          "--disable-dev-shm-usage"]
                )
                ctx = await browser.new_context(
                    viewport={"width":1280,"height":900},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                    locale="pt-PT"
                )
                # Tentar restaurar sessão
                sess_file = SESS / f"{platform}_session.json"
                if sess_file.exists():
                    await ctx.add_cookies(json.loads(sess_file.read_text()))

                page = await ctx.new_page()
                await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

                url = None
                if platform == "vinted":
                    url = await _publish_vinted(page, listing_data, email, password, ctx)
                elif platform == "wallapop":
                    url = await _publish_wallapop(page, listing_data, email, password)
                elif platform == "segunda":
                    url = await _publish_segunda(page, listing_data, email, password)

                # Guardar sessão
                cookies = await ctx.cookies()
                sess_file.write_text(json.dumps(cookies))
                await browser.close()

                log_entry["results"][platform] = {"success": bool(url), "url": url or "publicado"}

        except Exception as e:
            log_entry["results"][platform] = {"success": False, "error": str(e)[:200]}

    log_entry["status"] = "done"
    log_entry["finished"] = datetime.now().isoformat()

    # Actualizar listing
    f = STORE / f"{listing_data.get('id','')}.json"
    if f.exists():
        lst = json.loads(f.read_text())
        lst["status"] = "active"
        for p, res in log_entry["results"].items():
            if res.get("success"): lst["platforms"][p] = res.get("url","publicado")
        f.write_text(json.dumps(lst, indent=2))

async def _human_type(page, sel, text):
    import random
    await page.click(sel); await page.fill(sel, "")
    for c in text:
        await page.type(sel, c, delay=random.randint(40,100))
    await asyncio.sleep(random.uniform(0.3, 0.7))

async def _publish_vinted(page, l, email, password, ctx):
    import re
    await page.goto("https://www.vinted.pt/login", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    try: await page.click('[data-testid="consent-accept-all"]', timeout=3000)
    except: pass
    if "login" in page.url:
        await _human_type(page, '[data-testid="username"]', email)
        await _human_type(page, '[data-testid="password"]', password)
        await page.click('[data-testid="login-submit"]')
        await asyncio.sleep(3)
    await page.goto("https://www.vinted.pt/member/new_item", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    try: await _human_type(page, '[data-testid="item-title-input"]', l["title"])
    except: pass
    try: await _human_type(page, '[data-testid="item-description-input"]', l.get("description",""))
    except: pass
    try: await _human_type(page, '[data-testid="item-price-input"]', str(l["price"]))
    except: pass
    await asyncio.sleep(1)
    try:
        await page.click('[data-testid="submit-item-button"]')
        await page.wait_for_url(re.compile(r"/items/\d+"), timeout=15000)
        return page.url
    except: return None

async def _publish_wallapop(page, l, email, password):
    await page.goto("https://es.wallapop.com/login", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    try: await page.click('[id*="onetrust-accept"]', timeout=3000)
    except: pass
    try:
        await _human_type(page, 'input[type="email"]', email)
        await page.click('button[type="submit"]'); await asyncio.sleep(1)
        await _human_type(page, 'input[type="password"]', password)
        await page.click('button[type="submit"]'); await asyncio.sleep(3)
    except: pass
    await page.goto("https://es.wallapop.com/upload", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    try: await _human_type(page, 'input[name="title"]', l["title"])
    except: pass
    try: await _human_type(page, 'textarea[name="description"]', l.get("description",""))
    except: pass
    try: await _human_type(page, 'input[name="salePrice"]', str(l["price"]))
    except: pass
    await asyncio.sleep(1)
    try:
        await page.click('[data-testid="upload-submit"]')
        await asyncio.sleep(4)
        return page.url if "/item/" in page.url else None
    except: return None

async def _publish_segunda(page, l, email, password):
    await page.goto("https://www.segundamao.pt/anunciar", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    for sel in ['input[name="email"]','#email']:
        try: await _human_type(page, sel, email); break
        except: pass
    for sel in ['input[name="password"]','#password']:
        try: await _human_type(page, sel, password); break
        except: pass
    try: await page.click('button[type="submit"]'); await asyncio.sleep(2)
    except: pass
    for sel in ['#titulo','input[name="titulo"]']:
        try: await _human_type(page, sel, l["title"]); break
        except: pass
    for sel in ['#preco','input[name="preco"]']:
        try: await _human_type(page, sel, str(l["price"])); break
        except: pass
    await asyncio.sleep(1)
    try:
        await page.click('button[type="submit"]'); await asyncio.sleep(3)
        return page.url if "anuncio" in page.url else None
    except: return None

@app.get("/api/publish/jobs")
async def get_jobs(): return publish_log

# ── Monitor ───────────────────────────────────────────────────────────────────
class MonitorRequest(BaseModel):
    keywords: list[str]; max_price: float = None; domain: str = "www.vinted.pt"

@app.post("/api/monitor/run")
async def run_monitor(req: MonitorRequest):
    if monitor_cache["running"]: return {"status":"already_running"}
    asyncio.create_task(_run_monitor(req))
    return {"status":"started"}

async def _run_monitor(req: MonitorRequest):
    import aiohttp
    monitor_cache["running"] = True; monitor_cache["items"] = []
    creds = json.loads(CREDS_FILE.read_text()) if CREDS_FILE.exists() else {}
    headers = {"User-Agent":"Mozilla/5.0","Accept":"application/json","Referer":f"https://{req.domain}/"}
    cookie = creds.get("vinted",{}).get("session_cookie","")
    if cookie: headers["Cookie"] = cookie
    async with aiohttp.ClientSession() as s:
        for kw in req.keywords:
            params = {"search_text":kw,"per_page":96,"order":"newest_first"}
            if req.max_price: params["price_to"] = str(req.max_price)
            try:
                async with s.get(f"https://{req.domain}/api/v2/catalog/items",
                                  params=params, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        for item in data.get("items",[]):
                            p = item.get("price",{})
                            monitor_cache["items"].append({
                                "keyword":kw,"id":item.get("id"),"title":item.get("title",""),
                                "price":float(p.get("amount",0)) if p else 0,
                                "brand":item.get("brand_title",""),"condition":item.get("status",""),
                                "country":item.get("country",{}).get("title",""),
                                "seller":item.get("user",{}).get("login",""),
                                "photo":(item.get("photos") or [{}])[0].get("url",""),
                                "url":f"https://{req.domain}/items/{item.get('id','')}",
                            })
            except: pass
            await asyncio.sleep(2)
    monitor_cache["running"] = False
    monitor_cache["last_run"] = datetime.now().isoformat()

@app.get("/api/monitor/results")
async def get_monitor(): return monitor_cache

# ── Feed SSE ──────────────────────────────────────────────────────────────────
class FeedRequest(BaseModel):
    keywords: list[str]; max_price: float = None; interval: float = 2.0

@app.post("/api/feed/start")
async def start_feed(req: FeedRequest):
    global feed_running, feed_items
    feed_running = False; await asyncio.sleep(0.5)
    feed_items = []; feed_running = True
    asyncio.create_task(_feed_loop(req))
    return {"status":"started"}

@app.post("/api/feed/stop")
async def stop_feed():
    global feed_running; feed_running = False
    return {"status":"stopped"}

@app.get("/api/feed/events")
async def feed_events():
    async def gen():
        q = asyncio.Queue(); feed_clients.add(q)
        for item in feed_items[-30:]:
            yield f"data: {json.dumps({'type':'existing','item':item},ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield 'data: {"type":"ping"}\n\n'
        finally:
            feed_clients.discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                              headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

async def _bcast(data):
    msg = json.dumps(data, ensure_ascii=False)
    dead = set()
    for q in list(feed_clients):
        try: await q.put(msg)
        except: dead.add(q)
    feed_clients.difference_update(dead)

async def _feed_loop(req: FeedRequest):
    import aiohttp, time
    creds = json.loads(CREDS_FILE.read_text()) if CREDS_FILE.exists() else {}
    headers = {"User-Agent":"Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15 Mobile/15E148",
               "Accept":"application/json","Referer":"https://www.vinted.pt/"}
    cookie = creds.get("vinted",{}).get("session_cookie","")
    if cookie: headers["Cookie"] = cookie
    max_ids = {}
    async with aiohttp.ClientSession() as s:
        for kw in req.keywords:
            params = {"search_text":kw,"per_page":96,"order":"newest_first"}
            if req.max_price: params["price_to"] = str(req.max_price)
            try:
                async with s.get("https://www.vinted.pt/api/v2/catalog/items",
                                  params=params,headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=6)) as r:
                    if r.status==200:
                        d = await r.json()
                        ids = [i.get("id",0) for i in d.get("items",[])]
                        max_ids[kw] = max(ids) if ids else 0
            except: max_ids[kw] = 0
            await asyncio.sleep(0.5)
        await _bcast({"type":"ready","keywords":req.keywords})
        while feed_running:
            t0 = time.time()
            for kw in req.keywords:
                if not feed_running: break
                params = {"search_text":kw,"per_page":96,"order":"newest_first"}
                if req.max_price: params["price_to"] = str(req.max_price)
                try:
                    async with s.get("https://www.vinted.pt/api/v2/catalog/items",
                                      params=params,headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=6)) as r:
                        if r.status==200:
                            d = await r.json()
                            known = max_ids.get(kw,0)
                            new = [i for i in d.get("items",[]) if i.get("id",0)>known]
                            if new:
                                max_ids[kw] = max(i.get("id",0) for i in new)
                                for raw in reversed(new):
                                    p = raw.get("price",{})
                                    item = {"id":raw.get("id"),"title":raw.get("title",""),
                                            "price":float(p.get("amount",0)) if p else 0,
                                            "brand":raw.get("brand_title",""),
                                            "condition":raw.get("status",""),
                                            "seller":raw.get("user",{}).get("login",""),
                                            "photo":(raw.get("photos") or [{}])[0].get("url",""),
                                            "url":f"https://www.vinted.pt/items/{raw.get('id','')}",
                                            "keyword":kw,
                                            "seen_at":datetime.now().strftime("%H:%M:%S")}
                                    feed_items.insert(0,item)
                                    if len(feed_items)>300: feed_items.pop()
                                    await _bcast({"type":"new_item","item":item})
                except: pass
                await asyncio.sleep(0.3)
            await asyncio.sleep(max(0.1, req.interval-(time.time()-t0)))

# ── Status ────────────────────────────────────────────────────────────────────
@app.get("/api/status")
async def status():
    return {"ok":True,"version":"1.0","feed_running":feed_running,
            "feed_items":len(feed_items),"listings":len(list(STORE.glob("*.json"))),
            "network_url":"(cloud — acesso directo pelo URL)","time":datetime.now().isoformat()}

if __name__ == "__main__":
    print(f"\n  CrossList Pro — Porta {PORT}\n")
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)

# ── AI Photo Analysis ─────────────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    image_b64: str
    media_type: str = "image/jpeg"

@app.post("/api/ai/analyze")
async def analyze_photo(req: AnalyzeRequest):
    try:
        import anthropic, base64, json, re, os
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise HTTPException(400, "ANTHROPIC_API_KEY não configurada no Render")
        client = anthropic.Anthropic(api_key=api_key)
        img_bytes = base64.b64decode(req.image_b64)
        b64 = base64.standard_b64encode(img_bytes).decode()
        PROMPT = """És um vendedor experiente em marketplaces portugueses. Analisa esta foto e cria uma listagem completa.
Responde APENAS em JSON válido sem markdown.
{"titulo":"título curto e directo (max 60 chars)","marca":"marca do produto","categoria":"Componentes mobilidade","condicao":"good","descricao_vinted":"desc casual 120-150 chars com 1-2 emojis","descricao_olx":"desc profissional 150-200 chars","descricao_wallapop":"desc directa 100-130 chars","descricao_ebay":"desc técnica em inglês 200-250 chars","descricao_geral":"desc completa 200-300 chars","tags":["tag1","tag2","tag3","tag4","tag5"],"hook":"frase apelativa de vendedor experiente max 80 chars","notas_vendedor":"2-3 dicas rápidas para vender melhor","condicao_visual":"observação sobre o estado na foto"}"""
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":req.media_type,"data":b64}},
                {"type":"text","text":PROMPT}
            ]}]
        )
        raw = msg.content[0].text
        clean = re.sub(r"```json|```","",raw).strip()
        return json.loads(clean)

# ════════════════════════════════════════════════════════
# SYNC & IMPORT ENDPOINTS
# ════════════════════════════════════════════════════════
import sync_manager, importer as imp_module
from contextlib import asynccontextmanager

# Inicializar sync_manager com referências ao estado do app
def _init_sync():
    async def _publish_wrapper(listing, platforms, creds):
        for plat in platforms:
            plat_creds = creds.get(plat, {})
            if not plat_creds.get("email"):
                continue
            try:
                from playwright.async_api import async_playwright
                import sys
                script = BASE / "scripts" if (BASE/"scripts").exists() else BASE
                # usar publishers existentes
            except Exception as e:
                print(f"[sync publish] {plat}: {e}")

    sync_manager.init(
        store=STORE, creds=CREDS_FILE, sessions=SESS,
        broadcast=_bcast, publish=_publish_wrapper
    )

_init_sync()

# Arrancar scheduler ao iniciar
@app.on_event("startup")
async def on_startup():
    await sync_manager.start_scheduler(interval_hours=2)

# ── Sync manual ───────────────────────────────────────────────────────────────
@app.post("/api/sync/run")
async def sync_run():
    if sync_manager.sync_running:
        return {"status": "already_running"}
    asyncio.create_task(sync_manager.run_sync_cycle())
    return {"status": "started"}

@app.get("/api/sync/status")
async def sync_status():
    listings = [json.loads(f.read_text()) for f in STORE.glob("*.json")]
    active  = [l for l in listings if l.get("status") == "active"]
    sold    = [l for l in listings if l.get("status") == "sold"]

    # Detectar anúncios próximos de expirar em TODAS as plataformas
    EXPIRY = {"olx": 30, "custojusto": 60, "segunda": 90, "ebay": 10}
    olx_warnings = []
    for l in active:
        for plat_name, expiry_days in EXPIRY.items():
            plat_data = l.get("platforms", {}).get(plat_name)
            if not plat_data:
                continue
            pub = plat_data.get("published_at") if isinstance(plat_data, dict) else None
            if pub:
                try:
                    age = (datetime.now() - datetime.fromisoformat(pub)).days
                    warn_at = expiry_days - 5
                    if age >= warn_at:
                        olx_warnings.append({
                            "id": l["id"], "title": l["title"],
                            "platform": plat_name,
                            "expires_in": expiry_days - age
                        })
                except Exception:
                    pass

    return {
        "running":      sync_manager.sync_running,
        "active":       len(active),
        "sold":         len(sold),
        "olx_warnings": olx_warnings,
        "last_log":     sync_manager.sync_log[0] if sync_manager.sync_log else None,
        "next_auto":    f"de {sync_manager._sync_interval_hours}h em {sync_manager._sync_interval_hours}h (automático)",
    }

@app.get("/api/sync/log")
async def sync_log():
    return sync_manager.sync_log

# ── Import de plataformas ─────────────────────────────────────────────────────
@app.post("/api/import/{platform}")
async def import_platform(platform: str):
    creds = json.loads(CREDS_FILE.read_text()) if CREDS_FILE.exists() else {}
    plat_creds = creds.get(platform, {})

    asyncio.create_task(_run_import(platform, plat_creds))
    return {"status": "started", "platform": platform}

import_results: dict = {}

async def _run_import(platform: str, creds: dict):
    import_results[platform] = {"status": "running", "items": [], "started": datetime.now().isoformat()}
    items = []

    try:
        if platform == "vinted":
            cookie = creds.get("session_cookie", "")
            items = await imp_module.import_vinted(
                email=creds.get("email", ""),
                password=creds.get("password", ""),
                cookie=cookie
            )
        elif platform == "wallapop":
            token = creds.get("auth_token", "")
            items = await imp_module.import_wallapop(token)
        elif platform == "olx":
            items = await imp_module.import_olx(
                email=creds.get("email", creds.get("client_id", "")),
                password=creds.get("password", creds.get("client_secret", ""))
            )
    except Exception as e:
        import_results[platform] = {"status": "error", "error": str(e), "items": []}
        return

    # Guardar itens importados que não existam ainda
    existing_urls = set()
    for f in STORE.glob("*.json"):
        try:
            l = json.loads(f.read_text())
            for p, v in l.get("platforms", {}).items():
                url = v if isinstance(v, str) else v.get("url", "")
                if url:
                    existing_urls.add(url)
        except Exception:
            pass

    imported = 0
    for item in items:
        if item.get("url") in existing_urls:
            continue
        lid = f"lst_{datetime.now().strftime('%Y%m%d_%H%M%S%f')[:18]}_{imported}"
        listing = {
            "id": lid,
            "title": item["title"],
            "price": item["price"],
            "condition": item.get("condition", "good"),
            "description": item.get("description", ""),
            "brand": item.get("brand", ""),
            "category": "Importado",
            "location": "Portugal",
            "tags": [],
            "images": [item["photo"]] if item.get("photo") else [],
            "sku": "",
            "status": "active",
            "created_at": datetime.now().isoformat(),
            "imported_from": platform,
            "platforms": {
                platform: {
                    "url": item["url"],
                    "platform_id": item.get("platform_id", ""),
                    "published_at": item.get("published_at", datetime.now().isoformat()),
                    "status": item.get("status", "active"),
                }
            }
        }
        (STORE / f"{lid}.json").write_text(json.dumps(listing, indent=2, ensure_ascii=False))
        imported += 1
        await asyncio.sleep(0.05)

    import_results[platform] = {
        "status": "done",
        "found": len(items),
        "imported": imported,
        "skipped": len(items) - imported,
        "items": items[:20],
        "finished": datetime.now().isoformat()
    }

    await _bcast({"type": "import_done", "platform": platform,
                  "imported": imported, "found": len(items),
                  "message": f"✅ {platform}: {imported} anúncios importados ({len(items)-imported} já existiam)"})

@app.get("/api/import/results/{platform}")
async def get_import_results(platform: str):
    return import_results.get(platform, {"status": "not_started"})

# ── Notifications feed ────────────────────────────────────────────────────────
notifications: list[dict] = []

_original_bcast = _bcast  # type: ignore  # já definido acima

async def _bcast_with_log(data: dict):
    # Guardar notificações relevantes
    if data.get("type") in ("item_sold", "item_sold_done", "olx_warning",
                             "olx_relist", "item_removed", "sync_done", "import_done"):
        notifications.insert(0, {**data, "at": datetime.now().strftime("%H:%M %d/%m")})
        if len(notifications) > 50:
            notifications.pop()
    await _bcast(data)

# Redirecionar sync_manager para usar versão com log
sync_manager._broadcast_fn = _bcast_with_log

@app.get("/api/notifications")
async def get_notifications():
    return notifications

# ════════════════════════════════════════════════════════
# FOLDER SCANNER (iCloud / pasta local)
# ════════════════════════════════════════════════════════
import folder_scanner as fs

# Guardar config da pasta
folder_config_file = DATA / "folder_config.json"

def _load_folder_config() -> dict:
    if folder_config_file.exists():
        return json.loads(folder_config_file.read_text())
    return {"path": "", "mode": "auto"}

def _save_folder_config(cfg: dict):
    folder_config_file.write_text(json.dumps(cfg, indent=2))

@app.get("/api/folder/config")
async def get_folder_config():
    return _load_folder_config()

@app.post("/api/folder/config")
async def save_folder_config(data: dict):
    cfg = _load_folder_config()
    cfg.update({k: v for k, v in data.items() if k in ("path", "mode", "photos_per_product")})
    _save_folder_config(cfg)
    return cfg

class ScanRequest(BaseModel):
    path: str = ""
    mode: str = "batch"
    photos_per_product: int = 4

scan_state = {"running": False, "result": None}

@app.post("/api/folder/scan")
async def scan_folder(req: ScanRequest):
    cfg = _load_folder_config()
    path = req.path or cfg.get("path", "")
    mode = req.mode or cfg.get("mode", "auto")
    if not path:
        raise HTTPException(400, "Caminho da pasta não configurado")
    if scan_state["running"]:
        return {"status": "already_running"}
    ppp = req.photos_per_product
    asyncio.create_task(_run_scan(path, mode, ppp))
    return {"status": "started"}

async def _run_scan(path: str, mode: str, photos_per_product: int = 4):
    scan_state["running"] = True
    try:
        # Recolher folders já importados
        existing = set()
        for f in STORE.glob("*.json"):
            try:
                lst = json.loads(f.read_text())
                if lst.get("folder_path"):
                    existing.add(lst["folder_path"])
            except Exception:
                pass

        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: fs.scan_to_listings(path, mode, photos_per_product, existing)
        )
        scan_state["result"] = result
        await _bcast({"type": "scan_done",
                      "found": result["found"],
                      "new": result["new"],
                      "message": f"Scan concluído: {result['found']} produtos encontrados, {result['new']} novos"})
    except Exception as e:
        scan_state["result"] = {"error": str(e)}
        await _bcast({"type": "scan_error", "message": f"Erro no scan: {e}"})
    finally:
        scan_state["running"] = False

@app.get("/api/folder/scan/result")
async def get_scan_result():
    return {"running": scan_state["running"], "result": scan_state["result"]}

class ImportFolderItem(BaseModel):
    title: str
    images: list[str]
    folder: str

@app.post("/api/folder/import")
async def import_folder_items(items: list[ImportFolderItem]):
    """Importa os produtos seleccionados do scan como rascunhos."""
    imported = 0
    for item in items:
        lid = f"lst_{datetime.now().strftime('%Y%m%d_%H%M%S%f')[:19]}_{imported}"
        # Converter paths locais em base64 para exibir na UI
        previews = []
        for img_path in item.images[:8]:
            try:
                import base64, mimetypes
                p = Path(img_path)
                if p.exists():
                    mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
                    b64 = base64.b64encode(p.read_bytes()).decode()
                    previews.append(f"data:{mime};base64,{b64}")
            except Exception:
                pass

        listing = {
            "id": lid,
            "title": item.title,
            "price": 0,
            "condition": "good",
            "description": "",
            "brand": "",
            "category": "Importado",
            "location": "Porto",
            "tags": [],
            "images": previews or [],
            "image_paths": item.images,  # paths originais
            "folder_path": item.folder,
            "sku": "",
            "status": "draft",
            "created_at": datetime.now().isoformat(),
            "imported_from": "folder_scan",
            "platforms": {},
            "ai_hook": ""
        }
        (STORE / f"{lid}.json").write_text(
            json.dumps(listing, indent=2, ensure_ascii=False))
        imported += 1
        await asyncio.sleep(0.02)

    await _bcast({"type": "folder_import_done",
                  "imported": imported,
                  "message": f"✅ {imported} produto(s) importado(s) da pasta"})
    return {"imported": imported}

@app.get("/api/folder/image")
async def serve_local_image(path: str):
    """Serve uma imagem local pelo path."""
    from fastapi.responses import FileResponse
    import urllib.parse
    decoded = urllib.parse.unquote(path)
    img_path = Path(decoded)
    if not img_path.exists() or img_path.suffix.lower() not in {
        ".jpg",".jpeg",".png",".webp",".heic",".heif",".avif"
    }:
        raise HTTPException(404)
    return FileResponse(str(img_path))

# ════════════════════════════════════════════════════════
# GALLERY — lista todas as fotos da pasta
# ════════════════════════════════════════════════════════

@app.get("/api/gallery/list")
async def gallery_list(folder: str = ""):
    cfg = _load_folder_config()
    path = folder or cfg.get("path", "")
    if not path:
        return {"images": [], "error": "Pasta não configurada"}
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return {"images": [], "error": f"Pasta não encontrada: {path}"}
    exts = {".jpg",".jpeg",".png",".webp",".heic",".heif",".avif"}
    images = []
    for f in sorted(root.iterdir()):
        if f.is_file() and f.suffix.lower() in exts:
            images.append({"name": f.name, "path": str(f)})
    return {"images": images, "total": len(images), "folder": str(root)}

class GalleryProduct(BaseModel):
    title: str = ""
    image_paths: list[str]

@app.post("/api/gallery/create")
async def gallery_create(item: GalleryProduct):
    """Cria um rascunho a partir de fotos seleccionadas na galeria."""
    import base64, mimetypes as mt
    previews = []
    for p in item.image_paths[:10]:
        try:
            fp = Path(p)
            if fp.exists():
                mime = mt.guess_type(str(fp))[0] or "image/jpeg"
                b64 = base64.b64encode(fp.read_bytes()).decode()
                previews.append(f"data:{mime};base64,{b64}")
        except Exception:
            pass
    lid = f"lst_{datetime.now().strftime('%Y%m%d_%H%M%S%f')[:19]}"
    title = item.title or Path(item.image_paths[0]).stem.replace("_"," ").title() if item.image_paths else "Produto"
    listing = {
        "id": lid, "title": title, "price": 0,
        "condition": "good", "description": "", "brand": "",
        "category": "Importado", "location": "Porto",
        "tags": [], "images": previews, "image_paths": item.image_paths,
        "sku": "", "status": "draft", "created_at": datetime.now().isoformat(),
        "imported_from": "gallery", "platforms": {}, "ai_hook": ""
    }
    (STORE / f"{lid}.json").write_text(json.dumps(listing, indent=2, ensure_ascii=False))
    return {"id": lid, "title": title, "images": len(previews)}

# ════════════════════════════════════════════════════════
# GOOGLE DRIVE SYNC
# ════════════════════════════════════════════════════════
import drive_sync as ds

DRIVE_CONFIG_FILE = DATA / "drive_config.json"
DRIVE_KEY_FILE    = DATA / "drive_service_key.json"

def _load_drive_config() -> dict:
    if DRIVE_CONFIG_FILE.exists():
        return json.loads(DRIVE_CONFIG_FILE.read_text())
    return {"folder_id": "", "folder_name": "", "enabled": False}

def _save_drive_config(cfg: dict):
    DRIVE_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

async def _on_new_drive_file(file_id: str, name: str, thumbnail: str, url: str):
    """Callback: novo ficheiro detectado no Drive → notificar galeria."""
    await _bcast({"type": "drive_added", "file": {
        "id": file_id, "name": name,
        "thumbnail": thumbnail, "url": url
    }, "message": f"📁 Nova foto no Drive: {name}"})
    notifications.insert(0, {
        "type": "drive_added",
        "message": f"📁 Nova foto detectada no Drive: {name}",
        "at": datetime.now().strftime("%H:%M %d/%m")
    })

async def _on_del_drive_file(file_id: str):
    """Callback: ficheiro removido do Drive → remover produto associado."""
    for f in STORE.glob("*.json"):
        try:
            lst = json.loads(f.read_text())
            drive_id = lst.get("drive_file_id")
            if drive_id == file_id and lst.get("status") != "sold":
                lst["status"] = "drive_removed"
                f.write_text(json.dumps(lst, indent=2))
                await _bcast({"type": "drive_removed",
                              "listing_id": lst["id"],
                              "message": f"🗑 '{lst['title']}' removido do Drive"})
        except Exception:
            pass

# Inicializar Drive se configurado
async def _init_drive():
    cfg = _load_drive_config()
    if cfg.get("enabled") and DRIVE_KEY_FILE.exists():
        try:
            key = json.loads(DRIVE_KEY_FILE.read_text())
            ds.init(key, cfg["folder_id"], _bcast,
                    _on_new_drive_file, _on_del_drive_file)
            await ds.full_sync()
            await ds.start_polling(interval_seconds=30)
            print("[drive] Sync iniciado")
        except Exception as e:
            print(f"[drive] Erro ao iniciar: {e}")

@app.on_event("startup")
async def on_startup_drive():
    await asyncio.sleep(2)  # esperar app estar pronta
    await _init_drive()

# ── Endpoints Drive ───────────────────────────────────────────────────────────

@app.get("/api/drive/status")
async def drive_status():
    cfg = _load_drive_config()
    ready = ds.is_ready()
    files = ds.get_known_files() if ready else []
    return {
        "configured": DRIVE_KEY_FILE.exists(),
        "enabled": cfg.get("enabled", False),
        "connected": ready,
        "folder_id": cfg.get("folder_id", ""),
        "folder_name": cfg.get("folder_name", ""),
        "total_files": len(files),
        "poll_interval": ds._poll_interval if ready else 30,
    }

@app.post("/api/drive/configure")
async def configure_drive(data: dict):
    """Configura: recebe JSON da Service Account + folder_id."""
    service_key = data.get("service_key")
    folder_id   = data.get("folder_id", "").strip()
    folder_name = data.get("folder_name", "Inventario")
    if not service_key or not folder_id:
        raise HTTPException(400, "service_key e folder_id obrigatórios")
    DRIVE_KEY_FILE.write_text(json.dumps(service_key, indent=2))
    cfg = {"folder_id": folder_id, "folder_name": folder_name, "enabled": True}
    _save_drive_config(cfg)
    try:
        ds.init(service_key, folder_id, _bcast,
                _on_new_drive_file, _on_del_drive_file)
        result = await ds.full_sync()
        await ds.start_polling(30)
        return {"ok": True, "files_found": result.get("total", 0)}
    except Exception as e:
        raise HTTPException(400, f"Erro ao ligar ao Drive: {e}")

@app.get("/api/drive/files")
async def drive_files():
    if not ds.is_ready():
        cfg = _load_drive_config()
        if cfg.get("enabled") and DRIVE_KEY_FILE.exists():
            await _init_drive()
        else:
            return {"files": [], "error": "Drive não configurado"}
    return {"files": ds.get_known_files(), "total": len(ds.get_known_files())}

@app.post("/api/drive/sync")
async def manual_sync():
    if not ds.is_ready():
        raise HTTPException(400, "Drive não configurado")
    result = await ds.full_sync()
    return result

class DriveProduct(BaseModel):
    title: str = ""
    file_ids: list[str]

@app.post("/api/drive/create")
async def create_from_drive(item: DriveProduct):
    """Cria produto a partir de ficheiros seleccionados no Drive."""
    if not ds.is_ready():
        raise HTTPException(400, "Drive não configurado")
    files_meta = ds.get_known_files()
    meta_map   = {f["id"]: f for f in files_meta}
    images     = []
    for fid in item.file_ids[:8]:
        meta = meta_map.get(fid)
        if meta:
            images.append(meta["thumbnail"])
    lid = f"lst_{datetime.now().strftime('%Y%m%d_%H%M%S%f')[:19]}"
    title = item.title or f"Produto {len(list(STORE.glob('*.json')))+1}"
    listing = {
        "id": lid, "title": title, "price": 0,
        "condition": "good", "description": "", "brand": "",
        "category": "Importado", "location": "Porto",
        "tags": [], "images": images,
        "drive_file_ids": item.file_ids,
        "drive_file_id": item.file_ids[0] if item.file_ids else "",
        "sku": "", "status": "draft",
        "created_at": datetime.now().isoformat(),
        "imported_from": "google_drive", "platforms": {}, "ai_hook": ""
    }
    (STORE / f"{lid}.json").write_text(json.dumps(listing, indent=2, ensure_ascii=False))
    return {"id": lid, "title": title, "images": len(images)}

@app.delete("/api/drive/archive/{file_id}")
async def archive_drive_file(file_id: str):
    """Move ficheiro para pasta Arquivado no Drive."""
    if not ds.is_ready():
        raise HTTPException(400, "Drive não configurado")
    ok = await asyncio.get_event_loop().run_in_executor(
        None, lambda: ds.move_to_archived(file_id))
    return {"ok": ok}
