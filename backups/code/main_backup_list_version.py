import customtkinter as ctk
from tkinter import filedialog
import sqlite3
import os
import subprocess
import threading
import datetime
import shutil
import glob
import urllib.request
import urllib.parse
import json
from pathlib import Path
from PIL import Image

try:
    import yaml
except ImportError:
    yaml = None

# --- Database setup ---
DB_FILE = "library.db"

# Tracks which games are currently launching/running, so refresh_library()
# can always rebuild buttons fresh from this instead of holding onto a
# widget reference across a long-running background thread.
active_sessions = {}

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            exe_path TEXT,
            save_path TEXT,
            last_played TEXT,
            total_minutes INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def add_game_to_db(name, exe_path, save_path):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO games (name, exe_path, save_path) VALUES (?, ?, ?)",
        (name, exe_path, save_path)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id

def get_all_games():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, exe_path, save_path, last_played, total_minutes FROM games")
    rows = cursor.fetchall()
    conn.close()
    return rows

def record_session(game_id, minutes_played):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE games SET last_played = ?, total_minutes = total_minutes + ? WHERE id = ?",
        (now_str, minutes_played, game_id)
    )
    conn.commit()
    conn.close()


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


# =========================================================================
# SAVE BACKUP SYSTEM - safety rules, read this before changing anything:
#
#   1. The LIVE save folder (wherever the game actually keeps its save)
#      is NEVER deleted or overwritten automatically. Ever.
#   2. Backups only ever copy OUT of the live folder, into our own
#      storage under SaveBackups/. That copy can freely overwrite our
#      OWN previous backup - that's our storage, that's safe.
#   3. The only time anything is written INTO the live folder is if that
#      folder is completely missing or empty. If it has even one file
#      in it already, we leave it alone, full stop.
# =========================================================================
BACKUPS_ROOT = "SaveBackups"

# =========================================================================
# SAVE LOCATION AUTO-DETECTION
# Uses the public Ludusavi manifest (community-maintained database of where
# thousands of games store their saves) to suggest a save folder.
# This ONLY ever fills in a text field for the user to review - it never
# writes, copies, or deletes anything on its own.
# =========================================================================
MANIFEST_URL = "https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/master/data/manifest.yaml"
MANIFEST_CACHE_FILE = "ludusavi_manifest_cache.yaml"

_manifest_data = None  # loaded once per app run, cached in memory

def ensure_manifest_downloaded():
    if not os.path.exists(MANIFEST_CACHE_FILE):
        print("[DEBUG] Downloading save-location database (first time only)...")
        try:
            # 30 second timeout - if the network stalls, this fails fast
            # instead of hanging forever with no feedback.
            with urllib.request.urlopen(MANIFEST_URL, timeout=30) as response:
                data = response.read()
            with open(MANIFEST_CACHE_FILE, "wb") as f:
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

    # The C-accelerated loader (CSafeLoader) is dramatically faster than the
    # pure-Python one for a file this size. Fall back if it's not available.
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    print(f"[DEBUG] Parsing save-location database using {loader.__name__}...")
    with open(MANIFEST_CACHE_FILE, "r", encoding="utf-8") as f:
        _manifest_data = yaml.load(f, Loader=loader)
    print(f"[DEBUG] Parsed {len(_manifest_data)} game entries.")
    return _manifest_data

def _resolve_placeholders(template):
    """Turns a manifest path template into a real glob pattern for this PC.
    Returns None if the template can't be safely resolved (e.g. it points
    inside the game's install folder, which we never want to suggest)."""
    if "<base>" in template or "<root>" in template:
        return None  # would point inside the install folder - never suggest this

    replacements = {
        "<home>": str(Path.home()),
        "<winAppData>": os.environ.get("APPDATA", ""),
        "<winLocalAppData>": os.environ.get("LOCALAPPDATA", ""),
        "<winDocuments>": str(Path.home() / "Documents"),
        "<winProgramData>": os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        "<storeUserId>": "*",  # varies per install - treat as wildcard
    }
    resolved = template
    for placeholder, value in replacements.items():
        resolved = resolved.replace(placeholder, value)

    if "<" in resolved:
        return None  # some placeholder we don't handle - skip rather than guess wrong

    return resolved

def find_save_candidates(game_name):
    """Looks up a game by name in the manifest and returns a list of real,
    existing folders on this PC that likely contain its save data."""
    manifest = load_manifest()
    if not manifest:
        return []

    # Try an exact (case-insensitive) match first, then a loose contains-match.
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


