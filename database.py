import sqlite3
import datetime
import config

def init_db():
    conn = sqlite3.connect(config.DB_FILE)
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
    conn = sqlite3.connect(config.DB_FILE)
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
    conn = sqlite3.connect(config.DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, exe_path, save_path, last_played, total_minutes FROM games")
    rows = cursor.fetchall()
    conn.close()
    return rows

def record_session(game_id, minutes_played):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(config.DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE games SET last_played = ?, total_minutes = total_minutes + ? WHERE id = ?",
        (now_str, minutes_played, game_id)
    )
    conn.commit()
    conn.close()