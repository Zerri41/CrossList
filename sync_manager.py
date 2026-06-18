"""
CrossList Pro — Sync Manager
Verifica estado de todos os anúncios activos e executa:
  • Detecção de vendido → remove de todas as outras plataformas
  • OLX expiry (30 dias) → republica automaticamente
  • Alerta de anúncios expirados noutras plataformas
"""

import asyncio, aiohttp, json, re
from datetime import datetime, timedelta
from pathlib import Path

# Injectado pelo app.py
_store_path: Path = None
_creds_path: Path = None
_sessions_path: Path = None
_broadcast_fn = None   # SSE broadcast
_publish_fn   = None   # publisher

def init(store: Path, creds: Path, sessions: Path, broadcast, publish):
    global _store_path, _creds_path, _sessions_path, _broadcast_fn, _publish_fn
    _store_path    = store
    _creds_path    = creds
    _sessions_path = sessions
    _broadcast_fn  = broadcast
    _publish_fn    = publish

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_listings() -> list[dict]:
    if not _store_path:
        return []
    return [json.loads(f.read_text()) for f in sorted(_store_path.glob("*.json"), reverse=True)]

def _save_listing(lst: dict):
    (_store_path / f"{lst['id']}.json").write_text(json.dumps(lst, indent=2, ensure_ascii=False))

def _load_creds() -> dict:
    if _creds_path and _creds_path.exists():
        return json.loads(_creds_path.read_text())
    return {}

async def _notify(event_type: str, data: dict):
    if _broadcast_fn:
        await _broadcast_fn({"type": event_type, **data})

# ── Sold / status detection ───────────────────────────────────────────────────

SOLD_PATTERNS = {
    # Verificar em TODAS as 7 plataformas
    "vinted":      [r"can_be_sold.*false", r"item-sold", r"vendido", r"reservado",
                    r"this item is no longer available"],
    "olx":         [r"vendido", r"expirado", r"não encontrado", r"advert.*deleted",
                    r"this advert is no longer available"],
    "wallapop":    [r'"status"\s*:\s*"sold"', r"vendido", r"item-not-found",
                    r"reservado", r"ya no está disponible"],
    "ebay":        [r"this listing.*ended", r"item.*sold", r"listing has ended",
                    r"no longer available", r"sold for"],
    "custojusto":  [r"vendido", r"expirado", r"anúncio.*removido",
                    r"este anúncio não está disponível"],
    "segunda":     [r"vendido", r"removido", r"anúncio.*não.*disponível",
                    r"este anúncio foi removido"],
    "facebook":    [r"availability.*sold", r"vendido", r"marketplace.*unavailable",
                    r"this item has been sold"],
}

# Expiração por plataforma (dias) — cada plataforma tem a sua política
PLATFORM_EXPIRY_DAYS = {
    "olx":        30,   # apaga ao fim de 30 dias
    "custojusto": 60,   # apaga ao fim de 60 dias
    "segunda":    90,   # mais tolerante
    "wallapop":   None, # não expira automaticamente
    "vinted":     None, # não expira
    "ebay":       10,   # listings de leilão expiram, BIN podem durar mais
    "facebook":   None, # não expira
}

PLATFORM_WARN_DAYS = {k: max(v-5, 1) if v else None for k,v in PLATFORM_EXPIRY_DAYS.items() if v}
PLATFORM_RELIST_DAYS = {k: v-2 if v else None for k,v in PLATFORM_EXPIRY_DAYS.items() if v}

async def check_listing_status(session: aiohttp.ClientSession,
                                url: str, platform: str) -> str:
    """Verifica se um anúncio ainda está activo, vendido ou expirado."""
    if not url or not url.startswith("http"):
        return "unknown"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               allow_redirects=True) as r:
            if r.status == 404:
                return "removed"
            if r.status == 200:
                text = (await r.text()).lower()
                patterns = SOLD_PATTERNS.get(platform, [])
                for pat in patterns:
                    if re.search(pat, text):
                        return "sold"
                return "active"
            return "unknown"
    except asyncio.TimeoutError:
        return "unknown"
    except Exception:
        return "unknown"

