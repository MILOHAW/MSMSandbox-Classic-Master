import sqlite3
import time
import platform
import os

dev = platform.system() == "Windows"

suffix = "dev" if dev else "prod"

# Use absolute path to ensure database is in the project root
script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_file_name = os.path.join(script_dir, f"player_data_{suffix}.db")

def _connect_player_db() -> sqlite3.Connection:
    conn = sqlite3.connect(db_file_name, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _backup_corrupt_db() -> None:
    if not os.path.exists(db_file_name):
        return

    backup_name = f"{db_file_name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(db_file_name, backup_name)
        print(f"[!] Detected corrupt player database; backed up to {backup_name}")
    except OSError as exc:
        print(f"[!] Detected corrupt player database, but failed to back it up: {exc}")


try:
    db_player = _connect_player_db()
    try:
        db_player.execute("PRAGMA journal_mode=WAL;")
        db_player.commit()

        try:
            db_player.execute("UPDATE player_monsters SET volume = 1.0 WHERE volume IS NULL OR volume = 0")
            db_player.execute("UPDATE player_monsters SET level = 1 WHERE level IS NULL OR level <= 0")
            db_player.commit()
        except sqlite3.OperationalError:
            # Table may not exist yet; ignore until later initialization.
            pass
    except sqlite3.DatabaseError:
        db_player.close()
        _backup_corrupt_db()
        db_player = _connect_player_db()
        db_player.execute("PRAGMA journal_mode=WAL;")
        db_player.commit()
except sqlite3.DatabaseError:
    # Re-raise unexpected failures after the recovery attempt.
    raise

cur_player = db_player.cursor()

def get_db() -> sqlite3.Connection:
    return db_player