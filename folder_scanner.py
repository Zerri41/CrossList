"""
CrossList Pro — Folder Scanner v2
Suporta pastas com fotos soltas sem estrutura de subpastas.

Modos:
  batch        — cada N fotos (ordenadas por nome) = 1 produto
  flat_groups  — agrupa por prefixo de nome de ficheiro
  subfolders   — cada subpasta = 1 produto
  auto         — detecta automaticamente
"""

import os, re, math
from pathlib import Path
from datetime import datetime

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}

def _get_images_sorted(folder: Path) -> list[str]:
    """Lista imagens ordenadas por nome (reflecte ordem de captura)."""
    images = []
    for f in sorted(os.listdir(folder)):
        p = folder / f
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            images.append(str(p.resolve()))
    return images

def _title_from_key(key: str) -> str:
    name = key.replace("_", " ").replace("-", " ").strip()
    return " ".join(p.capitalize() for p in name.split()) or "Produto"

def _title_from_folder(folder: Path) -> str:
    return _title_from_key(folder.name)

def _group_key(filename: str) -> str:
    stem = Path(filename).stem.lower().strip()
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    stem = re.sub(r"([_\-\s])(?:foto|photo|img|image|pic)?\s*\d{1,3}$", "", stem)
    stem = re.sub(r"([_\-\s])(?:frente|costas|tras|trás|lado|etiqueta|detalhe|detail|front|back|side)$", "", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" _-")
    generic = {"img", "image", "foto", "photo", "dsc", "screenshot", "whatsapp image"}
    if not stem or stem in generic or re.fullmatch(r"(img|dsc|image|foto|pic|photo)[_\-\s]?\d+", stem):
        return "__generic__"
    return stem

# ── Modo BATCH: cada N fotos = 1 produto ─────────────────────────────────────

def scan_batch(root_path: str, photos_per_product: int = 4) -> list[dict]:
    """
    Agrupa todas as fotos em grupos de N, por ordem de nome.
    Ideal para pastas com fotos soltas sem nome de produto.
    """
    root = Path(root_path).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"Pasta não encontrada: {root_path}")

    images = _get_images_sorted(root)
    if not images:
        return []

    n = max(1, photos_per_product)
    total = len(images)
    num_products = math.ceil(total / n)
    products = []

    for i in range(num_products):
        chunk = images[i * n : (i + 1) * n]
        # Título baseado no número do produto
        products.append({
            "title": f"Produto {i + 1}",
            "images": chunk,
            "folder": str(root),
            "source": "batch",
            "index": i,
        })

    return products

# ── Modo FLAT_GROUPS: agrupar por prefixo de nome ────────────────────────────

def scan_flat_groups(root_path: str) -> list[dict]:
    root = Path(root_path).expanduser().resolve()
    images = _get_images_sorted(root)
    if not images:
        return []

    groups: dict[str, list[str]] = {}
    generic_list: list[str] = []

    for img in images:
        key = _group_key(Path(img).name)
        if key == "__generic__":
            generic_list.append(img)
        else:
            groups.setdefault(key, []).append(img)

    products = []
    for key, imgs in sorted(groups.items()):
        products.append({
            "title": _title_from_key(key),
            "images": imgs[:20],
            "folder": str(root),
            "source": "flat_group",
        })

    # Fotos genéricas que não agruparam: tratar como batch de 4
    if generic_list:
        n = 4
        for i in range(math.ceil(len(generic_list) / n)):
            chunk = generic_list[i * n : (i + 1) * n]
            products.append({
                "title": f"Produto {i + 1}",
                "images": chunk,
                "folder": str(root),
                "source": "batch",
                "index": i,
            })

    return products

# ── Modo SUBFOLDERS ───────────────────────────────────────────────────────────

def scan_subfolders(root_path: str) -> list[dict]:
    root = Path(root_path).expanduser().resolve()
    products = []
    for item in sorted(os.listdir(root)):
        sub = root / item
        if sub.is_dir():
            imgs = _get_images_sorted(sub)
            if imgs:
                products.append({
                    "title": _title_from_folder(sub),
                    "images": imgs[:20],
                    "folder": str(sub),
                    "source": "subfolder",
                })
    # fotos soltas na raiz
    root_imgs = _get_images_sorted(root)
    if root_imgs:
        products.extend(scan_flat_groups(root_path))
    return products

# ── Auto-detect ───────────────────────────────────────────────────────────────

def scan_auto(root_path: str, photos_per_product: int = 4) -> list[dict]:
    root = Path(root_path).expanduser().resolve()
    has_subfolders = any(
        (root / f).is_dir() for f in os.listdir(root)
        if (root / f).is_dir() and any(
            Path(x).suffix.lower() in IMAGE_EXTS
            for x in os.listdir(root / f)
        )
    )
    root_images = _get_images_sorted(root)

    if has_subfolders and not root_images:
        return scan_subfolders(root_path)

    # Verificar se nomes são genéricos
    if root_images:
        generic_count = sum(
            1 for img in root_images
            if _group_key(Path(img).name) == "__generic__"
        )
        if generic_count / len(root_images) > 0.6:
            # Maioria genérica → modo batch
            return scan_batch(root_path, photos_per_product)
        else:
            return scan_flat_groups(root_path)

    return scan_subfolders(root_path)

# ── Entrada principal ─────────────────────────────────────────────────────────

def scan_folder(root_path: str, mode: str = "auto",
                photos_per_product: int = 4) -> list[dict]:
    if mode == "batch":
        return scan_batch(root_path, photos_per_product)
    elif mode == "flat_groups":
        return scan_flat_groups(root_path)
    elif mode == "subfolders":
        return scan_subfolders(root_path)
    else:
        return scan_auto(root_path, photos_per_product)

def scan_to_listings(root_path: str, mode: str = "auto",
                     photos_per_product: int = 4,
                     existing_folders: set = None) -> dict:
    products = scan_folder(root_path, mode, photos_per_product)
    existing = existing_folders or set()
    new_items, skipped = [], 0
    for p in products:
        folder_key = p["folder"] + "_" + str(p.get("index", p["title"]))
        if folder_key in existing:
            skipped += 1
        else:
            new_items.append(p)
    return {
        "found": len(products),
        "new": len(new_items),
        "skipped": skipped,
        "items": new_items,
        "mode_used": mode,
        "photos_per_product": photos_per_product,
        "scanned_at": datetime.now().isoformat(),
    }
