#!/usr/bin/env python3
"""
CS:GO Weapon Folder Organizer  —  parallel edition

Speed improvements:
  • All image downloads run concurrently (ThreadPoolExecutor, 32 workers)
  • Folders, JSON, and desktop.ini are created in the main thread (fast, safe)
  • Already-downloaded skin.png / .ico files are skipped on re-runs
  • A persistent requests.Session with connection pooling is shared across threads

Folder structure:
  output/
  ├── assets/icons/
  ├── <Category>/<Weapon>/default/{weapon_info.json, skin.png}
  ├── <Category>/<Weapon>/<Skin Name>/default/{skin_info.json, skin.png}
  └── <InventoryCategory>/<Item>/default/{skin_info.json, skin.png}
"""

import os, sys, json, re, struct, threading, requests
from pathlib import Path
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Pillow ────────────────────────────────────────────────────────────────────
try:
    from PIL import Image
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow",
                           "--break-system-packages", "-q"])
    from PIL import Image

# ── Config ────────────────────────────────────────────────────────────────────
BASE_WEAPONS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/base_weapons.json"
INVENTORY_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/inventory.json"
BASE_DIR         = Path("output")
ICONS_DIR        = BASE_DIR / "assets" / "icons"
MAX_WORKERS      = 10   # concurrent download threads

CATEGORY_LABELS = {
    "skins":        None,          # merged into weapon tree
    "crates":       "Crates",
    "stickers":     "Stickers",
    "patches":      "Patches",
    "graffiti":     "Graffiti",
    "music_kits":   "Music Kits",
    "collectibles": "Collectibles",
    "agents":       "Agents",
    "keys":         "Keys",
    "passes":       "Passes",
    "gifts":        "Gifts",
    "tools":        "Tools",
}

# ── Thread-safe print ─────────────────────────────────────────────────────────
_print_lock = threading.Lock()

def tprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

# ── Shared HTTP session ───────────────────────────────────────────────────────
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=MAX_WORKERS,
    pool_maxsize=MAX_WORKERS,
    max_retries=3,
)
_session.mount("https://", _adapter)
_session.mount("http://",  _adapter)

# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def download_image(url: str) -> "Image.Image | None":
    try:
        r = _session.get(url, timeout=15)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA")
    except Exception as e:
        tprint(f"⚠  {e}")
        return None


def save_ico(img: "Image.Image", path: Path) -> None:
    sizes = [(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)]
    frames = [img.resize(s, Image.LANCZOS) for s in sizes]
    frames[0].save(path, format="ICO",
                   sizes=[(f.width, f.height) for f in frames],
                   append_images=frames[1:])


def save_png(img: "Image.Image", path: Path, size: int = 256) -> None:
    img.resize((size, size), Image.LANCZOS).save(path, format="PNG")


# ── Windows folder icon ───────────────────────────────────────────────────────

def _win_attr(path: str, attr: int) -> None:
    import ctypes
    ctypes.windll.kernel32.SetFileAttributesW(path, attr)


def set_windows_folder_icon(folder: Path, ico_path: Path) -> None:
    if os.name != "nt":
        return
    import ctypes
    ini = folder / "desktop.ini"
    if ini.exists():
        _win_attr(str(ini), 0x80)
    _win_attr(str(folder), 0x10)
    ini.write_text(
        "[.ShellClassInfo]\r\n"
        f"IconResource={ico_path.resolve()},0\r\n"
        "IconIndex=0\r\n",
        encoding="utf-8",
    )
    _win_attr(str(ini), 0x02 | 0x04)
    _win_attr(str(folder), 0x01 | 0x10)
    try:
        ctypes.windll.shell32.SHChangeNotify(0x00001000, 0x0005,
                                             str(folder.resolve()), None)
    except Exception:
        pass


# ── macOS folder icon ─────────────────────────────────────────────────────────

