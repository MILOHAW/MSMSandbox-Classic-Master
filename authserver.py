#
# authserver.py
# fuck you riot
#

from flask import Flask, request, jsonify, send_from_directory, abort
import logging
import sqlite3
import hashlib
import time
import random
import os
import sys
import json
import bcrypt
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from tools.database import get_db, db_player  # type: ignore
from tools.utils import encrypt, get_config_value


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
)

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.INFO)


@app.before_request
def log_request():
    app.logger.info(
        f"{request.remote_addr} {request.method} {request.path} args={request.args.to_dict()}"
    )


CONTENT_ROOT = os.path.join(os.path.dirname(__file__), "files")

IV = get_config_value("iv")
KEY = get_config_value("key")

GAME_SERVER_IP = "10.128.0.3"

PENDING_COMMANDS = []
PENDING_COMMANDS_LOCK = threading.Lock()


def error_response(message: str, status: int = 400):
    return jsonify({"ok": False, "message": message}), status


def table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


def get_friends(cur: sqlite3.Cursor, bbb_id: int) -> list[int]:
    cur.execute(
        "SELECT user_1, user_2 FROM user_friends WHERE user_1 = ? OR user_2 = ?",
        (bbb_id, bbb_id),
    )
    rows = cur.fetchall()

    # always include self
    friends = [1]
    for u1, u2 in rows:
        friends.append(u2 if u1 == bbb_id else u1)
    return friends


def make_access_token(username: str, login_type: str, client_version: str) -> str:
    payload = {
        "username": username,
        "login_type": login_type,
        "client_version": client_version,
        "issued_at": int(time.time()),
    }
    return encrypt(json.dumps(payload), IV, KEY)


# fmt: off
_TABLES = {
    "users": """
        CREATE TABLE users (
            bbb_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username     TEXT    NOT NULL UNIQUE,
            password     TEXT    NOT NULL,
            date_created INTEGER,
            mac_address  TEXT    NOT NULL,
            ip           TEXT
        )
    """,
    "user_friends": """
        CREATE TABLE user_friends (
            user_1 INTEGER,
            user_2 INTEGER
        )
    """,
    "players": """
        CREATE TABLE players (
            bbb_id       INTEGER PRIMARY KEY,
            active_island INTEGER,
            coins        INTEGER DEFAULT 0,
            food         INTEGER DEFAULT 0,
            diamonds     INTEGER DEFAULT 0,
            shards       INTEGER DEFAULT 0,
            xp           INTEGER DEFAULT 0,
            level        INTEGER DEFAULT 1,
            last_login   INTEGER,
            display_name TEXT
        )
    """,
    "player_islands": """
        CREATE TABLE player_islands (
            user_island_id INTEGER PRIMARY KEY AUTOINCREMENT,
            bbb_id         INTEGER,
            date_created   INTEGER,
            likes          INTEGER DEFAULT 0,
            dislikes       INTEGER DEFAULT 0,
            island_id      INTEGER,
            warp_speed     REAL    DEFAULT 1.0,
            FOREIGN KEY(bbb_id) REFERENCES users(bbb_id)
        )
    """,
    "player_monsters": """
        CREATE TABLE player_monsters (
            user_monster_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_island_id   INTEGER,
            pos_x            INTEGER,
            pos_y            INTEGER,
            flip             INTEGER DEFAULT 0,
            muted            INTEGER DEFAULT 0,
            level            INTEGER DEFAULT 1,
            date_created     INTEGER,
            happiness        INTEGER DEFAULT 50,
            monster          INTEGER,
            volume           REAL    DEFAULT 1.0,
            times_fed        INTEGER DEFAULT 0,
            collected_coins  INTEGER DEFAULT 0,
            last_collection  INTEGER,
            FOREIGN KEY(user_island_id) REFERENCES player_islands(user_island_id)
        )
    """,
    "player_gi_monsters": """
        CREATE TABLE player_gi_monsters (
            user_monster_id  INTEGER PRIMARY KEY,
            monster_parent_id INTEGER,
            island_parent_id  INTEGER,
            pos_x            INTEGER,
            pos_y            INTEGER,
            flip             INTEGER DEFAULT 0,
            muted            INTEGER DEFAULT 0,
            date_created     INTEGER,
            bbb_id           INTEGER,
            FOREIGN KEY(user_monster_id) REFERENCES player_monsters(user_monster_id)
                ON DELETE CASCADE
        )
    """,
    "player_structures": """
        CREATE TABLE player_structures (
            user_structure_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_island_id    INTEGER,
            date_created      INTEGER,
            pos_x             INTEGER,
            pos_y             INTEGER,
            flip              INTEGER DEFAULT 0,
            muted             INTEGER DEFAULT 0,
            is_complete       INTEGER DEFAULT 0,
            is_upgrading      INTEGER DEFAULT 0,
            structure         INTEGER,
            scale             REAL    DEFAULT 1.0,
            building_completed INTEGER,
            last_collection   INTEGER,
            obj_data          INTEGER,
            obj_end           INTEGER
        )
    """,
    "player_eggs": """
        CREATE TABLE player_eggs (
            user_egg_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_island_id   INTEGER,
            laid_on          INTEGER,
            hatches_on       INTEGER,
            monster          INTEGER,
            user_structure_id INTEGER,
            FOREIGN KEY(user_island_id) REFERENCES player_islands(user_island_id)
        )
    """,
    "player_breeding": """
        CREATE TABLE player_breeding (
            user_breeding_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_island_id   INTEGER,
            started_on       INTEGER,
            completes_on     INTEGER,
            result           INTEGER NOT NULL,
            monster_1        INTEGER,
            monster_2        INTEGER,
            user_structure_id INTEGER,
            FOREIGN KEY(user_island_id) REFERENCES player_islands(user_island_id)
        )
    """,
    "monster_mega_data": """
        CREATE TABLE monster_mega_data (
            user_monster_id INTEGER PRIMARY KEY,
            started_at      INTEGER,
            finishes_at     INTEGER,
            permamega       INTEGER,
            currently_mega  INTEGER,
            FOREIGN KEY(user_monster_id) REFERENCES player_monsters(user_monster_id)
        )
    """,
}


