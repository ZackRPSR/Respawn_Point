import os
import re
import glob
import time
import json
import shutil
import datetime
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
import config

try:
    import yaml
except ImportError:
    yaml = None

_manifest_data = None

def log(msg):
    print(msg)
    try:
        log_path = os.path.join("logs", "debug_log.txt")
        os.makedirs("logs", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

def sanitize_name(name):
    return re.sub(r'[^A-Za-z0-9\-]+', '-', name).strip('-')

def time_ago(timestamp_str):
    if not timestamp_str:
        return "Never"
    try:
        last = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        last = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M")

    diff = datetime.datetime.now() - last
    seconds = diff.total_seconds()
    days = diff.days

    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        m = int(seconds // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if seconds < 86400:
        h = int(seconds // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if days < 30:
        w = days // 7
        return f"{w} week{'s' if w != 1 else ''} ago"
    if days < 180:
        mo = days // 30
        return f"{mo} month{'s' if mo != 1 else ''} ago"
    if days < 365:
        return "6 months ago"
    if days < 730:
        return "1 year ago"
    return "A long time ago"

def paths_conflict(save_path, exe_path):
    if not save_path or not exe_path:
        return False
    save_norm = os.path.normcase(os.path.normpath(os.path.abspath(save_path)))
    exe_folder_norm = os.path.normcase(os.path.normpath(os.path.abspath(os.path.dirname(exe_path))))
    return save_norm == exe_folder_norm or exe_folder_norm.startswith(save_norm + os.sep)

# --- Save Backup Functions ---
def get_backup_dir(game_id, name):
    folder_name = f"{game_id}_{sanitize_name(name)}"
    path = os.path.join(config.BACKUPS_ROOT, folder_name, "backup")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def backup_save(game_id, name, save_path):
    if not save_path or not os.path.isdir(save_path):
        print(f"[DEBUG] Skipping backup - save folder not found: {save_path}")
        return
    if not os.listdir(save_path):
        print(f"[DEBUG] Skipping backup - save folder is empty: {save_path}")
        return

    backup_dir = get_backup_dir(game_id, name)
    try:
        if os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir)
        shutil.copytree(save_path, backup_dir)
        print(f"[DEBUG] Backed up save -> {backup_dir}")
    except Exception as e:
        print(f"[DEBUG] Backup failed: {e}")

def restore_if_missing(game_id, name, save_path):
    if not save_path:
        print("[DEBUG] No save path set - skipping restore check")
        return

    backup_dir = get_backup_dir(game_id, name)
    if not os.path.isdir(backup_dir) or not os.listdir(backup_dir):
        print("[DEBUG] No backup exists yet - nothing to restore")
        return

    if os.path.isdir(save_path) and os.listdir(save_path):
        print(f"[DEBUG] Live save folder already has files - not touching it: {save_path}")
        return

    os.makedirs(save_path, exist_ok=True)
    try:
        for item in os.listdir(backup_dir):
            s = os.path.join(backup_dir, item)
            d = os.path.join(save_path, item)
            if os.path.isdir(s):
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
        print(f"[DEBUG] Restored backup into empty save folder: {save_path}")
    except Exception as e:
        print(f"[DEBUG] Restore failed: {e}")

# --- Manifest / Auto-Detect Functions ---
def ensure_manifest_downloaded():
    if not os.path.exists(config.MANIFEST_CACHE_FILE):
        print("[DEBUG] Downloading save-location database (first time only)...")
        try:
            with urllib.request.urlopen(config.MANIFEST_URL, timeout=30) as response:
                data = response.read()
            with open(config.MANIFEST_CACHE_FILE, "wb") as f:
                f.write(data)
            print(f"[DEBUG] Download complete ({len(data)} bytes).")
        except Exception as e:
            print(f"[DEBUG] Download failed: {e}")
            raise

def load_manifest():
    global _manifest_data
    if _manifest_data is not None:
        return _manifest_data
    if yaml is None:
        print("[DEBUG] pyyaml not installed - auto-detect unavailable")
        return {}
    ensure_manifest_downloaded()

    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    print(f"[DEBUG] Parsing save-location database using {loader.__name__}...")
    with open(config.MANIFEST_CACHE_FILE, "r", encoding="utf-8") as f:
        _manifest_data = yaml.load(f, Loader=loader)
    print(f"[DEBUG] Parsed {len(_manifest_data)} game entries.")
    return _manifest_data

def _resolve_placeholders(template):
    if "<base>" in template or "<root>" in template:
        return None

    replacements = {
        "<home>": str(Path.home()),
        "<winAppData>": os.environ.get("APPDATA", ""),
        "<winLocalAppData>": os.environ.get("LOCALAPPDATA", ""),
        "<winDocuments>": str(Path.home() / "Documents"),
        "<winProgramData>": os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        "<storeUserId>": "*",
    }
    resolved = template
    for placeholder, value in replacements.items():
        resolved = resolved.replace(placeholder, value)

    if "<" in resolved:
        return None

    return resolved

def find_save_candidates(game_name):
    manifest = load_manifest()
    if not manifest:
        return []

    entry = None
    lower_name = game_name.strip().lower()
    for key in manifest:
        if key.lower() == lower_name:
            entry = manifest[key]
            break
    if entry is None:
        for key in manifest:
            if lower_name in key.lower() or key.lower() in lower_name:
                entry = manifest[key]
                break
    if entry is None or "files" not in entry:
        return []

    candidate_dirs = set()
    for path_template, meta in entry["files"].items():
        tags = meta.get("tags", [])
        if "save" not in tags:
            continue

        when_list = meta.get("when")
        if when_list:
            applies_to_windows = any(
                item.get("os") in (None, "windows") for item in when_list
            )
            if not applies_to_windows:
                continue

        pattern = _resolve_placeholders(path_template)
        if not pattern:
            continue

        for match in glob.glob(pattern):
            candidate_dirs.add(os.path.dirname(match))

    return sorted(candidate_dirs)

# --- SteamGridDB Art Functions ---
def _sgdb_request(url, retry=True):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {config.STEAMGRIDDB_API_KEY}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RespawnPointLauncher/1.0",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read()
            if not raw.strip():
                log("[DEBUG] SteamGridDB returned an empty response body")
                raise ValueError("Empty response from SteamGridDB")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception as read_err:
            body = f"<could not read response body: {read_err}>"
        log(f"[DEBUG] SteamGridDB HTTP {e.code} - response body: {body}")
        if retry and e.code in (403, 429):
            log("[DEBUG] Possible rate limit - waiting 5s and retrying once...")
            time.sleep(5)
            return _sgdb_request(url, retry=False)
        raise
    except Exception as other_err:
        log(f"[DEBUG] Non-HTTP error calling SteamGridDB: {type(other_err).__name__}: {other_err}")
        raise

def get_cover_art_path(game_id, name):
    os.makedirs(config.COVER_ART_ROOT, exist_ok=True)
    return os.path.join(config.COVER_ART_ROOT, f"{game_id}_{sanitize_name(name)}.png")

def get_hero_art_path(game_id, name):
    os.makedirs(config.HERO_ART_ROOT, exist_ok=True)
    return os.path.join(config.HERO_ART_ROOT, f"{game_id}_{sanitize_name(name)}.png")

def _download_sgdb_image(image_url, dest_path, name, label):
    log(f"[DEBUG] Downloading {label}: {image_url}")
    img_req = urllib.request.Request(
        image_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RespawnPointLauncher/1.0",
            "Referer": "https://www.steamgriddb.com/",
        }
    )
    try:
        with urllib.request.urlopen(img_req, timeout=25) as img_response:
            image_bytes = img_response.read()
    except urllib.error.HTTPError as e:
        log(f"[DEBUG] {label} download HTTP {e.code} for {name} - URL: {image_url}")
        return False
    with open(dest_path, "wb") as f:
        f.write(image_bytes)
    log(f"[DEBUG] Saved {label} -> {dest_path}")
    return True

def fetch_game_art(game_id, name):
    cover_path = get_cover_art_path(game_id, name)
    hero_path = get_hero_art_path(game_id, name)
    need_cover = not os.path.exists(cover_path)
    need_hero = not os.path.exists(hero_path)

    if not need_cover and not need_hero:
        return True

    try:
        log(f"[DEBUG] Searching art for: {name}")
        time.sleep(1)
        search_url = f"{config.STEAMGRIDDB_BASE}/search/autocomplete/{urllib.parse.quote(name)}"
        result = _sgdb_request(search_url)

        if not result.get("success") or not result.get("data"):
            log(f"[DEBUG] No SteamGridDB match found for: {name}")
            return False

        sgdb_game_id = result["data"][0]["id"]
        got_something = False

        if need_cover:
            grids_url = f"{config.STEAMGRIDDB_BASE}/grids/game/{sgdb_game_id}?dimensions=600x900"
            grids_result = _sgdb_request(grids_url)
            if grids_result.get("success") and grids_result.get("data"):
                if _download_sgdb_image(grids_result["data"][0]["url"], cover_path, name, "cover art"):
                    got_something = True

        if need_hero:
            time.sleep(1)
            heroes_url = f"{config.STEAMGRIDDB_BASE}/heroes/game/{sgdb_game_id}?dimensions=1920x620"
            heroes_result = _sgdb_request(heroes_url)
            if heroes_result.get("success") and heroes_result.get("data"):
                if _download_sgdb_image(heroes_result["data"][0]["url"], hero_path, name, "hero art"):
                    got_something = True

        return got_something

    except Exception as e:
        log(f"[DEBUG] Art fetch failed for {name}: {e}")
        return False