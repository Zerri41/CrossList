"""
CrossList Pro — Google Drive Sync
Monitoriza pasta Google Drive e sincroniza com o CrossList.

Fluxo:
  Drive (novo ficheiro)  → CrossList gallery (aparece automaticamente)
  Drive (ficheiro apagado) → CrossList remove o produto
  CrossList (produto apagado) → ficheiro movido para pasta "Arquivado" no Drive
"""

import asyncio, json, io, base64, mimetypes
from pathlib import Path
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError

IMAGE_MIMES = {
    "image/jpeg", "image/png", "image/webp",
    "image/heic", "image/heif", "image/avif"
}

SCOPES = ["https://www.googleapis.com/auth/drive"]

# ── Estado global ─────────────────────────────────────────────────────────────

_service       = None
_folder_id     = None
_archived_id   = None   # pasta "Arquivado" no Drive
_known_files   = {}     # { file_id: { name, thumbnail_url, created_at } }
_page_token    = None   # Drive changes API token
_broadcast_fn  = None
_on_new_file   = None   # callback(file_id, name, thumbnail_url, drive_url)
_on_del_file   = None   # callback(file_id)

def init(service_account_json: dict, folder_id: str,
         broadcast, on_new_file, on_del_file):
    global _service, _folder_id, _broadcast_fn, _on_new_file, _on_del_file
    creds    = service_account.Credentials.from_service_account_info(
        service_account_json, scopes=SCOPES)
    _service      = build("drive", "v3", credentials=creds)
    _folder_id    = folder_id
    _broadcast_fn = broadcast
    _on_new_file  = on_new_file
    _on_del_file  = on_del_file

def is_ready() -> bool:
    return _service is not None and _folder_id is not None

# ── Operações Drive ───────────────────────────────────────────────────────────

def _list_images(folder_id: str) -> list[dict]:
    """Lista todas as imagens numa pasta Drive."""
    results = []
    page_token = None
    while True:
        resp = _service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,createdTime,thumbnailLink,webViewLink)",
            pageToken=page_token,
            orderBy="createdTime",
            pageSize=100
        ).execute()
        for f in resp.get("files", []):
            if f.get("mimeType", "") in IMAGE_MIMES or f["name"].lower().rsplit(".",1)[-1] in {"jpg","jpeg","png","webp","heic","heif","avif"}:
                results.append(f)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results

def _ensure_archived_folder() -> str:
    """Cria ou encontra pasta 'Arquivado' dentro da pasta principal."""
    global _archived_id
    if _archived_id:
        return _archived_id
    resp = _service.files().list(
        q=f"'{_folder_id}' in parents and name='Arquivado' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id,name)"
    ).execute()
    files = resp.get("files", [])
    if files:
        _archived_id = files[0]["id"]
    else:
        folder = _service.files().create(
            body={"name": "Arquivado", "mimeType": "application/vnd.google-apps.folder",
                  "parents": [_folder_id]},
            fields="id"
        ).execute()
        _archived_id = folder["id"]
    return _archived_id

def get_thumbnail_url(file_id: str, size: int = 200) -> str:
    """URL de thumbnail público para exibir na galeria."""
    return f"https://drive.google.com/thumbnail?id={file_id}&sz=w{size}"

def get_drive_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"

def move_to_archived(file_id: str) -> bool:
    """Move ficheiro para a pasta 'Arquivado'."""
    try:
        archived_id = _ensure_archived_folder()
        file = _service.files().get(fileId=file_id, fields="parents").execute()
        prev_parents = ",".join(file.get("parents", []))
        _service.files().update(
            fileId=file_id,
            addParents=archived_id,
            removeParents=prev_parents,
            fields="id,parents"
        ).execute()
        return True
    except HttpError:
        return False

def get_file_as_base64(file_id: str, mime: str = "image/jpeg") -> str:
    """Download ficheiro e converte para base64 (para guardar no listing)."""
    request = _service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    data = base64.b64encode(buf.getvalue()).decode()
    return f"data:{mime};base64,{data}"

# ── Sincronização ─────────────────────────────────────────────────────────────