def create_player_tables():
    cur = db_player.cursor()
    for name, ddl in _TABLES.items():
        if not table_exists(cur, name):
            cur.execute(ddl)
            print(f"[+] table '{name}' created")

    db_player.commit()

# remove the comments to reset use currencies to 199,999,999 each time the auth server is started
#def reset_all_player_stats(value: int = 199_999_999) -> None:
#    cur = db_player.cursor()
#    try:
#        cur.execute(
#            """
#            UPDATE players SET coins = ?, food = ?, diamonds = ?, shards = ?, xp = ?
#            """,
#            (value, value, value, value, value),
#        )
#        db_player.commit()
#        print(f"[+] reset all player stats to {value}")
#    except Exception as e:
#        print(f"[!] failed to reset player stats: {e}")


@app.route("/auth.php", methods=["GET"])
def auth():
    q = request.args
    username = q.get("u", "").strip()
    password = q.get("p", "")
    login_type = q.get("t", "")
    client_version = q.get("client_version", "")
    ip = request.remote_addr

    if not username or not password:
        return error_response("Username and password are required.")
    if len(username) > 64 or len(password) > 128:
        return error_response("Username or password too long.")

    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT bbb_id, password FROM users WHERE username = ?", (username,))
    user = cur.fetchone()

    if user:
        bbb_id, stored_hash = user
        if not check_password(password, stored_hash):
            return jsonify({"ok": False, "acc_exists": True, "message": "Incorrect password"}), 401
    else:
        hashed = hash_password(password)
        cur.execute(
            "INSERT INTO users (username, password, date_created, mac_address, ip) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, hashed, int(time.time()), "00:00:00:00:00:00", ip),
        )
        bbb_id = cur.lastrowid
        db.commit()

    access_token = make_access_token(username, login_type, client_version)

    host = request.host.split(":")[0]
    return jsonify({
        "ok": True,
        "acc_exists": True,
        "sessId": access_token,
        "bbbId": bbb_id,
        "username": username,
        "serverIp": host,
        "login_type": login_type,
        "contentUrl": f"http://{host}:900/content/{client_version}/files.json",
        "friends": get_friends(cur, bbb_id),
        "auto_login": True,
    })


@app.route("/verify_user", methods=["POST"])
def verify_user():
    data = request.get_json(silent=True)
    if not data:
        return error_response("Invalid or missing JSON body.")

    bbb_id = data.get("bbb_id")
    game_id = data.get("game_id")

    if bbb_id is None:
        return error_response("Missing required field: bbb_id.")

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT bbb_id FROM users WHERE bbb_id = ?", (bbb_id,))
    if not cur.fetchone():
        return error_response("User not found.", status=404)

    session_payload = {
        "user_id": bbb_id,
        "game_id": game_id,
        "timestamp": int(time.time()),
    }
    session_id = encrypt(json.dumps(session_payload), IV, KEY)

    return jsonify({"ok": True, "session_id": session_id})


@app.route("/command/gs_display_generic_message", methods=["POST"])
def command_gs_display_generic_message():
    data = request.get_json(silent=True)
    if not data:
        return error_response("Invalid or missing JSON body.")

    message = str(data.get("message", "")).strip()
    if not message:
        return error_response("Missing required field: message.")

    force_logout = bool(data.get("force_logout", False))
    target_bbb_id = data.get("bbb_id")
    if target_bbb_id is not None:
        try:
            target_bbb_id = int(target_bbb_id)
        except (TypeError, ValueError):
            return error_response("bbb_id must be an integer.")

    queue_display_message(message, force_logout=force_logout, target_bbb_id=target_bbb_id)

    return jsonify({"ok": True, "queued": True, "command": {
        "command": "gs_display_generic_message",
        "payload": {
            "force_logout": force_logout,
            "msg": message,
        },
        "target_bbb_id": target_bbb_id,
    }})


