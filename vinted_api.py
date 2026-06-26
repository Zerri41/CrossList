"""
CrossList Pro — Vinted API Publisher (directo, sem Playwright)
Usa a API JSON do Vinted em vez de automação de formulário.

Fluxo:
  1. Obter CSRF token (da config da página)
  2. Upload de cada foto → POST /photos → recebe photo id
  3. Construir payload {"draft": {...}} com IDs correctos
  4. POST /item_upload/drafts (rascunho) ou /item_upload/items (publicar)

Requer: cookie de sessão válido (com _vinted_fr_session + datadome + anon_id).
O DataDome é a incógnita — se bloquear, devolve 403 access_denied.
"""

import aiohttp, asyncio, json, re, uuid, mimetypes
from pathlib import Path

BASE_API = "https://www.vinted.pt/api/v2"
BASE_WEB = "https://www.vinted.pt"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _build_cookie_header(raw_cookie: str) -> str:
    """Aceita o cookie colado pelo utilizador (pode ser só o valor da sessão,
    ou a string completa copiada do browser)."""
    raw = raw_cookie.strip()
    if "=" in raw and ";" in raw:
        # Já é uma string de cookies completa
        return raw
    if "=" in raw:
        return raw
    # Só o valor da sessão
    return f"_vinted_fr_session={raw}"