def _make_icns(img: "Image.Image", out_path: Path) -> None:
    buf = BytesIO()
    img.resize((256,256), Image.LANCZOS).save(buf, format="PNG")
    png = buf.getvalue()
    chunk = b"ic08" + struct.pack(">I", 8 + len(png)) + png
    out_path.write_bytes(b"icns" + struct.pack(">I", 8 + len(chunk)) + chunk)


def set_macos_folder_icon(folder: Path, img: "Image.Image") -> None:
    if sys.platform != "darwin":
        return
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".icns", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        _make_icns(img, tmp_path)
        script = (
            f'tell application "Finder"\n'
            f'  set f to POSIX file "{folder.resolve()}" as alias\n'
            f'  set src to POSIX file "{tmp_path}" as alias\n'
            f'  set the icon of f to src\n'
            f'end tell'
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass
    finally:
        tmp_path.unlink(missing_ok=True)


# ── Task descriptor ───────────────────────────────────────────────────────────
# We collect everything we need to know about an item BEFORE spawning threads,
# so folder creation and name deduplication stay single-threaded and safe.

class Task:
    __slots__ = ("item_folder", "default_dir", "ico_path",
                 "img_url", "info", "info_filename", "label")
    def __init__(self, item_folder, default_dir, ico_path,
                 img_url, info, info_filename, label):
        self.item_folder  = item_folder
        self.default_dir  = default_dir
        self.ico_path     = ico_path
        self.img_url      = img_url
        self.info         = info
        self.info_filename= info_filename
        self.label        = label


def make_task(parent_folder, folder_name, icon_key,
              img_url, info, info_filename, label) -> Task:
    """
    Creates the folder skeleton synchronously, returns a Task
    for the slow part (download + image conversion) to run in a thread.
    """
    item_folder = parent_folder / folder_name
    default_dir = item_folder / "default"
    item_folder.mkdir(parents=True, exist_ok=True)
    default_dir.mkdir(parents=True, exist_ok=True)

    # Write JSON immediately — it's fast and needs no network
    (default_dir / info_filename).write_text(
        json.dumps(info, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    ico_path = ICONS_DIR / (sanitize(icon_key) + ".ico")
    return Task(item_folder, default_dir, ico_path,
                img_url, info, info_filename, label)


def execute_task(task: Task) -> str:
    """
    Runs in a worker thread.
    Downloads the image, saves skin.png + .ico, sets folder icon.
    Returns a one-line status string.
    Skips if skin.png already exists (resume support).
    """
    skin_png = task.default_dir / "skin.png"

    # Skip if already done (resume / re-run)
    if skin_png.exists() and task.ico_path.exists():
        return f"–  {task.label}  (skipped, already exists)"

    if not task.img_url:
        return f"✘  {task.label}  (no image URL)"

    img = download_image(task.img_url)
    if not img:
        return f"✘  {task.label}  (download failed)"

    save_ico(img, task.ico_path)
    save_png(img, skin_png)
    set_windows_folder_icon(task.item_folder, task.ico_path)
    set_macos_folder_icon(task.item_folder, img)
    return f"✔  {task.label}"


# ── Build task list from weapon tree ─────────────────────────────────────────

def collect_weapon_tasks(weapons, skins_by_def) -> list[Task]:
    tasks = []
    categories: dict[str, list[dict]] = {}
    for w in weapons:
        cat = w.get("category", {}).get("name", "Unknown")
        categories.setdefault(cat, []).append(w)

    for cat_name, cat_weapons in sorted(categories.items()):
        cat_folder = BASE_DIR / sanitize(cat_name)
        cat_folder.mkdir(parents=True, exist_ok=True)

        used_weapon_names: set[str] = set()
        for weapon in cat_weapons:
            w_name = weapon.get("name", "Unknown")
            w_id   = weapon.get("id", "unknown")
            w_def  = weapon.get("def_index")

            wfn = sanitize(w_name)
            if wfn in used_weapon_names:
                wfn = f"{wfn} ({sanitize(w_id.rsplit('-',1)[-1])})"
            used_weapon_names.add(wfn)

            w_folder = cat_folder / wfn
            w_folder.mkdir(parents=True, exist_ok=True)

            # Base weapon default
            tasks.append(make_task(
                parent_folder = w_folder,
                folder_name   = "default",
                icon_key      = w_id,
                img_url       = weapon.get("image", ""),
                info          = weapon,
                info_filename = "weapon_info.json",
                label         = w_name,
            ))

            # Skins
            weapon_skins = skins_by_def.get(str(w_def), {})
            used_skin_names: set[str] = set()
            for paint_index, skin in weapon_skins.items():
                s_name  = skin.get("name", f"Skin {paint_index}")
                skin_fn = sanitize(s_name.split(" | ",1)[1] if " | " in s_name else s_name)
                if skin_fn in used_skin_names:
                    skin_fn = f"{skin_fn} ({paint_index})"
                used_skin_names.add(skin_fn)

                tasks.append(make_task(
                    parent_folder = w_folder,
                    folder_name   = skin_fn,
                    icon_key      = f"{sanitize(w_id)}_paint{paint_index}",
                    img_url       = skin.get("image", ""),
                    info          = skin,
                    info_filename = "skin_info.json",
                    label         = s_name,
                ))

    return tasks


def collect_inventory_tasks(inventory) -> list[Task]:
    tasks = []
    for inv_key, inv_items in inventory.items():
        if inv_key == "skins":
            continue
        if not isinstance(inv_items, dict) or not inv_items:
            continue

        cat_label  = CATEGORY_LABELS.get(inv_key) or inv_key.replace("_"," ").title()
        cat_folder = BASE_DIR / sanitize(cat_label)
        cat_folder.mkdir(parents=True, exist_ok=True)

        first_val = next(iter(inv_items.values()))
        is_nested = isinstance(first_val, dict) and any(
            isinstance(v, dict) for v in first_val.values()
        )
        all_items = (
            [(f"{ok}_{ik}", item)
             for ok, inner in inv_items.items()
             if isinstance(inner, dict)
             for ik, item in inner.items()]
            if is_nested else list(inv_items.items())
        )

        used_names: set[str] = set()
        for item_key, item in all_items:
            # Skip anything that isn't a proper dict (stray strings, nulls, etc.)
            if not isinstance(item, dict):
                continue

            i_name = item.get("name", f"Item {item_key}")
            ifn    = sanitize(i_name)
            if ifn in used_names:
                ifn = f"{ifn} ({item_key})"
            used_names.add(ifn)

            tasks.append(make_task(
                parent_folder = cat_folder,
                folder_name   = ifn,
                icon_key      = f"{inv_key}_{sanitize(item_key)}",
                img_url       = item.get("image", ""),
                info          = item,
                info_filename = "skin_info.json",
                label         = f"[{cat_label}] {i_name}",
            ))

    return tasks


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("📡  Fetching base weapons…")
    weapons: list[dict] = _session.get(BASE_WEAPONS_URL, timeout=30).json()
    print(f"✅  {len(weapons)} weapons")

    print("📡  Fetching inventory…")
    inventory: dict      = _session.get(INVENTORY_URL, timeout=60).json()
    skins_by_def         = inventory.get("skins", {})
    total_skins          = sum(len(v) for v in skins_by_def.values())
    print(f"✅  {total_skins} skins + other inventory categories\n")

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    ICONS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Build all tasks (fast — just mkdir + write JSON) ──────────────────────
    print("🗂   Building folder tree…")
    tasks  = collect_weapon_tasks(weapons, skins_by_def)
    tasks += collect_inventory_tasks(inventory)
    print(f"📋  {len(tasks)} items queued  →  downloading with {MAX_WORKERS} threads\n")

    # ── Execute downloads in parallel ─────────────────────────────────────────
    done = 0
    total = len(tasks)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(execute_task, t): t for t in tasks}
        for future in as_completed(futures):
            done += 1
            result = future.result()
            tprint(f"  [{done:>4}/{total}]  {result}")

    print(f"\n🎉  Done!")
    print(f"    Folders : {BASE_DIR.resolve()}")
    print(f"    Icons   : {ICONS_DIR.resolve()}")


if __name__ == "__main__":
    main()