@app.route("/commands", methods=["GET"])
def get_pending_commands():
    with PENDING_COMMANDS_LOCK:
        commands = list(PENDING_COMMANDS)
        PENDING_COMMANDS.clear()
    return jsonify({"ok": True, "commands": commands})


def resolve_content_root(ver: str) -> str | None:
    version_root = os.path.join(CONTENT_ROOT, ver)
    if os.path.isdir(version_root):
        return version_root

    available_versions = [
        d for d in os.listdir(CONTENT_ROOT)
        if os.path.isdir(os.path.join(CONTENT_ROOT, d))
    ]
    if not available_versions:
        return None

    fallback_version = sorted(available_versions, reverse=True)[0]
    fallback_root = os.path.join(CONTENT_ROOT, fallback_version)
    print(f"[!] requested content version '{ver}' not found; using fallback version '{fallback_version}'")
    return fallback_root


@app.route("/content/<ver>/files.json", methods=["GET"])
def get_updates(ver: str):
    version_root = resolve_content_root(ver)
    if version_root is None:
        return error_response("Unknown content version and no fallback available.", status=404)

    files_list = []
    for root, _dirs, files in os.walk(version_root):
        for filename in files:
            full_path = os.path.join(root, filename)
            relative_path = os.path.relpath(full_path, version_root)
            files_list.append({
                "localName": relative_path,
                "serverName": relative_path,
                "checksum": md5_file(full_path),
            })

    return jsonify(files_list)


@app.route("/content/<ver>/<path:filename>", methods=["GET"])
def serve_file(ver: str, filename: str):
    filename = filename.replace("\\", "/")
    version_root = resolve_content_root(ver)
    if version_root is None:
        abort(404)

    full_path = os.path.join(version_root, filename)
    if not os.path.isfile(full_path):
        abort(404)

    return send_from_directory(version_root, filename)


@app.route("/logging_in.mp4", methods=["GET"])
def serve_loading_screen_mp4():
    """Serve loading screen as MP4 video instead of static image."""
    version_dirs = sorted(os.listdir(CONTENT_ROOT), reverse=True)
    for ver in version_dirs:
        version_root = os.path.join(CONTENT_ROOT, ver)
        mp4_path = os.path.join(version_root, "gfx", "BigFishSplashScreen.mp4")
        if os.path.isfile(mp4_path):
            return send_from_directory(os.path.dirname(mp4_path), "BigFishSplashScreen.mp4")
    
    abort(404)


def queue_display_message(message: str, force_logout: bool = False, target_bbb_id: int | None = None):
    command = {
        "command": "gs_display_generic_message",
        "payload": {
            "force_logout": force_logout,
            "msg": message,
        },
        "target_bbb_id": target_bbb_id,
    }
    with PENDING_COMMANDS_LOCK:
        PENDING_COMMANDS.append(command)


def command_input_loop() -> None:
    print("[+] authserver console: type 'send <message>' to queue a gs_display_generic_message")
    while True:
        try:
            raw = input().strip()
        except EOFError:
            break

        if not raw:
            continue

        if raw.lower().startswith("send "):
            message = raw[5:].strip()
            if message:
                queue_display_message(message)
                print(f"[+] queued message: {message}")
            else:
                print("[!] message cannot be empty")
        else:
            print("[!] unknown command. Use 'send <message>'")


if __name__ == "__main__":
    if not os.path.isdir(CONTENT_ROOT):
        os.makedirs(CONTENT_ROOT, exist_ok=True)
        print(f"[!] created missing content root: {CONTENT_ROOT}")

    create_player_tables()
    # Reset all player resources to the high value on auth server start
    #reset_all_player_stats()
    if sys.stdin is not None and sys.stdin.isatty():
        threading.Thread(target=command_input_loop, daemon=True).start()
    else:
        print("[!] stdin is not available; authserver console input disabled")

    try:
        app.run(host=GAME_SERVER_IP, port=900, debug=False, use_reloader=False)
    except OSError as e:
        print(f"[!] failed to bind auth server to {GAME_SERVER_IP}: {e}; falling back to 0.0.0.0")
        try:
            app.run(host="0.0.0.0", port=900, debug=False, use_reloader=False)
        except OSError as e2:
            print(f"[!] failed to bind auth server to 0.0.0.0: {e2}")
            print("[!] authserver could not bind to any host; verify network interface and permissions")