async def _get_csrf_token(session: aiohttp.ClientSession, headers: dict) -> str:
    """Lê o CSRF token da config embebida na página."""
    try:
        async with session.get(f"{BASE_WEB}/items/new", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            html = await r.text()
            # procurar CSRF_TOKEN":"<uuid>" ou "csrf_token":"..."
            for pat in [r'CSRF_TOKEN["\']?\s*[:=]\s*["\']([a-f0-9\-]{36})',
                        r'"csrf_token"\s*:\s*"([a-f0-9\-]{36})"',
                        r'csrf-token["\']?\s+content=["\']([a-f0-9\-]{36})']:
                m = re.search(pat, html)
                if m:
                    return m.group(1)
            print("[vinted-api] CSRF token não encontrado no HTML")
    except Exception as e:
        print(f"[vinted-api] erro ao obter CSRF: {e}")
    return ""


async def _upload_photo(session: aiohttp.ClientSession, headers: dict,
                        img_bytes: bytes, mime: str, temp_uuid: str) -> dict:
    """Upload de uma foto. Devolve {id, orientation} ou None."""
    form = aiohttp.FormData()
    form.add_field("photo[type]", "item")
    form.add_field("photo[temp_uuid]", temp_uuid)
    ext = "jpg" if "jpeg" in mime else mime.split("/")[-1]
    form.add_field("photo[file]", img_bytes,
                   filename=f"photo.{ext}", content_type=mime)

    ph_headers = {k: v for k, v in headers.items()
                  if k.lower() not in ("content-type",)}
    try:
        async with session.post(f"{BASE_API}/photos", data=form,
                                headers=ph_headers,
                                timeout=aiohttp.ClientTimeout(total=30)) as r:
            print(f"[vinted-api] POST /photos → HTTP {r.status}")
            if r.status in (200, 201):
                data = await r.json()
                photo = data.get("photo", data)
                return {"id": photo.get("id"), "orientation": 0}
            else:
                body = await r.text()
                print(f"[vinted-api] erro foto: {body[:200]}")
    except Exception as e:
        print(f"[vinted-api] excepção upload foto: {e}")
    return None


async def publish_to_vinted(listing: dict, cookie: str,
                            as_draft: bool = True) -> dict:
    """
    Publica (ou cria rascunho) na Vinted via API directa.
    Devolve {"success": bool, "url": str, "error": str, "draft_id": int}
    """
    result = {"success": False, "url": "", "error": "", "draft_id": None}

    if not cookie:
        result["error"] = "Sem session cookie"
        return result

    cookie_header = _build_cookie_header(cookie)
    temp_uuid = str(uuid.uuid4())

    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
        "Cookie": cookie_header,
        "Referer": f"{BASE_WEB}/items/new",
        "Origin": BASE_WEB,
        "X-Requested-With": "XMLHttpRequest",
    }

    async with aiohttp.ClientSession() as session:
        # 1. CSRF token
        csrf = await _get_csrf_token(session, headers)
        if csrf:
            headers["X-CSRF-Token"] = csrf
            print(f"[vinted-api] CSRF token obtido: {csrf[:8]}...")
        else:
            print("[vinted-api] ⚠ sem CSRF — provável 403")

        # 2. Upload das fotos
        assigned_photos = []
        images = listing.get("images", [])
        for idx, img in enumerate(images[:5]):
            if not img.startswith("data:"):
                continue
            try:
                header, b64data = img.split(",", 1)
                import base64
                img_bytes = base64.b64decode(b64data)
                mime = "image/jpeg"
                if "png" in header: mime = "image/png"
                elif "webp" in header: mime = "image/webp"
                photo = await _upload_photo(session, headers, img_bytes, mime, temp_uuid)
                if photo and photo.get("id"):
                    assigned_photos.append(photo)
                    print(f"[vinted-api] ✓ foto {idx+1} carregada: id={photo['id']}")
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[vinted-api] erro a processar foto {idx}: {e}")

        if not assigned_photos:
            result["error"] = "Nenhuma foto foi carregada (obrigatório no Vinted)"
            print(f"[vinted-api] ✗ {result['error']}")
            return result

        # 3. Construir payload
        # Mapear condição (texto → id)
        cond_map = {
            "new_with_tags": 6, "new": 1, "very_good": 2,
            "good": 3, "acceptable": 4, "satisfactory": 4, "poor": 7,
        }
        status_id = cond_map.get(listing.get("condition", "good"), 2)

        catalog_id = listing.get("vinted_category_id", 0)
        if not catalog_id:
            result["error"] = "Sem categoria Vinted (catalog_id). Escolhe a categoria primeiro."
            print(f"[vinted-api] ✗ {result['error']}")
            return result

        # Limpar título (Vinted rejeita demasiadas maiúsculas)
        title = listing.get("title", "")
        if title and sum(1 for c in title if c.isupper()) > len(title) * 0.5:
            title = title.capitalize()

        draft = {
            "title": title,
            "description": listing.get("description", ""),
            "catalog_id": int(catalog_id),
            "currency": "EUR",
            "price": str(float(listing.get("price", 0))),
            "status_id": status_id,
            "color_ids": [],
            "size_id": None,
            "brand_id": None,
            "package_size_id": 1,
            "assigned_photos": assigned_photos,
            "is_unisex": False,
        }

        # Cor (opcional) — mapear se existir
        color = (listing.get("color", "") or "").lower()
        color_map = {"preto":1,"black":1,"cinzento":3,"grey":3,"gray":3,
                     "branco":12,"white":12,"azul":7,"blue":7,"vermelho":10,"red":10,
                     "verde":9,"green":9,"amarelo":8,"yellow":8}
        if color in color_map:
            draft["color_ids"] = [color_map[color]]

        endpoint = f"{BASE_API}/item_upload/drafts" if as_draft else f"{BASE_API}/item_upload/items"
        wrapper = "draft" if as_draft else "item"
        payload = {wrapper: draft}

        headers["Content-Type"] = "application/json"

        print(f"[vinted-api] POST {endpoint}")
        print(f"[vinted-api] payload: catalog_id={catalog_id} status={status_id} price={draft['price']} fotos={len(assigned_photos)}")

        try:
            async with session.post(endpoint, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=20)) as r:
                print(f"[vinted-api] resposta HTTP {r.status}")
                body_text = await r.text()
                if r.status in (200, 201):
                    data = json.loads(body_text)
                    obj = data.get("draft") or data.get("item") or {}
                    obj_id = obj.get("id")
                    result["success"] = True
                    result["draft_id"] = obj_id
                    if as_draft:
                        result["url"] = f"{BASE_WEB}/items/draft/{obj_id}"
                        print(f"[vinted-api] ✓✓ RASCUNHO CRIADO: id={obj_id}")
                    else:
                        result["url"] = f"{BASE_WEB}/items/{obj_id}"
                        print(f"[vinted-api] ✓✓ PUBLICADO: id={obj_id}")
                elif r.status == 403:
                    result["error"] = "403 — DataDome/CSRF bloqueou (IP de datacenter detectado)"
                    print(f"[vinted-api] ✗ 403: {body_text[:300]}")
                else:
                    result["error"] = f"HTTP {r.status}: {body_text[:200]}"
                    print(f"[vinted-api] ✗ {result['error']}")
        except Exception as e:
            result["error"] = f"Excepção: {e}"
            print(f"[vinted-api] ✗ excepção: {e}")

    return result
