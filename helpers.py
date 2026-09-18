import os
import sys
import re
import glob
import time
import json
import shutil
import zipfile
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

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def get_font_path():
    """Returns absolute path to Cal Sans font file."""
    return resource_path(os.path.join("assets", "Fonts", "Cal Sans SemiBold.ttf"))

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

# --- Full-Data Backup / Restore (export-to-file, not the auto save backup above) ---
def create_backup_archive(dest_zip_path):
    """Bundles every game's current playtime/last-played stats plus each game's already-
    backed-up save files into one .rpbackup zip at dest_zip_path, so the user can move it
    wherever they want (cloud-synced folder, USB, etc). Read-only against the live game
    data - it only ever reads from the DB and from the existing per-game backup_dir
    (the same folder backup_save() already maintains), never from a game's actual save_path
    or install folder. Games are keyed by name (not id) since a fresh install can assign a
    different id to the same game. Returns the metadata dict written into the archive."""
    import database as db  # local import - avoids a circular import at module load time

    games = db.get_all_games()
    metadata = {}
    for game_id, name, exe_path, save_path, last_played, total_minutes in games:
        metadata[name] = {
            "last_played": last_played,
            "total_minutes": total_minutes,
        }

    with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.json", json.dumps(metadata, indent=2))

        for game_id, name, exe_path, save_path, last_played, total_minutes in games:
            backup_dir = get_backup_dir(game_id, name)
            if not os.path.isdir(backup_dir) or not os.listdir(backup_dir):
                continue
            folder_key = sanitize_name(name)
            for root, _, files in os.walk(backup_dir):
                for f in files:
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, backup_dir)
                    arcname = os.path.join("saves", folder_key, rel_path)
                    zf.write(full_path, arcname)

    print(f"[DEBUG] Backup archive created -> {dest_zip_path} ({len(metadata)} game(s))")
    return metadata

def restore_backup_archive(zip_path):
    """Restores playtime/last-played + save files from a .rpbackup archive created by
    create_backup_archive(). Only touches games that already exist in the CURRENT library
    (matched by exact name) - anything in the backup that isn't in the library yet is
    skipped and reported back, rather than guessed at or silently dropped. Playtime is
    merged non-destructively via db.merge_restored_stats() (never lowers existing
    progress). Save files are restored only into this game's backup_dir - never written
    directly into the live save_path - so the existing restore_if_missing() check (which
    only fires into an empty/missing save folder) is still what actually puts files back
    into the live game folder, the next time that game is launched. This keeps the
    never-auto-overwrite-a-live-save rule intact for restores too.
    Returns (restored_names, skipped_names)."""
    import database as db

    if not os.path.isfile(zip_path):
        return [], []

    games_by_name = {}
    for game_id, name, exe_path, save_path, last_played, total_minutes in db.get_all_games():
        games_by_name[name] = game_id

    restored, skipped = [], []

    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            metadata = json.loads(zf.read("metadata.json").decode("utf-8"))
        except KeyError:
            print("[DEBUG] Restore failed - not a valid Respawn Point backup (no metadata.json)")
            return [], []

        for name, stats in metadata.items():
            game_id = games_by_name.get(name)
            if game_id is None:
                skipped.append(name)
                continue

            db.merge_restored_stats(game_id, stats.get("last_played"), stats.get("total_minutes", 0))

            folder_key = sanitize_name(name)
            prefix = f"saves/{folder_key}/"
            members = [m for m in zf.namelist() if m.startswith(prefix) and not m.endswith("/")]
            if members:
                backup_dir = get_backup_dir(game_id, name)
                if os.path.isdir(backup_dir):
                    shutil.rmtree(backup_dir)
                os.makedirs(backup_dir, exist_ok=True)
                for member in members:
                    rel_path = member[len(prefix):]
                    dest_path = os.path.join(backup_dir, rel_path)
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    with zf.open(member) as src, open(dest_path, "wb") as out:
                        shutil.copyfileobj(src, out)

            restored.append(name)

    print(f"[DEBUG] Restore complete - {len(restored)} restored, {len(skipped)} skipped: {skipped}")
    return restored, skipped

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
        if e.code == 404:
            log(f"[DEBUG] SteamGridDB endpoint returned 404 for URL: {url}")
            return {"success": False, "data": []}
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

    # Smart search query adjustments for tightly bound names like 'Thenoexistencofyouandme'
    search_queries = [name]
    # If there are no spaces, try breaking apart capital letters or common patterns
    if " " not in name:
        # Example: insert spaces before capitals or use fallback variations if needed
        spaced = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
        if spaced != name:
            search_queries.append(spaced)

    sgdb_game_id = None
    try:
        for q in search_queries:
            log(f"[DEBUG] Searching art for query: {q}")
            time.sleep(1)
            
            search_url = f"{config.STEAMGRIDDB_BASE}/search/autocomplete/{urllib.parse.quote(q)}"
            result = _sgdb_request(search_url)

            if result.get("success") and result.get("data"):
                sgdb_game_id = result["data"][0]["id"]
                break
            else:
                fallback_url = f"{config.STEAMGRIDDB_BASE}/search/query/{urllib.parse.quote(q)}"
                fallback_result = _sgdb_request(fallback_url)
                if fallback_result.get("success") and fallback_result.get("data"):
                    sgdb_game_id = fallback_result["data"][0]["id"]
                    break

        if not sgdb_game_id:
            log(f"[DEBUG] No SteamGridDB match found for: {name}")
            return False

        got_something = False

        if need_cover:
            grids_url = f"{config.STEAMGRIDDB_BASE}/grids/game/{sgdb_game_id}?dimensions=600x900"
            grids_result = _sgdb_request(grids_url)
            if not grids_result.get("success") or not grids_result.get("data"):
                grids_url = f"{config.STEAMGRIDDB_BASE}/grids/game/{sgdb_game_id}"
                grids_result = _sgdb_request(grids_url)

            if grids_result.get("success") and grids_result.get("data"):
                for grid in grids_result["data"]:
                    if _download_sgdb_image(grid["url"], cover_path, name, "cover art"):
                        got_something = True
                        break

        if need_hero:
            time.sleep(1)
            heroes_url = f"{config.STEAMGRIDDB_BASE}/heroes/game/{sgdb_game_id}?dimensions=1920x620"
            heroes_result = _sgdb_request(heroes_url)
            if not heroes_result.get("success") or not heroes_result.get("data"):
                heroes_url = f"{config.STEAMGRIDDB_BASE}/heroes/game/{sgdb_game_id}"
                heroes_result = _sgdb_request(heroes_url)

            if heroes_result.get("success") and heroes_result.get("data"):
                for hero in heroes_result["data"]:
                    if _download_sgdb_image(hero["url"], hero_path, name, "hero art"):
                        got_something = True
                        break

        return got_something

    except Exception as e:
        log(f"[DEBUG] Art fetch failed for {name}: {e}")
        return False