# =========================================================================
# COVER ART
# Fetches box art from SteamGridDB and caches it locally under CoverArt/.
# This only ever writes small image files into that one folder - it never
# reads from or writes to anything related to your actual games.
# =========================================================================
STEAMGRIDDB_API_KEY = "ef10bd125bc7253fe8a686b5cdae6314"
STEAMGRIDDB_BASE = "https://www.steamgriddb.com/api/v2"
COVER_ART_ROOT = "CoverArt"

import urllib.error
import time

def log(msg):
    """Prints AND writes to debug_log.txt - so we always have a reliable,
    complete record even if the console itself drops or wraps something."""
    print(msg)
    try:
        with open("debug_log.txt", "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

def _sgdb_request(url, retry=True):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {STEAMGRIDDB_API_KEY}",
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
    os.makedirs(COVER_ART_ROOT, exist_ok=True)
    return os.path.join(COVER_ART_ROOT, f"{game_id}_{sanitize_name(name)}.png")

def fetch_cover_art(game_id, name):
    """Looks up box art on SteamGridDB and saves it to CoverArt/ if not
    already cached. Returns True if art is available locally afterward."""
    dest_path = get_cover_art_path(game_id, name)
    if os.path.exists(dest_path):
        return True  # already cached, nothing to do

    try:
        log(f"[DEBUG] Searching cover art for: {name}")
        time.sleep(1)  # be gentle on the API - avoids tripping any rate limit
        search_url = f"{STEAMGRIDDB_BASE}/search/autocomplete/{urllib.parse.quote(name)}"
        result = _sgdb_request(search_url)

        if not result.get("success") or not result.get("data"):
            log(f"[DEBUG] No SteamGridDB match found for: {name}")
            return False

        sgdb_game_id = result["data"][0]["id"]

        grids_url = f"{STEAMGRIDDB_BASE}/grids/game/{sgdb_game_id}?dimensions=600x900"
        grids_result = _sgdb_request(grids_url)

        if not grids_result.get("success") or not grids_result.get("data"):
            log(f"[DEBUG] No cover art images found for: {name}")
            return False

        image_url = grids_result["data"][0]["url"]
        log(f"[DEBUG] Downloading image: {image_url}")
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
            log(f"[DEBUG] Image download HTTP {e.code} for {name} - URL: {image_url}")
            return False

        with open(dest_path, "wb") as f:
            f.write(image_bytes)
        log(f"[DEBUG] Saved cover art -> {dest_path}")
        return True

    except Exception as e:
        log(f"[DEBUG] Cover art fetch failed for {name}: {e}")
        return False


def sanitize_name(name):
    import re
    return re.sub(r'[^A-Za-z0-9\-]+', '-', name).strip('-')

def get_backup_dir(game_id, name):
    folder_name = f"{game_id}_{sanitize_name(name)}"
    path = os.path.join(BACKUPS_ROOT, folder_name, "backup")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def backup_save(game_id, name, save_path):
    """Copy OUT from the live save folder into our own backup storage.
    Only ever touches our own backup folder, never the live one."""
    if not save_path or not os.path.isdir(save_path):
        print(f"[DEBUG] Skipping backup - save folder not found: {save_path}")
        return
    if not os.listdir(save_path):
        print(f"[DEBUG] Skipping backup - save folder is empty: {save_path}")
        return

    backup_dir = get_backup_dir(game_id, name)
    try:
        if os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir)  # safe: this is OUR storage, not the live folder
        shutil.copytree(save_path, backup_dir)
        print(f"[DEBUG] Backed up save -> {backup_dir}")
    except Exception as e:
        print(f"[DEBUG] Backup failed: {e}")

def restore_if_missing(game_id, name, save_path):
    """Only writes into the live save folder if it's missing or empty.
    If it already has files, this does nothing - guaranteed."""
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


def paths_conflict(save_path, exe_path):
    """Returns True if save_path is the exe's folder, a parent of it,
    or otherwise overlaps with it - this is the check that would have
    caught the Sekiro mistake before it happened."""
    if not save_path or not exe_path:
        return False
    save_norm = os.path.normcase(os.path.normpath(os.path.abspath(save_path)))
    exe_folder_norm = os.path.normcase(os.path.normpath(os.path.abspath(os.path.dirname(exe_path))))
    return save_norm == exe_folder_norm or exe_folder_norm.startswith(save_norm + os.sep)