# ── Remove de plataforma ──────────────────────────────────────────────────────

async def remove_from_platform(listing: dict, platform: str,
                               creds: dict) -> bool:
    """Remove/desactiva um anúncio numa plataforma específica."""
    plat_data = listing.get("platforms", {}).get(platform, {})
    url = plat_data if isinstance(plat_data, str) else plat_data.get("url", "")
    plat_creds = creds.get(platform, {})

    if platform in ("vinted", "wallapop", "segunda"):
        # Browser automation para remover
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 Chrome/124.0",
                    locale="pt-PT"
                )
                # Restaurar sessão
                sess_file = _sessions_path / f"{platform}_session.json"
                if sess_file.exists():
                    await ctx.add_cookies(json.loads(sess_file.read_text()))

                page = await ctx.new_page()
                if platform == "vinted":
                    await _remove_vinted(page, url, plat_creds)
                elif platform == "wallapop":
                    await _remove_wallapop(page, url, plat_creds)
                elif platform == "segunda":
                    await _remove_segunda(page, url, plat_creds)

                cookies = await ctx.cookies()
                sess_file.write_text(json.dumps(cookies))
                await browser.close()
            return True
        except Exception as e:
            print(f"[sync] remove {platform}: {e}")
            return False

    elif platform == "olx":
        # API OLX
        try:
            plat_id = plat_data if isinstance(plat_data, str) else plat_data.get("id", "")
            if plat_id and plat_creds.get("client_id"):
                async with aiohttp.ClientSession() as s:
                    # Obter token
                    tok = await _olx_token(s, plat_creds)
                    if tok:
                        async with s.delete(
                            f"https://api.olx.pt/v1/adverts/{plat_id}",
                            headers={"Authorization": f"Bearer {tok}"},
                            timeout=aiohttp.ClientTimeout(total=8)
                        ) as r:
                            return r.status in (200, 204)
        except Exception as e:
            print(f"[sync] remove olx: {e}")
    return False

async def _remove_vinted(page, url, creds):
    if not url: return
    item_id = re.search(r"/items/(\d+)", url)
    if not item_id: return
    edit_url = f"https://www.vinted.pt/items/{item_id.group(1)}/edit"
    await page.goto(edit_url, wait_until="domcontentloaded")
    await asyncio.sleep(2)
    try:
        # Clicar em "Remover anúncio"
        for sel in ['[data-testid="delete-item"]', 'button:has-text("Remover")',
                    'button:has-text("Apagar")', '[class*="delete"]']:
            try:
                await page.click(sel, timeout=3000)
                await asyncio.sleep(1)
                # Confirmar
                for conf in ['[data-testid="confirm-delete"]', 'button:has-text("Confirmar")',
                             'button:has-text("Sim")']:
                    try:
                        await page.click(conf, timeout=2000)
                        await asyncio.sleep(1)
                        return
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

async def _remove_wallapop(page, url, creds):
    if not url: return
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(2)
    try:
        for sel in ['button[class*="delete"]', 'button:has-text("Eliminar")',
                    '[data-testid="delete"]', 'button:has-text("Borrar")']:
            try:
                await page.click(sel, timeout=3000)
                await asyncio.sleep(1)
                for conf in ['button:has-text("Confirmar")', 'button:has-text("Sí")',
                             'button:has-text("Sim")']:
                    try:
                        await page.click(conf, timeout=2000)
                        return
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

async def _remove_segunda(page, url, creds):
    if not url: return
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(2)
    try:
        for sel in ['a:has-text("Apagar")', 'button:has-text("Eliminar")',
                    '[class*="delete"]', 'a:has-text("Eliminar")']:
            try:
                await page.click(sel, timeout=3000)
                await asyncio.sleep(1)
                for conf in ['button:has-text("Confirmar")', 'button:has-text("Sim")',
                             'input[value="Confirmar"]']:
                    try:
                        await page.click(conf, timeout=2000)
                        return
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