async def full_sync() -> dict:
    """Carrega estado completo da pasta Drive."""
    global _known_files, _page_token
    if not is_ready():
        return {"error": "Drive não configurado"}

    loop = asyncio.get_event_loop()
    try:
        files = await loop.run_in_executor(None, lambda: _list_images(_folder_id))
    except Exception as e:
        return {"error": str(e)}

    _known_files = {}
    for f in files:
        _known_files[f["id"]] = {
            "id": f["id"],
            "name": f["name"],
            "thumbnail": get_thumbnail_url(f["id"]),
            "url": get_drive_url(f["id"]),
            "mime": f.get("mimeType", "image/jpeg"),
            "created_at": f.get("createdTime", ""),
        }

    # Guardar page token para tracking de mudanças
    try:
        resp = await loop.run_in_executor(
            None, lambda: _service.changes().getStartPageToken().execute())
        _page_token = resp.get("startPageToken")
    except Exception:
        pass

    return {
        "files": list(_known_files.values()),
        "total": len(_known_files),
        "folder_id": _folder_id,
        "synced_at": datetime.now().isoformat(),
    }

async def check_changes() -> list[dict]:
    """Verifica mudanças no Drive desde o último sync."""
    global _known_files, _page_token
    if not is_ready() or not _page_token:
        return []

    loop   = asyncio.get_event_loop()
    events = []

    try:
        resp = await loop.run_in_executor(None, lambda: _service.changes().list(
            pageToken=_page_token,
            fields="nextPageToken, newStartPageToken, changes(fileId,removed,file(id,name,mimeType,parents,trashed,createdTime))",
            spaces="drive"
        ).execute())

        for change in resp.get("changes", []):
            fid    = change["fileId"]
            removed = change.get("removed", False)
            file   = change.get("file", {})

            # Ficheiro removido ou movido para fora da pasta
            if removed or file.get("trashed") or _folder_id not in file.get("parents", []):
                if fid in _known_files:
                    events.append({"type": "removed", "file_id": fid,
                                   "name": _known_files[fid]["name"]})
                    del _known_files[fid]
                    if _on_del_file:
                        await _on_del_file(fid)

            # Ficheiro novo ou restaurado
            elif file.get("mimeType", "") in IMAGE_MIMES or \
                 file["name"].lower().rsplit(".",1)[-1] in {"jpg","jpeg","png","webp","heic","heif","avif"}:
                if _folder_id in file.get("parents", []) and not file.get("trashed"):
                    if fid not in _known_files:
                        entry = {
                            "id": fid,
                            "name": file["name"],
                            "thumbnail": get_thumbnail_url(fid),
                            "url": get_drive_url(fid),
                            "mime": file.get("mimeType", "image/jpeg"),
                            "created_at": file.get("createdTime", ""),
                        }
                        _known_files[fid] = entry
                        events.append({"type": "added", **entry})
                        if _on_new_file:
                            await _on_new_file(fid, file["name"],
                                               get_thumbnail_url(fid),
                                               get_drive_url(fid))

        _page_token = resp.get("newStartPageToken", resp.get("nextPageToken", _page_token))

    except Exception as e:
        print(f"[drive] check_changes: {e}")

    return events

# ── Loop de polling ───────────────────────────────────────────────────────────

_poll_task    = None
_poll_interval = 30  # segundos

async def start_polling(interval_seconds: int = 30):
    global _poll_task, _poll_interval
    _poll_interval = interval_seconds
    if _poll_task:
        _poll_task.cancel()
    _poll_task = asyncio.create_task(_poll_loop())

async def _poll_loop():
    while True:
        await asyncio.sleep(_poll_interval)
        if not is_ready():
            continue
        events = await check_changes()
        if events and _broadcast_fn:
            for e in events:
                msg = (
                    f"📁 Nova foto: {e['name']}" if e["type"] == "added"
                    else f"🗑 Foto removida: {e['name']}"
                )
                await _broadcast_fn({
                    "type": "drive_" + e["type"],
                    "file": e,
                    "message": msg
                })

def get_known_files() -> list[dict]:
    return list(_known_files.values())