# --- Add Game popup window ---
def open_add_game_form():
    form = ctk.CTkToplevel(app)
    form.title("Add Game")
    form.geometry("450x520")
    form.grab_set()

    ctk.CTkLabel(form, text="Game Name").pack(pady=(20, 5))
    name_entry = ctk.CTkEntry(form, width=350, placeholder_text="e.g. Sekiro")
    name_entry.pack()

    ctk.CTkLabel(form, text="Game .exe Location").pack(pady=(15, 5))
    exe_frame = ctk.CTkFrame(form, fg_color="transparent")
    exe_frame.pack()
    exe_entry = ctk.CTkEntry(exe_frame, width=270)
    exe_entry.pack(side="left", padx=(0, 5))

    def browse_exe():
        path = filedialog.askopenfilename(filetypes=[("Executable files", "*.exe")])
        if path:
            exe_entry.delete(0, "end")
            exe_entry.insert(0, path)

    ctk.CTkButton(exe_frame, text="Browse", width=70, command=browse_exe).pack(side="left")

    ctk.CTkLabel(form, text="Save Folder Location (optional)").pack(pady=(15, 5))
    ctk.CTkLabel(
        form,
        text="For offline single-player games only, so progress survives deletion.\n"
             "Leave this blank for online/live-service games (Genshin Impact,\n"
             "Wuthering Waves, etc.) - their progress lives on the developer's\n"
             "servers, not on your PC, so there's nothing here to back up.",
        font=("Arial", 10), text_color="gray", justify="center"
    ).pack()
    save_frame = ctk.CTkFrame(form, fg_color="transparent")
    save_frame.pack(pady=(5, 0))
    save_entry = ctk.CTkEntry(save_frame, width=200)
    save_entry.pack(side="left", padx=(0, 5))

    def browse_save():
        path = filedialog.askdirectory()
        if path:
            save_entry.delete(0, "end")
            save_entry.insert(0, path)

    ctk.CTkButton(save_frame, text="Browse", width=70, command=browse_save).pack(side="left", padx=(0, 5))

    detect_status = ctk.CTkLabel(form, text="", font=("Arial", 11), wraplength=380)
    detect_status.pack(pady=(5, 0))

    def run_auto_detect():
        name = name_entry.get().strip()
        if not name:
            detect_status.configure(text="Type the game's name first.", text_color="orange")
            return

        detect_status.configure(text="Searching...", text_color="gray")

        def search():
            try:
                candidates = find_save_candidates(name)
            except Exception as e:
                print(f"[DEBUG] Auto-detect failed: {e}")
                candidates = []

            def show_result():
                if not candidates:
                    detect_status.configure(
                        text="Couldn't find this game automatically - please browse manually.",
                        text_color="orange"
                    )
                elif len(candidates) == 1:
                    save_entry.delete(0, "end")
                    save_entry.insert(0, candidates[0])
                    detect_status.configure(text="Found it and filled in the field above.", text_color="lightgreen")
                else:
                    detect_status.configure(text="Found multiple - pick one:", text_color="lightgreen")
                    for path in candidates:
                        def pick(p=path):
                            save_entry.delete(0, "end")
                            save_entry.insert(0, p)
                        ctk.CTkButton(form, text=path, command=pick, font=("Arial", 10)).pack(pady=2)

            app.after(0, show_result)

        threading.Thread(target=search, daemon=True).start()

    ctk.CTkButton(save_frame, text="Auto-Detect", width=90, command=run_auto_detect).pack(side="left")

    error_label = ctk.CTkLabel(form, text="", text_color="red", wraplength=380)
    error_label.pack(pady=(10, 0))

    def submit():
        name = name_entry.get().strip()
        exe_path = exe_entry.get().strip()
        save_path = save_entry.get().strip()

        if not name or not exe_path:
            error_label.configure(text="Name and .exe path are required.")
            return

        if save_path and paths_conflict(save_path, exe_path):
            error_label.configure(
                text="That save folder is the game's install folder (or contains it). "
                     "This isn't allowed - point it at the real save location instead "
                     "(usually under AppData or Documents)."
            )
            return

        new_id = add_game_to_db(name, exe_path, save_path)
        form.destroy()
        refresh_library()

        def fetch_art():
            fetch_cover_art(new_id, name)
            app.after(0, refresh_library)
        threading.Thread(target=fetch_art, daemon=True).start()

    ctk.CTkButton(form, text="Save Game", command=submit).pack(pady=20)