async def _olx_token(session, creds) -> str:
    try:
        async with session.post("https://auth.olx.pt/oauth/token",
            data={"grant_type":"client_credentials",
                  "client_id":creds["client_id"],
                  "client_secret":creds["client_secret"],
                  "scope":"v1:adverts:write"},
            timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                return (await r.json()).get("access_token", "")
    except Exception:
        pass
    return ""

# ── Auto-relist (OLX 30 dias) ─────────────────────────────────────────────────

# Expiry agora gerido por PLATFORM_EXPIRY_DAYS acima

async def check_platform_expiry(listing: dict, platform: str, creds: dict) -> str:
    """Verifica e republica anúncios prestes a expirar em qualquer plataforma."""
    expiry = PLATFORM_EXPIRY_DAYS.get(platform)
    if not expiry:
        return "no_expiry"
    warn_days   = PLATFORM_WARN_DAYS.get(platform, expiry - 5)
    relist_days = PLATFORM_RELIST_DAYS.get(platform, expiry - 2)
    plat = listing.get("platforms", {}).get(platform, {})
    if not plat:
        return "no_platform"

    published_str = plat.get("published_at") if isinstance(plat, dict) else None
    if not published_str:
        return "unknown_date"

    try:
        published = datetime.fromisoformat(published_str)
        age_days  = (datetime.now() - published).days

        if age_days >= relist_days:
            # Republicar automaticamente
            await _notify("olx_relist", {
                "listing_id": listing["id"],
                "title": listing["title"],
                "age_days": age_days,
                "message": f"A republicar '{listing['title']}' em {platform} (expirava em {expiry - age_days} dias)"
            })
            if _publish_fn:
                await _publish_fn(listing, [platform], creds)
                # Actualizar data de publicação
                if isinstance(plat, dict):
                    plat["published_at"] = datetime.now().isoformat()
                listing["platforms"]["olx"] = plat
                _save_listing(listing)
            return "relisted"

        elif age_days >= warn_days:
            await _notify("olx_warning", {
                "listing_id": listing["id"],
                "title": listing["title"],
                "expires_in": OLX_EXPIRY_DAYS - age_days,
                "message": f"'{listing['title']}' expira em {platform} em {expiry - age_days} dias"
            })
            return "warning"

    except Exception as e:
        print(f"[sync] olx_expiry: {e}")

    return "ok"

# ── Ciclo principal de sync ───────────────────────────────────────────────────

sync_log: list[dict] = []
sync_running: bool   = False

async def run_sync_cycle():
    """Verifica todos os anúncios activos — vendidos, expirados, OLX relist."""
    global sync_running
    sync_running = True
    creds        = _load_creds()
    listings     = _load_listings()
    active       = [l for l in listings if l.get("status") == "active"
                    and l.get("platforms")]

    cycle_log = {
        "started":  datetime.now().isoformat(),
        "checked":  0,
        "sold":     [],
        "relisted": [],
        "warnings": [],
        "errors":   [],
    }

    headers = {"User-Agent": "Mozilla/5.0 Chrome/124.0"}

    async with aiohttp.ClientSession(headers=headers) as session:
        for listing in active:
            for platform, plat_data in listing.get("platforms", {}).items():
                url = plat_data if isinstance(plat_data, str) else plat_data.get("url", "")
                if not url:
                    continue

                try:
                    status = await check_listing_status(session, url, platform)
                    cycle_log["checked"] += 1

                    if status == "sold":
                        # VENDIDO — remover de todas as outras plataformas
                        other_platforms = [p for p in listing["platforms"] if p != platform]
                        removed = []

                        await _notify("item_sold", {
                            "listing_id": listing["id"],
                            "title": listing["title"],
                            "sold_on": platform,
                            "removing_from": other_platforms,
                            "message": f"✅ '{listing['title']}' vendido no {platform}! A remover de {', '.join(other_platforms)}..."
                        })

                        for other in other_platforms:
                            ok = await remove_from_platform(listing, other, creds)
                            if ok:
                                removed.append(other)

                        # Marcar como vendido
                        listing["status"] = "sold"
                        listing["sold_on"] = platform
                        listing["sold_at"] = datetime.now().isoformat()
                        listing["removed_from"] = removed
                        _save_listing(listing)

                        cycle_log["sold"].append({
                            "id": listing["id"],
                            "title": listing["title"],
                            "sold_on": platform,
                            "removed_from": removed
                        })

                        await _notify("item_sold_done", {
                            "listing_id": listing["id"],
                            "title": listing["title"],
                            "sold_on": platform,
                            "removed_from": removed,
                            "message": f"✅ Concluído: '{listing['title']}' marcado como vendido. Removido de {len(removed)} plataforma(s)."
                        })
                        break  # Já não precisamos de verificar as outras

                    elif status == "removed":
                        # Removido/expirado nesta plataforma
                        listing["platforms"].pop(platform, None)
                        _save_listing(listing)
                        cycle_log["warnings"].append({
                            "title": listing["title"],
                            "platform": platform,
                            "reason": "removed_or_expired"
                        })
                        await _notify("item_removed", {
                            "title": listing["title"],
                            "platform": platform,
                            "message": f"⚠️ '{listing['title']}' foi removido do {platform} (expirou ou foi apagado)"
                        })

                except Exception as e:
                    cycle_log["errors"].append({"title": listing.get("title", ""), "error": str(e)[:100]})
                    continue

                await asyncio.sleep(1)  # respeitar rate limits

            # Verificar expiração em TODAS as plataformas com limite de tempo
            for plat_name in list(listing.get("platforms", {}).keys()):
                if PLATFORM_EXPIRY_DAYS.get(plat_name):
                    try:
                        result = await check_platform_expiry(listing, plat_name, creds)
                        if result == "relisted":
                            cycle_log["relisted"].append(f"{listing['title']} ({plat_name})")
                        elif result == "warning":
                            cycle_log["warnings"].append({
                                "title": listing["title"],
                                "platform": plat_name,
                                "reason": "expiry_warning"
                            })
                    except Exception as e:
                        cycle_log["errors"].append({
                            "title": listing.get("title", ""),
                            "error": str(e)[:100]
                        })

    cycle_log["finished"] = datetime.now().isoformat()
    cycle_log["duration"] = str(round(
        (datetime.fromisoformat(cycle_log["finished"]) -
         datetime.fromisoformat(cycle_log["started"])).total_seconds()
    )) + "s"

    sync_log.insert(0, cycle_log)
    if len(sync_log) > 20:
        sync_log.pop()

    sync_running = False

    await _notify("sync_done", {
        "checked": cycle_log["checked"],
        "sold": len(cycle_log["sold"]),
        "relisted": len(cycle_log["relisted"]),
        "warnings": len(cycle_log["warnings"]),
        "message": f"Sync completo: {cycle_log['checked']} verificados, {len(cycle_log['sold'])} vendidos, {len(cycle_log['relisted'])} relistados"
    })

    return cycle_log


# ── Scheduler automático ──────────────────────────────────────────────────────

_scheduler_task = None
_sync_interval_hours = 2  # verificar de 2 em 2 horas

async def start_scheduler(interval_hours: int = 2):
    """Arranca o scheduler de sync automático."""
    global _scheduler_task, _sync_interval_hours
    _sync_interval_hours = interval_hours
    if _scheduler_task:
        _scheduler_task.cancel()
    _scheduler_task = asyncio.create_task(_scheduler_loop())

async def _scheduler_loop():
    while True:
        await asyncio.sleep(_sync_interval_hours * 3600)
        if not sync_running:
            print(f"[sync] A iniciar ciclo automático...")
            await run_sync_cycle()
