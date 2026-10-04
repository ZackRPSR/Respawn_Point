import sqlite3
import datetime
import os
import sys
import config

def get_db_path():
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "library.db")

DB_PATH = get_db_path()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            exe_path TEXT,
            save_path TEXT,
            last_played TEXT,
            total_minutes INTEGER DEFAULT 0,
            launch_args TEXT DEFAULT ''
        )
    """)
    # Migration for libraries created before launch arguments existed: add the column
    # only if it's missing. Existing rows (and their playtime stats) are untouched.
    cursor.execute("PRAGMA table_info(games)")
    existing_columns = [row[1] for row in cursor.fetchall()]
    if "launch_args" not in existing_columns:
        cursor.execute("ALTER TABLE games ADD COLUMN launch_args TEXT DEFAULT ''")
    conn.commit()
    conn.close()

def add_game_to_db(name, exe_path, save_path, launch_args=""):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO games (name, exe_path, save_path, launch_args) VALUES (?, ?, ?, ?)",
        (name, exe_path, save_path, launch_args or "")
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id

def get_all_games():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, exe_path, save_path, last_played, total_minutes FROM games")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_launch_args(game_id):
    """Returns the launch-arguments string for one game ('' if none). Kept separate from
    get_all_games() on purpose so that function's 6-column tuple shape (unpacked in many
    places in main.py) never changes."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT launch_args FROM games WHERE id = ?", (game_id,))
    row = cursor.fetchone()
    conn.close()
    if row is None or row[0] is None:
        return ""
    return row[0]

def set_launch_args(game_id, launch_args):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE games SET launch_args = ? WHERE id = ?",
        ((launch_args or "").strip(), game_id)
    )
    conn.commit()
    conn.close()

def update_game(game_id, name, exe_path, save_path):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE games SET name = ?, exe_path = ?, save_path = ? WHERE id = ?",
        (name, exe_path, save_path, game_id)
    )
    conn.commit()
    conn.close()

def delete_game(game_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM games WHERE id = ?", (game_id,))
    conn.commit()
    conn.close()

def record_session(game_id, minutes_played):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE games SET last_played = ?, total_minutes = total_minutes + ? WHERE id = ?",
        (now_str, minutes_played, game_id)
    )
    conn.commit()
    conn.close()

def merge_restored_stats(game_id, backup_last_played, backup_total_minutes):
    """Non-destructively folds a backed-up game's stats into its current row, matching
    the app's never-clobber-existing-progress rule elsewhere: total_minutes takes
    whichever of the two is higher (a restore can never make your playtime go DOWN),
    and last_played takes whichever timestamp is more recent (falling back to the
    backup's value only if this game has never been played locally yet)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT last_played, total_minutes FROM games WHERE id = ?", (game_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return

    current_last_played, current_total_minutes = row
    new_total = max(current_total_minutes or 0, backup_total_minutes or 0)

    new_last_played = current_last_played
    if backup_last_played:
        if not current_last_played:
            new_last_played = backup_last_played
        else:
            try:
                cur_dt = datetime.datetime.strptime(current_last_played, "%Y-%m-%d %H:%M:%S")
                bak_dt = datetime.datetime.strptime(backup_last_played, "%Y-%m-%d %H:%M:%S")
                if bak_dt > cur_dt:
                    new_last_played = backup_last_played
            except ValueError:
                pass

    cursor.execute(
        "UPDATE games SET last_played = ?, total_minutes = ? WHERE id = ?",
        (new_last_played, new_total, game_id)
    )
    conn.commit()
    conn.close()