# --- Launching a game and tracking the session ---
def launch_game(game_id, name, exe_path, save_path):
    if not exe_path or not os.path.exists(exe_path):
        print(f"[DEBUG] Launch aborted - exe not found at: {exe_path}")
        return

    if active_sessions.get(game_id):
        print(f"[DEBUG] Ignoring click - {name} is already running")
        return

    active_sessions[game_id] = True
    refresh_library()

    def run():
        game_folder = os.path.dirname(exe_path)
        start_time = datetime.datetime.now()
        print(f"[DEBUG] Launching: {exe_path}")

        # Only fills the save folder if it's missing/empty - never touches existing data.
        restore_if_missing(game_id, name, save_path)

        try:
            process = subprocess.Popen(exe_path, cwd=game_folder)
            print(f"[DEBUG] Process started with PID {process.pid}")
        except Exception as e:
            print(f"[DEBUG] Failed to launch process: {e}")
            active_sessions.pop(game_id, None)
            app.after(0, refresh_library)
            return

        import time
        time.sleep(2)
        if process.poll() is not None:
            print(f"[DEBUG] WARNING: process exited almost immediately (code {process.poll()}).")
            print("[DEBUG] Likely needs Administrator rights, or another copy is already running.")
            active_sessions.pop(game_id, None)
            app.after(0, refresh_library)
            return

        process.wait()
        minutes_played = max(1, int((datetime.datetime.now() - start_time).total_seconds() // 60))
        print(f"[DEBUG] Process exited after {minutes_played} min")

        # Only ever copies OUT of the live folder into our own backup storage.
        backup_save(game_id, name, save_path)
        record_session(game_id, minutes_played)

        def finish():
            active_sessions.pop(game_id, None)
            refresh_library()
        app.after(0, finish)

    threading.Thread(target=run, daemon=True).start()


# --- Library display ---
def refresh_library():
    for widget in library_frame.winfo_children():
        widget.destroy()

    games = get_all_games()

    if not games:
        ctk.CTkLabel(
            library_frame,
            text="No games added yet. Click '+ Add Game' to get started.",
            font=("Arial", 14)
        ).pack(pady=10)
        return

    for game in games:
        game_id, name, exe_path, save_path, last_played, total_minutes = game
        row = ctk.CTkFrame(library_frame)
        row.pack(fill="x", padx=10, pady=5)

        cover_path = get_cover_art_path(game_id, name)
        if os.path.exists(cover_path):
            try:
                pil_img = Image.open(cover_path)
                thumb = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(40, 60))
                ctk.CTkLabel(row, image=thumb, text="").pack(side="left", padx=(10, 5), pady=5)
            except Exception as e:
                print(f"[DEBUG] Failed to load cover art thumbnail for {name}: {e}")

        ctk.CTkLabel(row, text=name, font=("Arial", 16, "bold")).pack(side="left", padx=10, pady=10)
        ctk.CTkLabel(row, text=f"Last played: {time_ago(last_played)}").pack(side="left", padx=10)
        ctk.CTkLabel(row, text=f"Total time: {total_minutes} min").pack(side="left", padx=10)

        launch_button = ctk.CTkButton(row, text="Launch", width=90)
        if active_sessions.get(game_id):
            launch_button.configure(state="disabled", text="Running", fg_color="gray40")
        else:
            launch_button.configure(
                command=lambda gid=game_id, n=name, path=exe_path, sp=save_path:
                    launch_game(gid, n, path, sp)
            )
        launch_button.pack(side="right", padx=10)


# --- App setup ---
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

init_db()

app = ctk.CTk()
app.title("My Game Library")
app.geometry("900x600")

ctk.CTkLabel(app, text="Your Library", font=("Arial", 24, "bold")).pack(pady=20)
ctk.CTkButton(app, text="+ Add Game", width=150, height=40, command=open_add_game_form).pack(pady=10)

library_frame = ctk.CTkScrollableFrame(app, fg_color="transparent")
library_frame.pack(fill="both", expand=True, padx=20, pady=10)

refresh_library()

def fetch_missing_cover_art():
    for game in get_all_games():
        game_id, name, exe_path, save_path, last_played, total_minutes = game
        cover_path = get_cover_art_path(game_id, name)
        if not os.path.exists(cover_path):
            fetch_cover_art(game_id, name)
            app.after(0, refresh_library)

threading.Thread(target=fetch_missing_cover_art, daemon=True).start()

def periodic_refresh():
    refresh_library()
    app.after(30000, periodic_refresh)

app.after(30000, periodic_refresh)

app.mainloop()
