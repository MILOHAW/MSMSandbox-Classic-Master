from sfs2x.transport import server_from_url, TCPTransport
from sfs2x.protocol import Message, ControllerID, SysAction
from sfs2x.core import SFSObject, SFSArray

import hashlib
import asyncio
import time
import traceback
import requests
import base64
import os
import platform
import json
import random
import threading

from flask import g

from room import Room

from msmdata.player import Player
from msmdata.island import Island, cur as static_cur
from msmdata.structure import Structure
from msmdata.egg import Egg # type: ignore
from msmdata.monster import Monster, get_monster_position_state, record_monster_position #type: ignore
from msmdata.megadata import MegaData # type: ignore
from msmdata.breeding import Breeding # type: ignore

from msmdata.get_data import *

from tools.utils import player_exists, sanitize_name, normalize_text, invalid_name, decrypt, get_config_value, send_extension_response

from tools.database import cur_player, db_player # type: ignore

CURRENT_PLAYERS = 0
MAX_PLAYERS = 1000

KICK_IF_OUTDATED = True
GAME_SERVER_IP = "0.0.0.0"
AUTH_SERVER_IP = "10.128.0.3"

EVENT_LOOP = None
ADMIN_CURRENCIES = {"coins", "food", "diamonds", "shards", "xp", "level"}

GOLD_ISLAND_ID = 6
ETHEREAL_ISLAND_ID = 7
SHUGGA_ISLAND_ID = 8

CRYPT_KEY = get_config_value("key")
CRYPT_IV = get_config_value("iv")

dev = platform.system() == "Windows"

if os.path.exists("player_data.db") and not os.path.exists("player_data_prod.db"):
    os.rename("player_data.db", "player_data_prod.db")

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!¨\"#$&'()*+,-./:;<=>?@}{0123456789|£©¿®`~^ÀÁÂÄÇÈÉÊËÌÍÎÏÑÒÓÔÖÙÚÛÜßàáâäçèéêëìíîïñòóôöùúûü_ÆæÃãÕõАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя€₽¡"

if platform.system() == "Windows":
    KICK_IF_OUTDATED = False
    AUTH_SERVER_IP = GAME_SERVER_IP
    GAME_SERVER_IP = AUTH_SERVER_IP

DATA_CACHE = {}


CONNECTED_CLIENTS = {}

POLL_INTERVAL = 5.0          
IDLE_POLL_INTERVAL = 15.0    


def get_connected_player_summary() -> str:
    if not CONNECTED_CLIENTS:
        return "No connected players."

    lines = ["Connected players:"]
    for player_id, client in CONNECTED_CLIENTS.items():
        player = getattr(client, "player", None)
        name = player.display_name if player is not None else "unknown"
        lines.append(f"  {player_id}: {name}")
    return "\n".join(lines)


async def send_player_properties_update(client):
    if not hasattr(client, "player") or client.player is None:
        return

    response = SFSObject()
    response.put_sfs_array("properties", client.player.get_properties())
    await send_extension_response(client, "gs_update_properties", response)


def refresh_cached_monster(client, monster):
    if not hasattr(client, "player") or client.player is None:
        return
    client.player.refresh_monster(monster)
    record_monster_position(monster.user_island_id, monster.user_monster_id, monster.x, monster.y)


async def auto_update_monster_names(client):
    """Removed: no longer forcibly renames monsters."""
    pass


async def handle_pending_command(command):
    command_name = str(command.get("command") or "").strip().lower()
    payload = command.get("payload", {})
    target_id = command.get("target_bbb_id")

    if command_name == "gs_display_generic_message":
        msg = SFSObject()
        msg.put_bool("force_logout", payload.get("force_logout", False))
        msg.put_utf_string("msg", payload.get("msg", ""))

        if target_id is not None and target_id in CONNECTED_CLIENTS:
            client = CONNECTED_CLIENTS[target_id]
            await send_extension_response(client, "gs_display_generic_message", msg)
        else:
            for client in list(CONNECTED_CLIENTS.values()):
                await send_extension_response(client, "gs_display_generic_message", msg)
        return

    if command_name == "give_currency":
        if target_id is None:
            print("[!] pending give_currency missing target_bbb_id")
            return

        currency = payload.get("currency")
        amount = payload.get("amount")
        if currency not in {"coins", "food", "diamonds", "shards", "xp", "level"}:
            print(f"[!] invalid currency '{currency}' in pending command")
            return

        try:
            amount = int(amount)
        except (TypeError, ValueError):
            print("[!] invalid amount in pending give_currency command")
            return

        client = CONNECTED_CLIENTS.get(target_id)
        if client is not None and hasattr(client, "player") and client.player is not None:
            success = client.player.add_properties(**{currency: amount})
            if success:
                await send_player_properties_update(client)
                print(f"[+] applied give_currency for connected player {target_id}: {amount} {currency}")
            else:
                print(f"[!] failed to apply give_currency for connected player {target_id}")
            return

        # Fallback for offline players: update DB directly.
        cur_player.execute(
            f"UPDATE players SET {currency} = {currency} + ? WHERE bbb_id = ?",
            (amount, target_id)
        )
        db_player.commit()
        if cur_player.rowcount == 0:
            print(f"[!] offline player {target_id} not found for give_currency")
        else:
            print(f"[+] updated offline player {target_id}: {amount} {currency}")
        return

    print(f"[!] unknown pending command: {command_name}")


async def poll_pending_commands():
    while True:
        try:
            response = requests.get(f"http://{AUTH_SERVER_IP}:900/commands", timeout=5)
            response.raise_for_status()
            data = response.json()
            commands = data.get("commands", [])
        except Exception as e:
            print(f"[!] failed to poll pending commands: {e}")
            await asyncio.sleep(POLL_INTERVAL)
            continue

        for command in commands:
            try:
                await handle_pending_command(command)
            except Exception as e:
                print(f"[!] error handling pending command {command}: {e}")

        await asyncio.sleep(POLL_INTERVAL)


def admin_console_loop() -> None:
    print("[+] admin console commands:")
    print("    show userid                        - list connected players by bbb_id")
    print("    give <id> <currency> <value>      - add currency to a connected player")

    while True:
        try:
            raw = input().strip()
        except EOFError:
            break

        if not raw:
            continue

        tokens = raw.split()
        command = tokens[0].lower()

        if raw.lower() == "show userid":
            print(get_connected_player_summary())
            continue

        if command == "give" and len(tokens) == 4:
            try:
                player_id = int(tokens[1])
            except ValueError:
                print("[!] Player id must be an integer")
                continue

            currency = tokens[2].lower()
            if currency not in ADMIN_CURRENCIES:
                print(f"[!] Unsupported currency '{currency}'. Allowed: {', '.join(sorted(ADMIN_CURRENCIES))}")
                continue

            try:
                amount = int(tokens[3])
            except ValueError:
                print("[!] Value must be an integer")
                continue

            client = CONNECTED_CLIENTS.get(player_id)
            if client is None:
                print(f"[!] Player {player_id} is not currently connected")
                continue

            success = client.player.add_properties(**{currency: amount})
            if not success:
                print(f"[!] Failed to add {currency} to player {player_id}")
                continue

            try:
                if EVENT_LOOP is not None:
                    future = asyncio.run_coroutine_threadsafe(send_player_properties_update(client), EVENT_LOOP)
                    future.result(timeout=5)
            except Exception as exc:
                print(f"[!] Currency granted but failed to send update: {exc}")
                continue

            print(f"[+] Granted {amount} {currency} to player {player_id}")
            continue

        print("[!] unknown command. Use 'show userid' or 'give <id> <currency> <value>'")


def table_exists(cursor, table_name):
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name=?
    """, (table_name,))
    return cursor.fetchone() is not None

def table_has_column(cursor, table_name, column_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    rows = cursor.fetchall()
    return any(row[1] == column_name for row in rows)


def get_monster_name(monster_row):
    if monster_row is None:
        return "Monster"
    try:
        if "name" in monster_row.keys():
            return monster_row["name"] or "Monster"
    except Exception:
        pass
    return "Monster"


def create_player_tables():
    tables = {
        "users": """
            CREATE TABLE users (
                bbb_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                date_created INTEGER,
                mac_address TEXT NOT NULL,
                ip TEXT
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
                bbb_id INTEGER PRIMARY KEY,
                active_island INTEGER,
                coins INTEGER DEFAULT 0,
                food INTEGER DEFAULT 0,
                diamonds INTEGER DEFAULT 0,
                shards INTEGER DEFAULT 0,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                last_login INTEGER,
                display_name TEXT
            )
        """,
        "player_islands": """
            CREATE TABLE player_islands (
                user_island_id INTEGER PRIMARY KEY AUTOINCREMENT,
                bbb_id INTEGER,
                date_created INTEGER,
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                island_id INTEGER,
                warp_speed REAL DEFAULT 1.0,
                FOREIGN KEY(bbb_id) REFERENCES users(bbb_id)
            )
        """,
        "player_monsters": """
            CREATE TABLE player_monsters (
                user_monster_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_island_id INTEGER,
                pos_x INTEGER,
                pos_y INTEGER,
                flip INTEGER DEFAULT 0,
                muted INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                date_created INTEGER,
                happiness INTEGER DEFAULT 0,
                monster INTEGER,
                name TEXT DEFAULT 'Monster',
                volume REAL DEFAULT 1.0,
                times_fed INTEGER DEFAULT 0,
                collected_coins INTEGER DEFAULT 0,
                last_collection INTEGER,
                FOREIGN KEY(user_island_id) REFERENCES player_islands(user_island_id)
            )
        """,
        "player_gi_monsters": """
            CREATE TABLE player_gi_monsters (
                user_monster_id INTEGER PRIMARY KEY,
                monster_parent_id INTEGER,
                island_parent_id INTEGER,
                pos_x INTEGER,
                pos_y INTEGER,
                flip INTEGER DEFAULT 0,
                muted INTEGER DEFAULT 0,
                date_created INTEGER,
                bbb_id INTEGER,
                FOREIGN KEY(user_monster_id) REFERENCES player_monsters(user_monster_id)
                    ON DELETE CASCADE
            )
        """,
        "player_structures": """
            CREATE TABLE player_structures (
                user_structure_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_island_id INTEGER,
                date_created INTEGER,
                pos_x INTEGER,
                pos_y INTEGER,
                flip INTEGER DEFAULT 0,
                muted INTEGER DEFAULT 0,
                is_complete INTEGER DEFAULT 0,
                is_upgrading INTEGER DEFAULT 0,
                structure INTEGER,
                scale REAL DEFAULT 1.0,
                building_completed INTEGER,
                last_collection INTEGER,
                obj_data INTEGER,
                obj_end INTEGER
            )
        """,
        "player_eggs": """
            CREATE TABLE player_eggs (
                user_egg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_island_id INTEGER,
                laid_on INTEGER,
                hatches_on INTEGER,
                monster INTEGER,
                user_structure_id INTEGER,
                FOREIGN KEY(user_island_id) REFERENCES player_islands(user_island_id)
            )
        """,
        "player_breeding": """
            CREATE TABLE player_breeding (
                user_breeding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_island_id INTEGER,
                started_on INTEGER,
                completes_on INTEGER,
                result INTEGER NOT NULL,
                monster_1 INTEGER,
                monster_2 INTEGER,
                user_structure_id INTEGER,
                FOREIGN KEY(user_island_id) REFERENCES player_islands(user_island_id)
            )
        """,
        "monster_mega_data": """
            CREATE TABLE monster_mega_data (
                user_monster_id INTEGER PRIMARY KEY,
                started_at INTEGER,
                finishes_at INTEGER,
                permamega INTEGER,
                currently_mega INTEGER,
                FOREIGN KEY(user_monster_id) REFERENCES player_monsters(user_monster_id)
            )
        """,
    }

    for name, query in tables.items():
        if not table_exists(cur_player, name):
            cur_player.execute(query)
            print(f"Table '{name}' created successfully.")
        else:
            print(f"ℹTable '{name}' already exists.")

    if table_exists(cur_player, "player_monsters") and not table_has_column(cur_player, "player_monsters", "name"):
        try:
            cur_player.execute("ALTER TABLE player_monsters ADD COLUMN name TEXT DEFAULT 'Monster'")
            print("Column 'name' added to player_monsters.")
        except Exception as e:
            print(f"[!] Failed to add 'name' column to player_monsters: {e}")

    if table_exists(cur_player, "player_monsters") and table_has_column(cur_player, "player_monsters", "name"):
        try:
            cur_player.execute("UPDATE player_monsters SET name = 'Monster' WHERE name IS NULL")
        except Exception as e:
            print(f"[!] Failed to populate default monster names: {e}")

    db_player.commit()


def reset_all_player_stats(value=1_999_999_999):
    # No-op: currency is now persistent and should never be mass-reset at startup.
    pass

async def send_extension_response(client, cmd, params):
    ext_resp = SFSObject()
    ext_resp.put_utf_string("c", cmd)
    ext_resp.put_int("r", -1)
    ext_resp.put_sfs_object("p", params)

    await client.send(Message(
        controller=ControllerID.EXTENSION,
        action=12,
        payload=ext_resp
    ))

def load_static_data():
    global DATA_CACHE

    print("Loading static data")

    DATA_CACHE["genes"] = get_genes()

    DATA_CACHE["islands"] = get_islands()
    DATA_CACHE["torches"] = get_torch_data()

    DATA_CACHE["monsters"] = get_monsters()
    DATA_CACHE["structures"] = get_structures()

    DATA_CACHE["levels"] = get_levels()
    DATA_CACHE["levels_dict"] = get_levels_dict()

    DATA_CACHE["scratchoffs"] = get_scratchoffs()
    DATA_CACHE["timed_events"] = get_timed_events()
    DATA_CACHE["quests"] = SFSArray()#get_quests()

    DATA_CACHE["game_settings"] = get_game_settings()

    DATA_CACHE["store_groups"] = get_store_groups()
    DATA_CACHE["store_items"] = get_store_items()
    DATA_CACHE["store_currencys"] = get_store_currencys()

    print("Static data loaded")

def get_game_setting_from_key(search_key):
    for obj in DATA_CACHE["game_settings"]:
        key = obj.get("key")
        if key == search_key:
            return obj.get("value")
    return None


async def poll_pending_commands():
    """Poll the auth server for pending commands and forward them to clients.

    This function avoids polling when no clients are connected and uses
    a longer idle interval to reduce authserver /commands requests.
    """
    while True:
        try:
            if not CONNECTED_CLIENTS:
                await asyncio.sleep(IDLE_POLL_INTERVAL)
                continue

            resp = await asyncio.to_thread(requests.get, f"http://{AUTH_SERVER_IP}:900/commands", timeout=5)
            data = await asyncio.to_thread(lambda: resp.json())
            if data.get("ok"):
                commands = data.get("commands", [])
                for cmd in commands:
                    command = cmd.get("command")
                    payload = cmd.get("payload", {}) or {}
                    target = cmd.get("target_bbb_id")

                    if command == "gs_display_generic_message":
                        msg = SFSObject()
                        msg.put_bool("force_logout", bool(payload.get("force_logout", False)))
                        msg_text = payload.get("msg", "")
                        msg.put_utf_string("msg", str(msg_text))

                        if target is not None:
                            try:
                                target_id = int(target)
                            except Exception:
                                target_id = None

                            if target_id is not None and target_id in CONNECTED_CLIENTS:
                                client = CONNECTED_CLIENTS.get(target_id)
                                try:
                                    await send_extension_response(client, "gs_display_generic_message", msg)
                                except Exception:
                                    pass
                        else:
                            # broadcast to all connected clients
                            for client in list(CONNECTED_CLIENTS.values()):
                                try:
                                    await send_extension_response(client, "gs_display_generic_message", msg)
                                except Exception:
                                    pass
                    elif command == "give_currency":
                        currency = str(payload.get("currency", "")).lower()
                        amount = payload.get("amount")
                        if currency not in {"coins", "food", "diamonds", "shards", "xp", "level"}:
                            print(f"[!] invalid currency in pending give_currency: {currency}")
                        else:
                            try:
                                amount = int(amount)
                            except (TypeError, ValueError):
                                print(f"[!] invalid amount in pending give_currency: {amount}")
                                amount = None

                            if amount is not None:
                                if target is not None:
                                    try:
                                        target_id = int(target)
                                    except Exception:
                                        target_id = None
                                else:
                                    target_id = None

                                if target_id is not None and target_id in CONNECTED_CLIENTS:
                                    client = CONNECTED_CLIENTS[target_id]
                                    success = client.player.add_properties(**{currency: amount})
                                    if success:
                                        try:
                                            await send_player_properties_update(client)
                                        except Exception:
                                            pass
                                        print(f"[+] applied give_currency to connected player {target_id}: {amount} {currency}")
                                    else:
                                        print(f"[!] failed to apply give_currency to connected player {target_id}")
                                else:
                                    cur_player.execute(
                                        f"UPDATE players SET {currency} = {currency} + ? WHERE bbb_id = ?",
                                        (amount, target_id if target_id is not None else -1)
                                    )
                                    db_player.commit()
                                    if cur_player.rowcount == 0:
                                        print(f"[!] offline player {target_id} not found for give_currency")
                                    else:
                                        print(f"[+] updated offline player {target_id}: {amount} {currency}")

        except Exception:
            # ignore transient errors and retry
            pass

        await asyncio.sleep(POLL_INTERVAL)

def buy_entity(client, entity_id):
    cur.execute("SELECT * FROM entities WHERE entity_id = ?", (entity_id,))
    row = cur.fetchone()

    worked = client.player.add_properties(
        -row["cost_coins"],
        -row["cost_diamonds"],
        0,
        0,
        -row["cost_eth_currency"],
    )

    return worked

def sell_entity(client, entity_id):
    cur.execute("SELECT * FROM entities WHERE entity_id = ?", (entity_id,))
    row = cur.fetchone()

    worked = client.player.add_properties(row["cost_coins"] * 0.75, 0, 0, 0, 0)

    return worked

def get_breeding_result(monster_1, monster_2, level1, level2, player_level=None):
    if monster_1 > monster_2:
        monster_1, monster_2 = monster_2, monster_1

    cur.execute("""
        SELECT result, probability, modifier 
        FROM breeding_combinations
        WHERE (monster_1 = ? AND monster_2 = ?) 
           OR (monster_1 = ? AND monster_2 = ?)
        ORDER BY probability DESC  -- Higher probability results checked first
    """, (monster_1, monster_2, monster_2, monster_1))

    combinations = cur.fetchall()

    if combinations:
        for combo in combinations:
            result = combo["result"]
            base_prob = combo["probability"]
            modifier = combo["modifier"]

            breed_chance = 1999999 
            '''calculate_probability_for_breeding(
                level1, level2, base_prob, modifier
            )'''

            if random.randint(1, 100) <= breed_chance:
                return result

    total_levels = level1 + level2
    if total_levels <= 0:
        return random.choice([monster_1, monster_2])

    first_prob = int((level1 / total_levels) * 100)

    return monster_1 if random.randint(1, 100) <= first_prob else monster_2

async def handle_client(client: TCPTransport):
    global CURRENT_PLAYERS
    try:
        async for message in client.listen():
            payload = message.payload
            current_time_ms = int(time.time() * 1000)
            if "p" in payload and payload["p"] is not None:
                params = payload.get("p")
            else:
                params = SFSObject()

            if message.action == SysAction.HANDSHAKE:
                print("Handshake")
                token = hashlib.md5(client._host.encode()).hexdigest()

                session_info = SFSObject()
                session_info.put_int("ct", 1000000)
                session_info.put_int("ms", 8000000)
                session_info.put_utf_string("tk", token)
                
                await client.send(Message(
                    controller=ControllerID.SYSTEM,
                    action=SysAction.HANDSHAKE,
                    payload=session_info
                ))
            elif message.action == SysAction.LOGIN:
                print("Login")

                bbb_id = int(payload.get("un"))

                login = SFSObject()
                login.put_short("rs", 0)
                login.put_utf_string("zn", "MySingingMonsters")
                login.put_utf_string("un", str(bbb_id))
                login.put_short("pi", 1)
                login.put_int("id", CURRENT_PLAYERS + 1)
                login.put_sfs_object("p", SFSObject())

                MSMRoom = Room(room_id=0, name="Limbo", room_type="default", is_hidden=False, 
                is_password_protected=False, is_game=False, user_count=1, max_players=MAX_PLAYERS)

                RoomArrays = SFSArray()
                RoomArrays.add(MSMRoom.to_sfs_array())

                login.put_sfs_array("rl", RoomArrays)

                await client.send(Message(
                    controller=ControllerID.SYSTEM,
                    action=SysAction.LOGIN,
                    payload=login
                ))

                CURRENT_PLAYERS += 1

                payload = {"bbb_id": bbb_id, "game_id": 1}

                verification = requests.post(f"http://127.0.0.1:900/verify_user", json=payload).json()

                ok = verification["ok"]

                game_settings = SFSObject()
                game_settings.put_sfs_array("user_game_settings", DATA_CACHE["game_settings"])

                await send_extension_response(client, "game_settings", game_settings)

                initialized = SFSObject()
                initialized.put_long("bbb_id", bbb_id)

                await send_extension_response(client, "gs_initialized", initialized)

                if ok != True:
                    ban = SFSObject()
                    ban.put_utf_string("reason", "Your auth session has expired. Please re-login.")
                    await send_extension_response(client, "gs_player_banned", ban)
                    return

                if CURRENT_PLAYERS >= MAX_PLAYERS:
                    ban = SFSObject()
                    ban.put_utf_string("reason", "Server is full\n\n({MAX_PLAYERS}/{MAX_PLAYERS})")
                    await send_extension_response(client, "gs_player_banned", ban)
                    return

                exists = player_exists(bbb_id)

                try:
                    session_json = decrypt(verification["session_id"], CRYPT_IV, CRYPT_KEY)
                    session_data = json.loads(session_json)
                except (UnicodeDecodeError, json.JSONDecodeError, Exception) as e:
                    print(f"Error decrypting/parsing session data: {e}")
                    ban = SFSObject()
                    ban.put_utf_string("reason", "Session data is invalid. Please re-login.")
                    await send_extension_response(client, "gs_player_banned", ban)
                    return

                user_id = session_data["user_id"] 

                player = Player(bbb_id, "New Player", user_id)

                if exists:
                    cur_player.execute("""
                        SELECT active_island, display_name, coins, food, diamonds, shards, xp, level
                        FROM players WHERE bbb_id = ?
                    """, (bbb_id,))
                    row = cur_player.fetchone()

                    player.active_island = row["active_island"]
                    player.display_name = row["display_name"] or "New Player"

                    player.coins = row["coins"]
                    player.food = row["food"]
                    player.diamonds = row["diamonds"]
                    player.shards = row["shards"]
                    player.xp = row["xp"]
                    player.level = row["level"]

                    cur_player.execute("""
                        SELECT * FROM player_islands WHERE bbb_id = ?
                    """, (bbb_id,))
                    islands = cur_player.fetchall()
                    for islandData in islands:
                        island = Island(bbb_id, islandData["island_id"], islandData["user_island_id"])

                        island.likes = islandData["likes"]
                        island.dislikes = islandData["dislikes"]

                        island.add_player_monsters()
                        island.add_player_structures()
                        island.add_player_eggs()
                        island.add_player_breedings()

                        player.add_island(island)                    
                else:
                    cur_player.execute("""
                        INSERT INTO player_islands (bbb_id, date_created, island_id)
                        VALUES (?, ?, ?)
                    """, (bbb_id, current_time_ms, 1))

                    db_player.commit()

                    user_island_id = cur_player.lastrowid

                    cur_player.execute("""
                        INSERT INTO players (
                            bbb_id,
                            active_island,
                            coins,
                            food,
                            diamonds,
                            shards,
                            xp,
                            level,
                            display_name,
                            last_login
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        bbb_id,
                        user_island_id, # active_island
                        1_200,    # coins
                        0,    # food
                        12,    # diamonds
                        0,    # shards
                        0,    # xp
                        1,    # level
                        "New Player",
                        current_time_ms
                    ))

                    db_player.commit()

                    player.active_island = user_island_id

                    island = Island(bbb_id, 1, user_island_id)
                    island.create_structures()
                    
                    # Load the newly created structures into the island object
                    island.add_player_structures()
                    island.add_player_eggs()
                    island.add_player_breedings()

                    player.add_island(island)

                # Ensure level table is available before applying XP/level changes
                player._levels = DATA_CACHE["levels_dict"]

                if not exists:
                    # New player: apply the starter values from the INSERT above
                    player.coins = 1_200
                    player.food = 0
                    player.diamonds = 12
                    player.shards = 0
                    player.xp = 0
                    player.level = 1

                player.display_name = sanitize_name(player.display_name, ALPHABET)

                client.player = player

                # register connected client so server can push queued commands
                try:
                    CONNECTED_CLIENTS[player.bbb_id] = client
                except Exception:
                    pass
                
                # Start background task to auto-update all monster names every second
                asyncio.create_task(auto_update_monster_names(client))

            else:
                cmd = payload.get("c") or ""
                cmd_lower = str(cmd).strip().lower()
                print(cmd)
                if cmd_lower == "db_gene":
                    response = SFSObject()
                    response.put_sfs_array("genes_data", DATA_CACHE["genes"])
                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "db_island":
                    response = SFSObject()
                    response.put_sfs_array("islands_data", DATA_CACHE["islands"])
                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "db_island_torches":
                    response = SFSObject()
                    response.put_sfs_array("island_torch_data", DATA_CACHE["torches"])
                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "db_monster":
                    DATA_CACHE["monsters"] = get_monsters()
                    response = SFSObject()
                    response.put_sfs_array("monsters_data", DATA_CACHE["monsters"])
                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "db_store":
                    store_items = DATA_CACHE["store_items"]
                    store_groups = DATA_CACHE["store_groups"]
                    store_currencys = DATA_CACHE["store_currencys"]

                    response = SFSObject()
                    response.put_sfs_array("store_item_data", store_items)
                    response.put_sfs_array("store_group_data", store_groups)
                    response.put_sfs_array("store_currency_data", store_currencys)

                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "db_structure":
                    response = SFSObject()
                    response.put_sfs_array("structures_data", DATA_CACHE["structures"])
                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)
                        
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "db_level":
                    response = SFSObject()
                    response.put_sfs_array("level_data", DATA_CACHE["levels"])
                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)
                        
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "db_scratch_offs":
                    response = SFSObject()
                    response.put_sfs_array("scratch_offs", DATA_CACHE["scratchoffs"])
                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)
                        
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_promos":
                    response = SFSObject()

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_timed_events":
                    response = SFSObject()

                    response.put_sfs_array("timed_event_list", DATA_CACHE["timed_events"])
                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_quest":
                    response = SFSObject()
                    response.put_sfs_array("result", DATA_CACHE["quests"])

                    response.put_long("server_time", current_time_ms)
                    response.put_long("last_updated", current_time_ms)
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_buy_island":
                    island_id = int(params.get("island_id"))
                    response = SFSObject()

                    if island_id == 0:
                        print("Whoops1")
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Error")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "SELECT * FROM player_islands WHERE bbb_id = ? AND island_id = ?",
                        (client.player.bbb_id, island_id)
                    )
                    existing = cur_player.fetchone()
                    if existing:
                        print("Whoops3")
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Error")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur.execute("SELECT * FROM islands WHERE island_id = ?", (island_id,))
                    island_row = cur.fetchone()
                    if not island_row:
                        print("Whoops")
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Error")
                        await send_extension_response(client, cmd, response)
                        continue

                    cost_coins = island_row["cost_coins"]
                    cost_diamonds = island_row["cost_diamonds"]
                    if not client.player.add_properties(-cost_coins, -cost_diamonds, 0, 0):
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Not enough resources")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "INSERT INTO player_islands (bbb_id, date_created, island_id) VALUES (?, ?, ?)",
                        (client.player.bbb_id, current_time_ms, island_id)
                    )
                    db_player.commit()
                    user_island_id = cur_player.lastrowid

                    new_island = Island(client.player.bbb_id, island_id, user_island_id)
                    new_island.create_structures()
                    client.player.add_island(new_island)

                    response.put_bool("success", True)
                    response.put_sfs_array("properties", client.player.get_properties())
                    response.put_sfs_object("user_island", new_island.get_sfs_object())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_change_island":
                    user_island_id = params.get("user_island_id")
                    bbb_id = client.player.bbb_id

                    response = SFSObject()

                    cur_player.execute(
                        "SELECT * FROM player_islands WHERE user_island_id = ? AND bbb_id = ?",
                        (user_island_id, bbb_id)
                    )
                    row = cur_player.fetchone()

                    if row:
                        cur_player.execute(
                            "UPDATE players SET active_island = ? WHERE bbb_id = ?",
                            (user_island_id, bbb_id)
                        )
                        db_player.commit()

                        client.player.active_island = user_island_id

                        response.put_bool("success", True)
                        response.put_long("user_island_id", user_island_id)
                    else:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "You don't have this island")

                    hidden_objects = SFSObject()
                    hidden_objects.put_sfs_array("objects", SFSArray())
                    response.put_sfs_object("hidden_objects", hidden_objects)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_buy_egg":
                    response = SFSObject()

                    monster_id = params.get("monster_id")

                    cur.execute("SELECT * FROM monsters WHERE monster_id = ?", (monster_id,))
                    row = cur.fetchone()

                    cur.execute("SELECT * FROM entities WHERE entity_id = ?", (row["entity"],))
                    row2 = cur.fetchone()

                    if buy_entity(client, row["entity"]) != True:
                        continue

                    endtime = current_time_ms + (row2["build_time"] * 1000)

                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE user_island_id = ? AND structure = 1",
                        (client.player.active_island,)
                    )

                    row = cur_player.fetchone()

                    if row is None:
                        print("Error")
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Error")
                    else:
                        cur_player.execute(
                            "INSERT INTO player_eggs (user_island_id, laid_on, hatches_on, monster, user_structure_id) VALUES (?, ?, ?, ?, ?)",
                            (client.player.active_island, current_time_ms, endtime, monster_id, row["user_structure_id"])
                        )
                        db_player.commit()
                        user_egg_id = cur_player.lastrowid

                        egg = Egg(client.player.active_island, current_time_ms, endtime, monster_id, user_egg_id, row["user_structure_id"])

                        cur_player.execute(
                            "SELECT * FROM player_eggs WHERE user_egg_id = ?",
                            (user_egg_id,)
                        )
                        egg_row = cur_player.fetchone()

                        if egg_row:
                            new_egg = Egg(
                                egg_row["user_island_id"],
                                egg_row["laid_on"],
                                egg_row["hatches_on"],
                                egg_row["monster"],
                                egg_row["user_egg_id"],
                                egg_row["user_structure_id"],
                            )

                            eggs_array = SFSArray()
                            eggs_array.add_sfs_object(new_egg.get_sfs_object())

                            update_resp = SFSObject()
                            update_resp.put_sfs_array("eggs", eggs_array)

                            await send_extension_response(client, "gs_update_eggs", update_resp)

                        response.put_sfs_object("user_egg", egg.get_sfs_object())
                        response.put_bool("success", True)
                        response.put_bool("remove_buyback", False)
                        response.put_sfs_array("properties", client.player.get_properties())
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_sell_egg":
                    user_egg_id = params.get("user_egg_id")
                    response = SFSObject()

                    if user_egg_id is None:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Invalid egg ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "SELECT * FROM player_eggs WHERE user_island_id = ? AND user_egg_id = ?",
                        (client.player.active_island, user_egg_id)
                    )
                    egg_row = cur_player.fetchone()

                    if egg_row is None:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Egg not found")
                        await send_extension_response(client, cmd, response)
                        continue

                    monster_id = egg_row["monster"]
                    cur.execute("SELECT * FROM monsters WHERE monster_id = ?", (monster_id,))
                    monster_row = cur.fetchone()

                    refund_coins = 0
                    refund_diamonds = 0
                    if monster_row is not None:
                        cur.execute("SELECT * FROM entities WHERE entity_id = ?", (monster_row["entity"],))
                        entity_row = cur.fetchone()
                        if entity_row is not None:
                            refund_coins = int(entity_row["cost_coins"] * 0.75)
                            refund_diamonds = int(entity_row["cost_diamonds"] * 0.75)

                    if refund_coins or refund_diamonds:
                        client.player.add_properties(coins=refund_coins, diamonds=refund_diamonds)

                    cur_player.execute(
                        "DELETE FROM player_eggs WHERE user_egg_id = ?",
                        (user_egg_id,)
                    )
                    db_player.commit()

                    response.put_bool("success", True)
                    response.put_long("user_egg_id", user_egg_id)
                    response.put_sfs_array("properties", client.player.get_properties())
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_speed_up_hatching":
                    user_egg_id = params.get("user_egg_id")

                    cur_player.execute(
                        "SELECT * FROM player_eggs WHERE user_island_id = ? AND user_egg_id = ?",
                        (client.player.active_island, user_egg_id)
                    )
                    row = cur_player.fetchone()

                    if row is None:
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Error")
                        await send_extension_response(client, cmd, response)
                        continue

                    response = SFSObject()

                    laid_on = row["laid_on"]
                    monster_id = row["monster"]

                    cur_player.execute(
                        "UPDATE player_eggs SET hatches_on = ? WHERE user_egg_id = ?",
                        (current_time_ms, user_egg_id)
                    )
                    db_player.commit()

                    response = SFSObject()
                    response.put_bool("success", True)
                    response.put_long("user_egg_id", user_egg_id)
                    response.put_long("hatches_on", current_time_ms)
                    response.put_long("laid_on", laid_on)
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_hatch_egg":
                    pos_x = params.get("pos_x")
                    pos_y = params.get("pos_y")
                    pos_x = int(pos_x) if pos_x not in (None, "") else 1
                    pos_y = int(pos_y) if pos_y not in (None, "") else 1
                    flip = int(bool(params.get("flip")))
                    user_egg_id = params.get("user_egg_id")
                    response = SFSObject()

                    cur_player.execute(
                        "SELECT * FROM player_eggs WHERE user_island_id = ? AND user_egg_id = ?",
                        (client.player.active_island, user_egg_id)
                    )
                    row = cur_player.fetchone()

                    if row is None:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Error")
                        await send_extension_response(client, cmd, response)
                        continue

                    monster_id = row["monster"]
                    user_structure_id = row["user_structure_id"]
                    
                    cur_player.execute(
                        "DELETE FROM player_eggs WHERE user_egg_id = ?",
                        (user_egg_id,)
                    )
                    db_player.commit()

                    new_name = "Monster"
                    cur_player.execute(
                        """
                        INSERT INTO player_monsters (
                            user_island_id,
                            pos_x,
                            pos_y,
                            flip,
                            level,
                            happiness,
                            date_created,
                            monster,
                            name,
                            volume,
                            last_collection
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            client.player.active_island,
                            pos_x,
                            pos_y,
                            flip,
                            1,
                            0,
                            current_time_ms,
                            monster_id,
                            new_name,
                            1.0,
                            current_time_ms
                        )
                    )

                    db_player.commit()

                    cur_player.execute(
                        """
                        SELECT *
                        FROM player_monsters
                        WHERE user_island_id = ?
                        ORDER BY user_monster_id DESC
                        LIMIT 1
                        """,
                        (client.player.active_island,)
                    )
                    monster_row = cur_player.fetchone()
                    user_monster_id = monster_row["user_monster_id"]

                    cur.execute("SELECT * FROM monsters WHERE monster_id = ?", (monster_id,))
                    monster_static = cur.fetchone()

                    cur.execute("SELECT xp FROM entities WHERE entity_id = ?", (monster_static["entity"],))
                    row2 = cur.fetchone()

                    if client.player.level < 4:
                        client.player.add_properties(xp=150)
                    else:
                        client.player.add_properties(xp=row2["xp"])

                    newMonster = Monster(client.player.active_island, user_monster_id, monster_id, pos_x, pos_y, flip, 1, 0, 0, 0, 1.0, current_time_ms, current_time_ms, 0, name=get_monster_name(monster_row))
                    refresh_cached_monster(client, newMonster)

                    response.put_sfs_array("properties", client.player.get_properties())
                    response.put_long("user_egg_id", user_egg_id)
                    response.put_long("island", client.player.active_island)
                    response.put_sfs_object("monster", newMonster.get_sfs_object())
                    response.put_bool("success", True)
                    response.put_bool("directPlace", False)
                    response.put_bool("remove_buyback", False)
                    response.put_long("user_structure_id", user_structure_id)

                    await send_extension_response(client, cmd, response)

                    plrisland = client.player.get_active_island()

                    plrisland.add_monster(newMonster)

                    hatch_resp = SFSObject()
                    hatch_resp.put_bool("success", True)
                    hatch_resp.put_long("user_monster_id", user_monster_id)
                    hatch_resp.put_sfs_object("monster", newMonster.get_sfs_object())
                    await send_extension_response(client, "gs_update_monster", hatch_resp)

                    player_response = SFSObject()
                    player_response.put_sfs_object("player_object", client.player.get_sfs_object())
                    player_response.put_long("server_time", current_time_ms)

                    await send_extension_response(client, "gs_player", player_response)
                elif cmd_lower == "gs_buy_structure":
                    x = params.get("pos_x")
                    y = params.get("pos_y")
                    flip = params.get("flip")
                    scale = params.get("scale")
                    structure_id = params.get("structure_id")

                    cur.execute("SELECT * FROM structures WHERE structure_id = ?", (structure_id,))
                    row = cur.fetchone()

                    cur.execute("SELECT * FROM entities WHERE entity_id = ?", (row["entity"],))
                    row2 = cur.fetchone()

                    if buy_entity(client, row["entity"]) != True:
                        continue

                    cur_player.execute("""
                        INSERT INTO player_structures (
                            user_island_id,
                            date_created,
                            pos_x,
                            pos_y,
                            flip,
                            muted,
                            is_complete,
                            is_upgrading,
                            structure,
                            scale,
                            building_completed,
                            last_collection,
                            obj_data,
                            obj_end
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        client.player.active_island,
                        current_time_ms,
                        x,
                        y,
                        flip,
                        0,
                        1,
                        0,
                        structure_id,
                        scale,
                        current_time_ms,
                        current_time_ms,
                        0,
                        0
                    ))

                    db_player.commit()

                    cur_player.execute("SELECT user_structure_id FROM player_structures WHERE user_island_id = ? AND pos_x = ? AND pos_y = ?", (client.player.active_island, x, y))
                    row = cur_player.fetchone()

                    newStructure = Structure(
                        client.player.active_island,
                        row["user_structure_id"],
                        structure_id,
                        x,
                        y,
                        flip,
                        scale,
                        current_time_ms,
                        current_time_ms,
                        current_time_ms,
                        0,
                        0,
                    )

                    response = SFSObject()

                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE user_structure_id = ?",
                        (row["user_structure_id"],)
                    )
                    structure_row = cur_player.fetchone()

                    if structure_row:
                        updated_structure = Structure(
                            structure_row["user_island_id"],
                            structure_row["user_structure_id"],
                            structure_row["structure"],
                            structure_row["pos_x"],
                            structure_row["pos_y"],
                            structure_row["flip"],
                            structure_row["scale"],
                            structure_row["date_created"],
                            structure_row["building_completed"],
                            structure_row["last_collection"],
                            structure_row["obj_data"],
                            structure_row["obj_end"],
                        )

                        structures_array = SFSArray()
                        structures_array.add_sfs_object(updated_structure.get_sfs_object())

                        update_resp = SFSObject()
                        update_resp.put_sfs_array("structures", structures_array)

                        await send_extension_response(client, "gs_update_structures", update_resp)

                    response.put_bool("success", True)
                    response.put_sfs_array("properties", client.player.get_properties())
                    response.put_sfs_object("user_structure", newStructure.get_sfs_object())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_start_upgrade_structure":
                    user_structure_id = params.get("user_structure_id")
                    response = SFSObject()

                    if user_structure_id is None:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "No structure specified")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE user_structure_id = ? AND user_island_id = ?",
                        (user_structure_id, client.player.active_island)
                    )
                    structure_row = cur_player.fetchone()

                    if structure_row is None:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Structure not found")
                        await send_extension_response(client, cmd, response)
                        continue

                    if structure_row["is_upgrading"] == 1:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Structure already upgrading")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur.execute("SELECT * FROM structures WHERE structure_id = ?", (structure_row["structure"],))
                    current_structure = cur.fetchone()
                    if current_structure is None:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Current structure data missing")
                        await send_extension_response(client, cmd, response)
                        continue

                    next_structure_id = current_structure["upgrades_to"]
                    if not next_structure_id:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "No upgrade available")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur.execute("SELECT * FROM structures WHERE structure_id = ?", (next_structure_id,))
                    next_structure = cur.fetchone()
                    if next_structure is None:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Upgrade data missing")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur.execute("SELECT * FROM entities WHERE entity_id = ?", (next_structure["entity"],))
                    next_entity = cur.fetchone()
                    if next_entity is None:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Upgrade entity missing")
                        await send_extension_response(client, cmd, response)
                        continue

                    cost_coins = next_entity["cost_coins"] or 0
                    cost_diamonds = next_entity["cost_diamonds"] or 0
                    cost_shards = next_entity["cost_eth_currency"] or 0

                    if not client.player.add_properties(coins=-cost_coins, diamonds=-cost_diamonds, food=0, xp=0, shards=-cost_shards):
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Not enough currency")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "UPDATE player_structures SET structure = ?, date_created = ?, building_completed = ?, last_collection = ?, obj_data = ?, obj_end = ? WHERE user_structure_id = ?",
                        (
                            next_structure_id,
                            current_time_ms,
                            current_time_ms,
                            current_time_ms,
                            None,
                            None,
                            user_structure_id,
                        )
                    )
                    db_player.commit()

                    upgraded_structure = Structure(
                        client.player.active_island,
                        user_structure_id,
                        next_structure_id,
                        structure_row["pos_x"],
                        structure_row["pos_y"],
                        structure_row["flip"],
                        structure_row["scale"],
                        current_time_ms,
                        current_time_ms,
                        current_time_ms,
                        None,
                        None,
                    )

                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE user_structure_id = ?",
                        (user_structure_id,)
                    )
                    structure_row = cur_player.fetchone()

                    if structure_row:
                        updated_structure = Structure(
                            structure_row["user_island_id"],
                            structure_row["user_structure_id"],
                            structure_row["structure"],
                            structure_row["pos_x"],
                            structure_row["pos_y"],
                            structure_row["flip"],
                            structure_row["scale"],
                            structure_row["date_created"],
                            structure_row["building_completed"],
                            structure_row["last_collection"],
                            structure_row["obj_data"],
                            structure_row["obj_end"],
                        )

                        structures_array = SFSArray()
                        structures_array.add_sfs_object(updated_structure.get_sfs_object())

                        update_resp = SFSObject()
                        update_resp.put_sfs_array("structures", structures_array)

                        await send_extension_response(client, "gs_update_structures", update_resp)

                    response.put_bool("success", True)
                    response.put_long("user_structure_id", user_structure_id)
                    response.put_sfs_object("user_structure", upgraded_structure.get_sfs_object())
                    response.put_sfs_array("properties", client.player.get_properties())
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_mute_monster":
                    user_monster_id = params.get("user_monster_id")

                    cur_player.execute(
                        """
                        SELECT muted FROM player_monsters
                        WHERE user_monster_id = ? AND user_island_id = ?
                        """,
                        (user_monster_id, client.player.active_island)
                    )
                    row_muted = cur_player.fetchone()

                    muted = 0 if row_muted["muted"] == 1 else 1

                    response = SFSObject()

                    cur_player.execute(
                        """
                        UPDATE player_monsters
                        SET muted = ?
                        WHERE user_monster_id = ? AND user_island_id = ?
                        """,
                        (muted, user_monster_id, client.player.active_island)
                    )
                    db_player.commit()

                    cur_player.execute(
                        """
                        SELECT * FROM player_monsters
                        WHERE user_monster_id = ? AND user_island_id = ?
                        """,
                        (user_monster_id, client.player.active_island)
                    )
                    monster_row = cur_player.fetchone()

                    updatedMonster = Monster(
                        client.player.active_island,
                        user_monster_id,
                        monster_row["monster"],
                        monster_row["pos_x"],
                        monster_row["pos_y"],
                        monster_row["flip"],
                        monster_row["level"],
                        monster_row["happiness"],
                        monster_row["collected_coins"],
                        monster_row["times_fed"],
                        monster_row["volume"],
                        monster_row["date_created"],
                        monster_row["last_collection"],
                        muted,
                        name=get_monster_name(monster_row)
                    )
                    refresh_cached_monster(client, updatedMonster)

                    response.put_bool("success", True)
                    response.put_long("user_monster_id", user_monster_id)
                    response.put_sfs_object("monster", updatedMonster.get_sfs_object())
                    response.put_int("muted", muted)

                    response2 = SFSObject()
                    response2.put_bool("success", True)

                    await send_extension_response(client, cmd, response2)

                    await send_extension_response(client, "gs_update_monster", response)
                elif cmd_lower == "gs_mute_structure":
                    user_structure_id = params.get("user_structure_id")

                    cur_player.execute(
                        "SELECT muted FROM player_structures WHERE user_structure_id = ? AND user_island_id = ?",
                        (user_structure_id, client.player.active_island)
                    )
                    row_muted = cur_player.fetchone()

                    response = SFSObject()
                    if row_muted is None:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid structure ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    muted = 0 if row_muted["muted"] == 1 else 1

                    cur_player.execute(
                        "UPDATE player_structures SET muted = ? WHERE user_structure_id = ? AND user_island_id = ?",
                        (muted, user_structure_id, client.player.active_island)
                    )
                    db_player.commit()

                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE user_structure_id = ? AND user_island_id = ?",
                        (user_structure_id, client.player.active_island)
                    )
                    structure_row = cur_player.fetchone()

                    updatedStructure = Structure(
                        client.player.active_island,
                        user_structure_id,
                        structure_row["structure"],
                        structure_row["pos_x"],
                        structure_row["pos_y"],
                        structure_row["flip"],
                        structure_row["scale"],
                        structure_row["date_created"],
                        structure_row["building_completed"] if "building_completed" in structure_row.keys() else None,
                        structure_row["last_collection"] if "last_collection" in structure_row.keys() else None,
                        structure_row["obj_data"] if "obj_data" in structure_row.keys() else None,
                        structure_row["obj_end"] if "obj_end" in structure_row.keys() else None,
                        muted=muted,
                    )

                    response.put_bool("success", True)
                    response.put_long("user_structure_id", user_structure_id)
                    response.put_sfs_object("user_structure", updatedStructure.get_sfs_object())
                    response.put_int("muted", muted)

                    response2 = SFSObject()
                    response2.put_bool("success", True)

                    await send_extension_response(client, cmd, response2)
                    await send_extension_response(client, "gs_update_structure", response)
                elif cmd_lower == "gs_referral_request":
                    code = str(params.get("referring_bbb_id"))

                    response = SFSObject()

                    if code == "132026":
                        worked = client.player.add_properties(coins=999_999_999,diamonds=0,food=999_999_999,shards=999_999_999,xp=0,level=0,set=True)

                        response.put_bool("success", True)
                        response.put_sfs_array("properties", client.player.get_properties())

                        await send_extension_response(client, "gs_update_properties", response)
                    elif code == "0000":
                        worked = client.player.add_properties(coins=0, diamonds=0, food=0, shards=0, xp=0, level=1, set=True)

                        response.put_bool("success", True)
                        response.put_sfs_array("properties", client.player.get_properties())

                        await send_extension_response(client, "gs_update_properties", response)                   
                elif cmd_lower == "gs_set_displayname":
                    displayname = params.get("newName")

                    if not displayname:
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("message", "INVALID_DISPLAY_NAME")
                        response.put_bool("responseToUser", True)
                        await send_extension_response(client, cmd, response)
                        continue

                    displayname = sanitize_name(displayname, ALPHABET)

                    errmsg = invalid_name(displayname)

                    if errmsg is not None:
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("message", errmsg)
                        response.put_bool("responseToUser", True)
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "UPDATE players SET display_name = ? WHERE bbb_id = ?",
                        (displayname, client.player.bbb_id)
                    )
                    db_player.commit()

                    response = SFSObject()
                    response.put_bool("success", True)
                    response.put_utf_string("displayName", displayname)
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_memory_minigame_current_cost":
                    diamonds = 2
                    coins = 0

                    response = SFSObject()
                    response.put_int("diamond_cost", diamonds)
                    response.put_int("coin_cost", coins)
                    response.put_bool("success", True)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_get_memory_game_numbers":
                    response = SFSObject()
                    
                    response.put_int("memoryGameAudioSampleNumber", 100)  # MEMORY_AUDIO_SAMPLE_NUM
                    response.put_float("toneDuration", 2.0)               # MEMORY_TONE_DURATION
                    response.put_float("startGamePauseDuration", 2.0)     # MEMORY_START_GAME_PAUSE_DURATION
                    response.put_float("startSeqPauseDuration", 0.0)      # MEMORY_START_SEQ_PAUSE_DURATION
                    response.put_float("postNotePauseDuration", 0.0)      # MEMORY_POST_NOTE_PAUSE_DURATION
                    response.put_float("postSwapPauseDuration", 0.5)      # MEMORY_POST_SWAP_PAUSE_DURATION
                    response.put_float("failPauseDuration", 1.0)          # MEMORY_FAIL_PAUSE_DURATION
                    
                    # Swap / double tap settings
                    response.put_int("swapBeginStep", -1)                 # MEMORY_SWAP_BEGIN_STEP
                    response.put_float("monsterSwapChance", 0.5)         # MEMORY_MONSTER_SWAP_CHANCE
                    response.put_int("stepDurationOfSwap", 1)            # MEMORY_STEP_DURATION_OF_SWAP
                    response.put_float("swapAnimationSpeed", 5000.0)     # MEMORY_SWAP_ANIM_SPEED
                    response.put_int("doubleTapBeginStep", 10)           # MEMORY_DOUBLE_TAP_BEGIN_STEP
                    response.put_float("doubleTapChance", 0.5)           # MEMORY_DOUBLE_TAP_CHANCE
                    
                    # Tier response levels
                    response.put_int("tier1ResponseLevel", 5)            # MEMORY_TIER1_RESPONSE_LVL
                    response.put_int("tier2ResponseLevel", 10)           # MEMORY_TIER2_RESPONSE_LVL
                    response.put_int("tier3ResponseLevel", 20)           # MEMORY_TIER3_RESPONSE_LVL
                    response.put_int("tier4ResponseLevel", 50)           # MEMORY_TIER4_RESPONSE_LVL
                    
                    # Tone duration mode (fixed or animation-based)
                    response.put_int("fixedToneDuration", 0)             # MEMORY_FIXED_TONE_DURATION
                    
                    # Rewards / pricing (optional but in table)
                    response.put_int("diamondPrice", 2)                  # MEMORY_DIAMOND_PRICE
                    response.put_int("coinPrice", 0)                     # MEMORY_COIN_PRICE
                    response.put_int("diamondReward", 1)                # MEMORY_DIAMOND_REWARD
                    response.put_int("coinReward", 25)                  # MEMORY_COIN_REWARD
                    response.put_int("foodReward", 50)                  # MEMORY_FOOD_REWARD
                    
                    # Reward frequencies
                    response.put_int("coinRewardFreq", 1)               # MEMORY_COIN_REWARD_FREQ
                    response.put_int("foodRewardFreq", 5)               # MEMORY_FOOD_REWARD_FREQ
                    response.put_int("diamondRewardFreq", 1)            # MEMORY_DIAMOND_REWARD_FREQ
                    
                    # Timing before fail
                    response.put_float("timeBeforeFail", 5.0)           # MEMORY_TIME_BEFORE_FAIL

                    response.put_int("prev_highscore", 1200)
                    response.put_int("topscore", 4500)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_collect_daily_reward":
                    response = SFSObject()

                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_player_has_scratch_off":
                    response = SFSObject()

                    response.put_bool("success", False)
                    #response.put_utf_string("type", params.get("type"))

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_play_scratch_off" or cmd_lower == "gs_purchase_scratch_off":
                    response = SFSObject()

                    response.put_bool("success", False)
                    ticket = SFSObject()
                    ticket.put_int("id", 9)
                    ticket.put_utf_string("type", "C")
                    ticket.put_int("amount", 1000)
                    ticket.put_utf_string("prize", "diamonds")

                    scaled_prizes = SFSObject()
                    scaled_prizes.put_int("tier1", 50)
                    scaled_prizes.put_int("tier2", 100)
                    scaled_prizes.put_int("tier3", 200)

                    response.put_sfs_object("ticket", ticket)
                    response.put_sfs_object("scaled_prizes", scaled_prizes)
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_move_monster":
                    user_monster_id = params.get("user_monster_id")
                    new_x = params.get("pos_x")
                    new_y = params.get("pos_y")
                    new_x = int(new_x) if new_x not in (None, "") else 1
                    new_y = int(new_y) if new_y not in (None, "") else 1

                    response = SFSObject()

                    cur_player.execute(
                        """
                        SELECT *
                        FROM player_monsters
                        WHERE user_monster_id = ? AND user_island_id = ?
                        """,
                        (user_monster_id, client.player.active_island)
                    )
                    monster_row = cur_player.fetchone()

                    if not monster_row:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    position_state = get_monster_position_state(client.player.active_island, user_monster_id)
                    stale_move = (
                        position_state is not None
                        and (new_x, new_y) == position_state["previous"]
                        and (new_x, new_y) != position_state["current"]
                    )

                    final_x = monster_row["pos_x"]
                    final_y = monster_row["pos_y"]

                    if not stale_move:
                        cur_player.execute(
                            """
                            UPDATE player_monsters
                            SET pos_x = ?, pos_y = ?
                            WHERE user_monster_id = ? AND user_island_id = ?
                            """,
                            (new_x, new_y, user_monster_id, client.player.active_island)
                        )
                        db_player.commit()

                        final_x = new_x
                        final_y = new_y
                    else:
                        print(
                            f"[!] rejected stale monster move for {user_monster_id} on island {client.player.active_island}: "
                            f"requested {(new_x, new_y)} -> forcing {(final_x, final_y)}"
                        )

                    updatedMonster = Monster(
                        client.player.active_island,
                        user_monster_id,
                        monster_row["monster"],
                        final_x,
                        final_y,
                        monster_row["flip"],
                        monster_row["level"],
                        monster_row["happiness"],
                        monster_row["collected_coins"],
                        monster_row["times_fed"],
                        monster_row["volume"],
                        monster_row["date_created"],
                        monster_row["last_collection"],
                        monster_row["muted"],
                        name=get_monster_name(monster_row)
                    )
                    refresh_cached_monster(client, updatedMonster)

                    response.put_bool("success", True)
                    response.put_long("user_monster_id", user_monster_id)
                    response.put_sfs_object("monster", updatedMonster.get_sfs_object())
                    response.put_int("pos_x", final_x)
                    response.put_int("pos_y", final_y)

                    response2 = SFSObject()
                    response2.put_bool("success", True)

                    await send_extension_response(client, cmd, response2)

                    await send_extension_response(client, "gs_update_monster", response)
                elif cmd_lower == "gs_feed_monster":
                    user_monster_id = params.get("user_monster_id")
                    response = SFSObject()

                    cur_player.execute(
                        "SELECT * FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                        (user_monster_id, client.player.active_island)
                    )
                    monster_row = cur_player.fetchone()

                    if not monster_row:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    monster_id = monster_row["monster"]
                    current_level = monster_row["level"]

                    cur.execute(
                        "SELECT * FROM monster_levels WHERE monster = ? AND level = ?",
                        (monster_id, current_level)
                    )
                    mlevel = cur.fetchone()

                    if not mlevel:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Monster level data not found")
                        await send_extension_response(client, cmd, response)
                        continue

                    food_needed = mlevel["food"]

                    success = client.player.add_properties(food=-food_needed)
                    if not success:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Not enough food")
                        await send_extension_response(client, cmd, response)
                        continue

                    times_fed = (monster_row["times_fed"] or 0) + 1
                    new_level = monster_row["level"]

                    leveled_up = False

                    if times_fed >= 4:
                        times_fed = 0
                        new_level += 1
                        leveled_up = True

                        cur.execute(
                            "SELECT * FROM monster_levels WHERE monster = ? AND level = ?",
                            (monster_id, new_level)
                        )
                        next_level_data = cur.fetchone()
                        if not next_level_data:
                            new_level -= 1
                            times_fed = 4

                    # Each feeding grants +5 happiness, capped at 50
                    new_happiness = min(50, (monster_row["happiness"] or 0) + 5)
                    cur_player.execute(
                        "UPDATE player_monsters SET times_fed = ?, level = ?, happiness = ? WHERE user_monster_id = ?",
                        (times_fed, new_level, new_happiness, user_monster_id)
                    )
                    db_player.commit()

                    response = SFSObject()
                    response.put_bool("success", True)
                    await send_extension_response(client, cmd, response)

                    monster_obj = Monster(
                        client.player.active_island,
                        user_monster_id,
                        monster_id,
                        monster_row["pos_x"],
                        monster_row["pos_y"],
                        monster_row["flip"],
                        new_level,
                        new_happiness,
                        monster_row["collected_coins"],
                        times_fed,
                        monster_row["volume"],
                        monster_row["date_created"],
                        monster_row["last_collection"],
                        monster_row["muted"],
                        name=get_monster_name(monster_row)
                    )
                    refresh_cached_monster(client, monster_obj)

                    response2 = SFSObject()
                    response2.put_long("user_monster_id", user_monster_id)
                    response2.put_int("times_fed", times_fed)

                    if leveled_up:
                        response2.put_int("level", new_level)
                        response2.put_long("last_collection", monster_row["last_collection"])
                        response2.put_int("collected_coins", monster_row["collected_coins"])
                        response2.put_int("collected_eth", 0)

                    response2.put_sfs_object("monster", monster_obj.get_sfs_object())
                    response2.put_sfs_array("properties", client.player.get_properties())
                    await send_extension_response(client, "gs_update_monster", response2)

                    response3 = SFSObject()
                    response3.put_bool("success", True)
                    response3.put_sfs_array("properties", client.player.get_properties())
                    await send_extension_response(client, "gs_update_properties", response3)
                elif cmd_lower == "gs_collect_monster":
                    user_monster_id = params.get("user_monster_id")
                    response = SFSObject()

                    cur_player.execute(
                        "SELECT * FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                        (user_monster_id, client.player.active_island)
                    )
                    monster_row = cur_player.fetchone()

                    if not monster_row:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    monster_id = monster_row["monster"]
                    current_level = monster_row["level"]

                    cur.execute(
                        "SELECT * FROM monster_levels WHERE monster = ? AND level = ?",
                        (monster_id, current_level)
                    )
                    mlevel = cur.fetchone()

                    if not mlevel:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Monster level data not found")
                        await send_extension_response(client, cmd, response)
                        continue

                    coins_rate = mlevel["coins"]
                    max_coins = mlevel["max_coins"]

                    last_collection = monster_row["last_collection"] or current_time_ms
                    time_delta_s = (current_time_ms - last_collection) / 5000  

                    # Add any previously collected coins
                    previous_collected = monster_row["collected_coins"]
                    reward = coins_rate * time_delta_s + previous_collected

                    total_collected = min(max_coins, int(reward))

                    if total_collected > max_coins:
                        total_collected = max_coins

                    client.player.add_properties(total_collected)
                    cur_player.execute(
                        "UPDATE player_monsters SET last_collection = ?, collected_coins = 0 WHERE user_monster_id = ?",
                        (current_time_ms, user_monster_id)
                    )
                    db_player.commit()

                    if total_collected > 0:
                        response.put_bool("success", True)
                        response.put_int("coins", total_collected)
                    else:
                        response.put_bool("success", False)
                        response.put_utf_string("message", "nothing to collect")
                    response.put_long("user_monster_id", user_monster_id)
                    await send_extension_response(client, cmd, response)

                    update_response = SFSObject()
                    update_response.put_bool("success", True)
                    update_response.put_long("user_monster_id", user_monster_id)

                    monster_obj = Monster(
                        client.player.active_island,
                        user_monster_id,
                        monster_row["monster"],
                        monster_row["pos_x"],
                        monster_row["pos_y"],
                        monster_row["flip"],
                        monster_row["level"],
                        monster_row["happiness"],
                        monster_row["collected_coins"],
                        monster_row["times_fed"],
                        monster_row["volume"],
                        monster_row["date_created"],
                        monster_row["last_collection"],
                        monster_row["muted"],
                        name=get_monster_name(monster_row)
                    )
                    update_response.put_sfs_object("monster", monster_obj.get_sfs_object())
                    update_response.put_sfs_array("properties", client.player.get_properties())
                    update_response.put_long("last_collection", current_time_ms)
                    update_response.put_int("collected_coins", total_collected)

                    await send_extension_response(client, "gs_update_monster", update_response)

                    props_response = SFSObject()
                    props_response.put_sfs_array("properties", client.player.get_properties())
                    await send_extension_response(client, "gs_update_properties", props_response)
                elif cmd_lower == "gs_mega_monster_message":
                    user_monster_id = params.get("user_monster_id")
                    permanent = params.get("permanent")
                    cost = 20 if permanent else 2
                    duration_ms = 60 * 60 * 24 * 1000

                    cur_player.execute(
                        "SELECT * FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                        (user_monster_id, client.player.active_island)
                    )
                    monster_row = cur_player.fetchone()

                    if not monster_row:
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "DELETE FROM monster_mega_data WHERE finishes_at < ?",
                        (current_time_ms,)
                    )
                    db_player.commit()

                    cur_player.execute(
                        "SELECT * FROM monster_mega_data WHERE user_monster_id = ?",
                        (user_monster_id,)
                    )
                    existing_mega_data = cur_player.fetchone()

                    if existing_mega_data:
                            finishes_at = existing_mega_data["finishes_at"] or 0
                            permamega = existing_mega_data["permamega"]
                            currently_mega = existing_mega_data["currently_mega"]

                            if permamega or finishes_at > current_time_ms:
                                new_mega = 0 if currently_mega else 1
                                cur_player.execute(
                                    "UPDATE monster_mega_data SET currently_mega = ? WHERE user_monster_id = ?",
                                    (new_mega, user_monster_id)
                                )
                                db_player.commit()

                                response2 = SFSObject()
                                response2.put_bool("success", True)
                                response2.put_sfs_array("properties", client.player.get_properties())
                                response2.put_long("user_monster_id", user_monster_id)

                                if new_mega == 0:
                                    megamonster_data = MegaData(
                                        user_monster_id,
                                        permamega,
                                        False,
                                        existing_mega_data["started_at"],
                                        existing_mega_data["finishes_at"]
                                    )
                                    response2.put_sfs_object("megamonster", megamonster_data.get_sfs_object())
                                else:
                                    megamonster_data = MegaData(
                                        user_monster_id,
                                        permamega,
                                        True,
                                        existing_mega_data["started_at"],
                                        existing_mega_data["finishes_at"]
                                    )
                                    response2.put_sfs_object("megamonster", megamonster_data.get_sfs_object())

                                response = SFSObject()
                                response.put_bool("success", True)
                                response.put_long("user_monster_id", user_monster_id)

                                updatedMonster = Monster(
                                    client.player.active_island,
                                    user_monster_id,
                                    monster_row["monster"],
                                    monster_row["pos_x"],
                                    monster_row["pos_y"],
                                    monster_row["flip"],
                                    monster_row["level"],
                                    monster_row["happiness"],
                                    monster_row["collected_coins"],
                                    monster_row["times_fed"],
                                    monster_row["volume"],
                                    monster_row["date_created"],
                                    monster_row["last_collection"],
                                    monster_row["muted"],
                                    mega_data=megamonster_data,
                                    name=get_monster_name(monster_row)
                                )
                                refresh_cached_monster(client, updatedMonster)

                                response2.put_sfs_object("monster", updatedMonster.get_sfs_object())

                                await send_extension_response(client, cmd, response)
                                await send_extension_response(client, "gs_update_monster", response2)
                                continue

                    purchase = not existing_mega_data or (existing_mega_data["finishes_at"] or 0) < current_time_ms

                    end_time = current_time_ms + duration_ms if not permanent else None

                    if purchase and client.player.add_properties(0, -cost, 0, 0) != True:
                        continue

                    if purchase:
                        if permanent:
                            cur_player.execute(
                                """
                                INSERT INTO monster_mega_data (user_monster_id, permamega, currently_mega)
                                VALUES (?, ?, ?)
                                ON CONFLICT(user_monster_id) DO UPDATE SET permamega=excluded.permamega
                                """,
                                (user_monster_id, 1, 1)
                            )
                        else:
                            cur_player.execute(
                                """
                                INSERT INTO monster_mega_data (user_monster_id, started_at, finishes_at, permamega, currently_mega)
                                VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(user_monster_id) DO UPDATE SET started_at=excluded.started_at,
                                                                        finishes_at=excluded.finishes_at,
                                                                        permamega=0,
                                                                        currently_mega=0
                                """,
                                (user_monster_id, current_time_ms, end_time, 0, 1)
                            )

                    db_player.commit()

                    response2 = SFSObject()
                    response2.put_bool("success", True)
                    response2.put_sfs_array("properties", client.player.get_properties())
                    response2.put_long("user_monster_id", user_monster_id)

                    megamonster_data = MegaData(user_monster_id, permanent, True, current_time_ms if not permanent else None, end_time if not permanent else None)
                    response2.put_sfs_object("megamonster", megamonster_data.get_sfs_object())

                    updatedMonster = Monster(
                        client.player.active_island,
                        user_monster_id,
                        monster_row["monster"],
                        monster_row["pos_x"],
                        monster_row["pos_y"],
                        monster_row["flip"],
                        monster_row["level"],
                        monster_row["happiness"],
                        monster_row["collected_coins"],
                        monster_row["times_fed"],
                        monster_row["volume"],
                        monster_row["date_created"],
                        monster_row["last_collection"],
                        monster_row["muted"],
                        mega_data=megamonster_data,
                        name=get_monster_name(monster_row)
                    )
                    refresh_cached_monster(client, updatedMonster)
                    response2.put_sfs_object("monster", updatedMonster.get_sfs_object())

                    response = SFSObject()
                    response.put_bool("success", True)
                    response.put_long("user_monster_id", user_monster_id)

                    await send_extension_response(client, cmd, response)
                    await send_extension_response(client, "gs_update_monster", response2)
                elif cmd_lower == "gs_place_on_gold_island":
                    user_monster_id = int(params.get("user_monster_id"))
                    parent_island_id = int(params.get("user_parent_island_id"))

                    pos_x = params.get("pos_x")
                    pos_y = params.get("pos_y")
                    flip = params.get("flip", 0)

                    response = SFSObject()

                    cur_player.execute(
                        "SELECT * FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                        (user_monster_id, parent_island_id)
                    )

                    parent_monster = cur_player.fetchone()

                    if not parent_monster:
                        print("Not monster")
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "SELECT * FROM player_islands WHERE user_island_id = ? AND island_id = 6",
                        (client.player.active_island,)
                    )
                    gold_island = cur_player.fetchone()
                    if not gold_island:
                        print("not gold island")
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid Gold Island ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute("""
                        INSERT INTO player_gi_monsters (
                            user_monster_id,
                            monster_parent_id,
                            island_parent_id,
                            pos_x,
                            pos_y,
                            flip,
                            date_created,
                            bbb_id
                        )
                        SELECT 
                            COALESCE(MAX(m.user_monster_id), 0) + 1,
                            ?, ?, ?, ?, ?, ?, ?
                        FROM (
                            SELECT user_monster_id FROM player_monsters
                            UNION ALL
                            SELECT user_monster_id FROM player_gi_monsters
                        ) AS m
                    """, (
                        parent_monster["user_monster_id"],
                        parent_island_id,
                        pos_x,
                        pos_y,
                        flip,
                        current_time_ms,
                        client.player.bbb_id
                    ))

                    gi_monster_id = cur_player.lastrowid
                    db_player.commit()

                    response.put_bool("success", True)
                    response.put_sfs_array("properties", client.player.get_properties())
                    response.put_long("user_monster_id", user_monster_id)

                    gi_monster = Monster(
                        client.player.active_island,
                        gi_monster_id,
                        parent_monster["monster"],
                        pos_x,
                        pos_y,
                        flip,
                        parent_monster["level"],
                        100,
                        parent_monster["collected_coins"],
                        parent_monster["times_fed"],
                        parent_monster["volume"],
                        parent_monster["date_created"],
                        parent_monster["last_collection"],
                        0,
                        parent_island_id=parent_island_id,
                        parent_monster_id=parent_monster["user_monster_id"],
                        name=get_monster_name(parent_monster)
                    )

                    cur_player.execute("""
                        SELECT * FROM monster_mega_data WHERE user_monster_id = ?
                    """, (parent_monster["user_monster_id"],))
                    mega_data = cur_player.fetchone()
                    if mega_data:
                        gi_monster.mega_data = MegaData(
                            user_monster_id=parent_monster["user_monster_id"],
                            permamega=mega_data["permamega"],
                            currently_mega=mega_data["currently_mega"],
                            started_at=mega_data["started_at"] or None,
                            finishes_at=mega_data["finishes_at"] or None
                        )

                    refresh_cached_monster(client, gi_monster)

                    response.put_sfs_object("monster", gi_monster.get_sfs_object())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_sell_monster":
                    user_monster_id = params.get("user_monster_id")

                    cur_player.execute(
                        """
                        SELECT monster FROM player_monsters
                        WHERE user_monster_id = ? AND user_island_id = ?
                        """,
                        (user_monster_id, client.player.active_island)
                    )

                    player_monster = cur_player.fetchone()
                    if not player_monster:
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    monster_id = player_monster["monster"]

                    cur.execute("SELECT * FROM monsters WHERE monster_id = ?", (monster_id,))
                    row = cur.fetchone()
                    if not row:
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster data")
                        await send_extension_response(client, cmd, response)
                        continue

                    sell_entity(client, row["entity"])

                    cur_player.execute(
                        """
                        DELETE FROM player_monsters
                        WHERE user_monster_id = ? AND user_island_id = ?
                        """,
                        (user_monster_id, client.player.active_island)
                    )
                    db_player.commit()

                    response = SFSObject()
                    response.put_bool("success", True)
                    response.put_long("user_monster_id", user_monster_id)
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_sell_structure":
                    user_structure_id = params.get("user_structure_id")

                    cur_player.execute(
                        """
                        SELECT structure FROM player_structures
                        WHERE user_structure_id = ? AND user_island_id = ?
                        """,
                        (user_structure_id, client.player.active_island)
                    )
                    structure_id = cur_player.fetchone()["structure"]

                    cur.execute("SELECT * FROM structures WHERE structure_id = ?", (structure_id,))
                    row = cur.fetchone()

                    sell_entity(client, row["entity"])

                    cur_player.execute(
                        """
                        DELETE FROM player_structures
                        WHERE user_structure_id = ? AND user_island_id = ?
                        """,
                        (user_structure_id, client.player.active_island)
                    )
                    db_player.commit()

                    response = SFSObject()
                    response.put_bool("success", True)
                    response.put_long("user_structure_id", user_structure_id)
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_clear_obstacle":
                    user_structure_id = params.get("user_structure_id")

                    cur_player.execute(
                        """
                        SELECT structure FROM player_structures
                        WHERE user_structure_id = ? AND user_island_id = ?
                        """,
                        (user_structure_id, client.player.active_island)
                    )
                    structure_row = cur_player.fetchone()

                    if structure_row is None:
                        print("no structure")
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid structure ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    structure_id = structure_row["structure"]
                    # Fetch static structure/entity data (cost + xp reward)
                    cur.execute("SELECT * FROM structures WHERE structure_id = ?", (structure_id,))
                    struct_static = cur.fetchone()

                    cur.execute("SELECT * FROM entities WHERE entity_id = ?", (struct_static["entity"],))
                    entity_row = cur.fetchone()

                    # Deduct the clearing cost before removing the obstacle
                    cost_coins = 0
                    cost_diamonds = 0
                    if entity_row is not None:
                        try:
                            cost_coins = entity_row["cost_coins"] or 0
                        except Exception:
                            cost_coins = 0
                        try:
                            cost_diamonds = entity_row["cost_diamonds"] or 0
                        except Exception:
                            cost_diamonds = 0

                    if not client.player.add_properties(coins=-cost_coins, diamonds=-cost_diamonds):
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Not enough resources to clear obstacle")
                        await send_extension_response(client, cmd, response)
                        continue

                    # Attempt to delete — do this after cost check to avoid race conditions
                    cur_player.execute(
                        """
                        DELETE FROM player_structures
                        WHERE user_structure_id = ? AND user_island_id = ?
                        """,
                        (user_structure_id, client.player.active_island)
                    )
                    db_player.commit()

                    # If no row was deleted, the obstacle was already removed — refund
                    if cur_player.rowcount == 0:
                        client.player.add_properties(coins=cost_coins, diamonds=cost_diamonds)
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Structure already removed")
                        await send_extension_response(client, cmd, response)
                        continue

                    # Award XP only after successful deletion
                    xp_reward = 0
                    if entity_row is not None:
                        try:
                            xp_reward = entity_row["xp"] or 0
                        except Exception:
                            xp_reward = 0

                    if xp_reward:
                        client.player.add_properties(0, 0, 0, xp_reward, 0)

                    response = SFSObject()
                    response.put_bool("success", True)
                    response.put_long("user_structure_id", user_structure_id)
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_move_structure":
                    user_structure_id = params.get("user_structure_id")
                    new_x = params.get("pos_x")
                    new_y = params.get("pos_y")
                    scale = params.get("scale")

                    cur_player.execute(
                        "UPDATE player_structures SET pos_x = ?, pos_y = ? WHERE user_structure_id = ? AND user_island_id = ?",
                        (new_x, new_y, user_structure_id, client.player.active_island)
                    )
                    db_player.commit()

                    response = SFSObject()

                    properties = client.player.get_properties()

                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE user_structure_id = ? AND user_island_id = ?",
                        (user_structure_id, client.player.active_island)
                    )
                    row = cur_player.fetchone()

                    structure_id = row["structure"]
                    flip = row["flip"]
                    date_created = row["date_created"]
                    building_completed = row["building_completed"]
                    last_collection = row["last_collection"]
                    obj_data = row["obj_data"] if "obj_data" in row.keys() else None
                    obj_end = row["obj_end"] if "obj_end" in row.keys() else None

                    newStructure = Structure(
                        client.player.active_island,
                        user_structure_id,
                        structure_id,
                        new_x,
                        new_y,
                        flip,
                        scale,
                        date_created,
                        building_completed,
                        last_collection,
                        obj_data,
                        obj_end,
                    )
                    prop = SFSObject()
                    prop.put_int("pos_x", new_x)
                    properties.add_sfs_object(prop)

                    prop = SFSObject()
                    prop.put_int("pos_y", new_y)
                    properties.add_sfs_object(prop)

                    prop = SFSObject()
                    prop.put_double("scale", scale)
                    properties.add_sfs_object(prop)

                    response.put_sfs_array("properties", properties)

                    response.put_long("user_structure_id", user_structure_id)
                    response.put_sfs_object("user_structure", newStructure.get_sfs_object())
                    response.put_bool("success", True)
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_flip_structure":
                    user_structure_id = params.get("user_structure_id")

                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE user_structure_id = ? AND user_island_id = ?",
                        (user_structure_id, client.player.active_island)
                    )
                    row = cur_player.fetchone()

                    response = SFSObject()
                    if not row:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid structure ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    new_flip = 0 if row["flip"] else 1

                    cur_player.execute(
                        "UPDATE player_structures SET flip = ? WHERE user_structure_id = ? AND user_island_id = ?",
                        (new_flip, user_structure_id, client.player.active_island)
                    )
                    db_player.commit()

                    newStructure = Structure(
                        client.player.active_island,
                        user_structure_id,
                        row["structure"],
                        row["pos_x"],
                        row["pos_y"],
                        new_flip,
                        row["scale"],
                        row["date_created"],
                        row["building_completed"] if "building_completed" in row.keys() else None,
                        row["last_collection"] if "last_collection" in row.keys() else None,
                        row["obj_data"] if "obj_data" in row.keys() else None,
                        row["obj_end"] if "obj_end" in row.keys() else None,
                    )

                    flip_resp = SFSObject()
                    flip_resp.put_bool("success", True)
                    await send_extension_response(client, "gs_flip_structure", flip_resp)

                    props = SFSArray()
                    props.add_sfs_object(SFSObject().put_int("flip", new_flip))

                    update_resp = SFSObject()
                    update_resp.put_long("user_structure_id", user_structure_id)
                    update_resp.put_sfs_object("user_structure", newStructure.get_sfs_object())
                    update_resp.put_sfs_array("properties", props)
                    update_resp.put_bool("success", True)
                    await send_extension_response(client, "gs_update_structure", update_resp)
                elif cmd_lower == "gs_flip_monster":
                    user_monster_id = params.get("user_monster_id")
                    flipped = params.get("flipped")

                    response = SFSObject()

                    cur_player.execute(
                        """
                        SELECT * FROM player_monsters
                        WHERE user_monster_id = ? AND user_island_id = ?
                        """,
                        (user_monster_id, client.player.active_island)
                    )
                    monster_row = cur_player.fetchone()

                    if not monster_row:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    if flipped is not None:
                        new_flip = 1 if flipped else 0
                    else:
                        new_flip = 0 if monster_row["flip"] else 1

                    cur_player.execute(
                        """
                        UPDATE player_monsters
                        SET flip = ?
                        WHERE user_monster_id = ? AND user_island_id = ?
                        """,
                        (new_flip, user_monster_id, client.player.active_island)
                    )
                    db_player.commit()

                    updatedMonster = Monster(
                        client.player.active_island,
                        user_monster_id,
                        monster_row["monster"],
                        monster_row["pos_x"],
                        monster_row["pos_y"],
                        new_flip,
                        monster_row["level"],
                        monster_row["happiness"],
                        monster_row["collected_coins"],
                        monster_row["times_fed"],
                        monster_row["volume"],
                        monster_row["date_created"],
                        monster_row["last_collection"],
                        monster_row["muted"],
                        name=get_monster_name(monster_row)
                    )

                    flip_resp = SFSObject()
                    flip_resp.put_bool("success", True)
                    await send_extension_response(client, "gs_flip_monster", flip_resp)

                    update_resp = SFSObject()
                    update_resp.put_bool("success", True)
                    update_resp.put_long("user_monster_id", user_monster_id)
                    update_resp.put_int("flip", new_flip)
                    update_resp.put_sfs_object("monster", updatedMonster.get_sfs_object())
                    await send_extension_response(client, "gs_update_monster", update_resp)
                elif cmd_lower == "gs_collect_scratch_off":
                    response = SFSObject()

                    response.put_bool("success", True)
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_name_monster":
                    user_monster_id = params.get("user_monster_id")
                    monster_name = params.get("name") or params.get("monster_name") or params.get("newName")
                    response = SFSObject()

                    if not user_monster_id:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    if monster_name is None:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster name")
                        await send_extension_response(client, cmd, response)
                        continue

                    monster_name = sanitize_name(str(monster_name), ALPHABET).strip()
                    invalid_reason = invalid_name(monster_name)
                    if invalid_reason:
                        response.put_bool("success", False)
                        response.put_utf_string("error", invalid_reason)
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "SELECT * FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                        (user_monster_id, client.player.active_island)
                    )
                    monster_row = cur_player.fetchone()

                    if not monster_row:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "UPDATE player_monsters SET name = ? WHERE user_monster_id = ? AND user_island_id = ?",
                        (monster_name, user_monster_id, client.player.active_island)
                    )
                    db_player.commit()

                    active_island = client.player.get_active_island()
                    if active_island:
                        target_monster = active_island.find_monster(user_monster_id)
                        if target_monster:
                            target_monster.name = monster_name

                    response.put_bool("success", True)
                    await send_extension_response(client, cmd, response)

                    cur_player.execute(
                        "SELECT * FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                        (user_monster_id, client.player.active_island)
                    )
                    updated_monster_row = cur_player.fetchone()

                    if updated_monster_row:
                        updated_monster = Monster(
                            client.player.active_island,
                            updated_monster_row["user_monster_id"],
                            updated_monster_row["monster"],
                            updated_monster_row["pos_x"],
                            updated_monster_row["pos_y"],
                            updated_monster_row["flip"],
                            updated_monster_row["level"],
                            updated_monster_row["happiness"],
                            updated_monster_row["collected_coins"],
                            updated_monster_row["times_fed"],
                            updated_monster_row["volume"],
                            updated_monster_row["date_created"],
                            updated_monster_row["last_collection"],
                            updated_monster_row["muted"],
                            name=get_monster_name(updated_monster_row)
                        )

                        update_resp = SFSObject()
                        update_resp.put_bool("success", True)
                        update_resp.put_long("user_monster_id", user_monster_id)
                        update_resp.put_sfs_object("monster", updated_monster.get_sfs_object())
                        await send_extension_response(client, "gs_update_monster", update_resp)
                elif cmd_lower == "gs_collect_from_mine":
                    user_structure_id = params.get("user_structure_id")
                    response = SFSObject()

                    response.put_long("user_structure_id", user_structure_id)
                    properties = client.player.get_properties()
                    properties.add_sfs_object(SFSObject().put_long("last_collection", current_time_ms))
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, "gs_update_structure", response)
                elif cmd_lower == "gs_get_torchgifts":
                    response = SFSObject()
                    
                    response.put_bool("success", True)
                    
                    response.put_sfs_array("torch_gifts", SFSArray())
                    
                    properties_array = SFSArray()
                    prop = SFSObject()

                    prop.put_sfs_array("can_gift_torch_times", SFSArray())
                    properties_array.add_sfs_object(prop)
                    
                    response.put_sfs_array("properties", properties_array)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_light_torch":
                    response = SFSObject()

                    is_permanent = bool(
                        params.get("permanent")
                        or params.get("is_permanent")
                        or params.get("permalit")
                    )
                    cost_key = (
                        "USER_DIAMOND_COST_PER_PERMALIT_TORCH"
                        if is_permanent
                        else "USER_DIAMOND_COST_PER_LIT_TORCH"
                    )
                    try:
                        cost = int(float(get_game_setting_from_key(cost_key) or (100 if is_permanent else 2)))
                    except (TypeError, ValueError):
                        cost = 100 if is_permanent else 2

                    if not client.player.add_properties(diamonds=-cost):
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Not enough diamonds")
                        await send_extension_response(client, cmd, response)
                        continue

                    response.put_bool("success", True)
                    response.put_long("user_island_id", int(params.get("user_island_id") or client.player.active_island))
                    response.put_utf_string("animation", "light_torch")
                    response.put_sfs_array("properties", client.player.get_properties())

                    system_payload = SFSObject()
                    system_payload.put_bool("force_logout", False)
                    system_payload.put_utf_string("msg", "Torch lit successfully.")
                    response.put_sfs_object("system", system_payload)

                    animations = SFSArray()
                    animation = SFSObject()
                    animation.put_utf_string("animation", "light_torch")
                    animation.put_utf_string("animation_alias", "torch_lighting")
                    animation.put_long("user_island_id", int(params.get("user_island_id") or client.player.active_island))
                    animations.add_sfs_object(animation)
                    response.put_sfs_array("animations", animations)

                    await send_extension_response(client, cmd, response)

                    await send_extension_response(client, "gs_display_generic_message", system_payload)
                elif cmd_lower == "gs_breed_monsters":
                    user_monster_id_1 = params.get("user_monster_id_1")
                    user_monster_id_2 = params.get("user_monster_id_2")
                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE structure = 2 AND user_island_id = ?",
                        (client.player.active_island,)
                    )
                    row = cur_player.fetchone()
                    user_structure_id = row["user_structure_id"]

                    if user_structure_id is None:
                        print("no structure id")
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Structure ID is required")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "SELECT * FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                        (user_monster_id_1, client.player.active_island)
                    )
                    monster1 = cur_player.fetchone()

                    cur_player.execute(
                        "SELECT * FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                        (user_monster_id_2, client.player.active_island)
                    )
                    monster2 = cur_player.fetchone()

                    if monster1 is None or monster2 is None:
                        print("Cant find monsters")
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster IDs")
                        await send_extension_response(client, cmd, response)
                        continue

                    monster_id_1 = monster1["monster"]
                    monster_id_2 = monster2["monster"]

                    result = get_breeding_result(monster_id_1, monster_id_2, monster1["level"], monster2["level"], client.player.level)

                    response = SFSObject()

                    cur.execute("SELECT * FROM monsters WHERE monster_id = ?", (result,))

                    result_row = cur.fetchone()

                    if result_row is None:
                        print("No result")
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Breeding result not found")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur.execute("SELECT * FROM entities WHERE entity_id = ?", (result_row["entity"],))

                    entity_row = cur.fetchone()

                    end_time = current_time_ms + (entity_row["build_time"] * 1000)

                    response.put_bool("success", True)
                    response.put_long("last_bred_monster_1", monster1["user_monster_id"])
                    response.put_long("last_bred_monster_2", monster2["user_monster_id"])

                    cur_player.execute("""
                        INSERT INTO player_breeding (user_island_id, started_on, completes_on, result, monster_1, monster_2, user_structure_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (client.player.active_island, current_time_ms, end_time, result, monster_id_1, monster_id_2, user_structure_id))

                    db_player.commit()
                    user_breeding_id = cur_player.lastrowid

                    user_breeding = Breeding(client.player.active_island, user_breeding_id, user_structure_id, monster_id_1, monster_id_2, result, current_time_ms, end_time)

                    response.put_sfs_object("user_breeding", user_breeding.get_sfs_object())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_finish_breeding":
                    response = SFSObject()

                    user_breeding_id = params.get("user_breeding_id")

                    cur_player.execute("SELECT * FROM player_breeding WHERE user_breeding_id = ?", (user_breeding_id,))
                    breeding_row = cur_player.fetchone()

                    if breeding_row is None:
                        continue

                    cur.execute("SELECT * FROM monsters WHERE monster_id = ?", (breeding_row["result"],))
                    row = cur.fetchone()

                    cur.execute("SELECT * FROM entities WHERE entity_id = ?", (row["entity"],))
                    row2 = cur.fetchone()

                    if buy_entity(client, row["entity"]) != True:
                        continue

                    endtime = current_time_ms + (row2["build_time"] * 1000)

                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE user_island_id = ? AND structure = 1",
                        (client.player.active_island,)
                    )

                    structure_row = cur_player.fetchone()

                    if row is None:
                        print("Error")
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Error")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        """
                        DELETE FROM player_breeding
                        WHERE user_breeding_id = ? AND user_island_id = ?
                        """,
                        (user_breeding_id, client.player.active_island)
                    )
                    db_player.commit()

                    cur_player.execute(
                        "INSERT INTO player_eggs (user_island_id, laid_on, hatches_on, monster, user_structure_id) VALUES (?, ?, ?, ?, ?)",
                        (client.player.active_island, current_time_ms, endtime, breeding_row["result"], structure_row["user_structure_id"])
                    )
                    db_player.commit()
                    user_egg_id = cur_player.lastrowid

                    egg = Egg(client.player.active_island, current_time_ms, endtime, breeding_row["result"], user_egg_id, structure_row["user_structure_id"])

                    response.put_sfs_object("user_egg", egg.get_sfs_object())

                    response.put_bool("success", True)
                    response.put_long("user_breeding_id", user_breeding_id)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_speed_up_breeding":
                    user_breeding_id = params.get("user_breeding_id")

                    response = SFSObject()

                    cur_player.execute(
                        "SELECT * FROM player_breeding WHERE user_island_id = ? AND user_breeding_id = ?",
                        (client.player.active_island, user_breeding_id)
                    )
                    row = cur_player.fetchone()

                    if row is None:
                        response = SFSObject()
                        response.put_bool("success", False)
                        response.put_utf_string("message", "Error")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "UPDATE player_breeding SET completes_on = ? WHERE user_breeding_id = ?",
                        (current_time_ms, user_breeding_id)
                    )
                    db_player.commit()

                    response.put_bool("success", True)
                    response.put_long("userBreedingId", user_breeding_id)
                    response.put_long("complete_on", current_time_ms)
                    response.put_long("started_on", row["started_on"])

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_player":
                    response = SFSObject()

                    response.put_sfs_object("player_object", client.player.get_sfs_object())

                    print(response)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_get_island_rank":
                    response = SFSObject()

                    user_island_id = params.get("island_id")

                    cur_player.execute("""
                    SELECT
                        pi.user_island_id,
                        pi.likes - pi.dislikes AS score,
                        CASE
                            WHEN pi.likes - pi.dislikes <= 0 THEN 0
                            WHEN (
                                SELECT COUNT(*) 
                                FROM player_islands AS other
                                WHERE other.likes - other.dislikes > pi.likes - pi.dislikes
                            ) < 10 THEN 10
                            WHEN (
                                SELECT COUNT(*) 
                                FROM player_islands AS other
                                WHERE other.likes - other.dislikes > pi.likes - pi.dislikes
                            ) < 100 THEN 100
                            WHEN (
                                SELECT COUNT(*) 
                                FROM player_islands AS other
                                WHERE other.likes - other.dislikes > pi.likes - pi.dislikes
                            ) < 500 THEN 500
                            WHEN (
                                SELECT COUNT(*) 
                                FROM player_islands AS other
                                WHERE other.likes - other.dislikes > pi.likes - pi.dislikes
                            ) < 1000 THEN 1000
                            ELSE 0
                        END AS rank_tier
                    FROM player_islands pi
                    WHERE pi.user_island_id = ?
                    LIMIT 1
                    """, (user_island_id,))

                    row = cur_player.fetchone()

                    if row:
                        response.put_bool("success", True)
                        response.put_int("rank", row[2])
                        response.put_long("island_id", user_island_id)
                    else:
                        response.put_bool("success", False)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_get_friend_visit_data":
                    friend_id = params.get("user_id")

                    response = SFSObject()

                    cur_player.execute(
                        "SELECT active_island, display_name FROM players WHERE bbb_id = ?",
                        (friend_id,)
                    )
                    row2 = cur_player.fetchone()

                    friendPlayer = Player(friend_id, row2["display_name"], friend_id)

                    friendPlayer.active_island = row2["active_island"]

                    cur_player.execute("""
                        SELECT * FROM player_islands WHERE bbb_id = ?
                    """, (friend_id,))

                    islands = cur_player.fetchall()
                    for islandData in islands:
                        island = Island(friend_id, islandData["island_id"], islandData["user_island_id"])

                        island.likes = islandData["likes"]
                        island.dislikes = islandData["dislikes"]

                        island.add_player_monsters()
                        island.add_player_structures()
                        island.add_player_eggs()
                        island.add_player_breedings()

                        friendPlayer.add_island(island)

                    response.put_bool("success", True)
                    response.put_sfs_object("friend_object", friendPlayer.get_sfs_object())

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_get_ranked_island_data":
                    offset = params.get("weekly_rank") - 1

                    cur_player.execute("""
                        SELECT user_island_id, bbb_id, island_id, likes, dislikes,
                            (likes - dislikes) AS score
                        FROM player_islands
                        WHERE likes >= dislikes
                        ORDER BY score DESC, likes DESC
                        LIMIT 1 OFFSET ?
                    """, (offset,))
                    row = cur_player.fetchone()

                    if row is None:
                        msg = SFSObject()
                        msg.put_bool("force_logout", False)
                        msg.put_utf_string("msg", "No ranked island found")

                        await send_extension_response(client, "gs_display_generic_message", msg)
                        continue

                    cur_player.execute(
                        "SELECT active_island, display_name FROM players WHERE bbb_id = ?",
                        (row["bbb_id"],)
                    )
                    row2 = cur_player.fetchone()

                    friendPlayer = Player(row["bbb_id"], row2["display_name"], row["bbb_id"])

                    friendPlayer.active_island = row2["active_island"]

                    cur_player.execute("""
                        SELECT * FROM player_islands WHERE bbb_id = ?
                    """, (row["bbb_id"],))

                    islands = cur_player.fetchall()
                    for islandData in islands:
                        island = Island(row["bbb_id"], islandData["island_id"], islandData["user_island_id"])

                        island.likes = islandData["likes"]
                        island.dislikes = islandData["dislikes"]

                        island.add_player_monsters()
                        island.add_player_structures()
                        island.add_player_eggs()
                        island.add_player_breedings()

                        friendPlayer.add_island(island)

                    response = SFSObject()
                    
                    response.put_long("ranked_island_id", row["user_island_id"])
                    response.put_long("user_island_id", row["user_island_id"])
                    response.put_sfs_object("friend_object", friendPlayer.get_sfs_object())
                    response.put_int("weekly_rank", offset + 1)
                    response.put_long("num_ranked_islands", 10)
                    response.put_bool("island_rated", False)
                    response.put_bool("success", True)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_get_random_visit_data":
                    cur_player.execute("""
                        SELECT * FROM player_islands
                        ORDER BY RANDOM()
                        LIMIT 1
                    """)
                    row = cur_player.fetchone()

                    cur_player.execute(
                        "SELECT active_island, display_name FROM players WHERE bbb_id = ?",
                        (row["bbb_id"],)
                    )
                    row2 = cur_player.fetchone()

                    friendPlayer = Player(row["bbb_id"], row2["display_name"], row["bbb_id"])

                    friendPlayer.active_island = row["user_island_id"]

                    cur_player.execute("""
                        SELECT * FROM player_islands WHERE bbb_id = ?
                    """, (row["bbb_id"],))

                    islands = cur_player.fetchall()
                    for islandData in islands:
                        island = Island(row["bbb_id"], islandData["island_id"], islandData["user_island_id"])

                        island.likes = islandData["likes"]
                        island.dislikes = islandData["dislikes"]

                        island.add_player_monsters()
                        island.add_player_structures()
                        island.add_player_eggs()
                        island.add_player_breedings()

                        friendPlayer.add_island(island)

                    response = SFSObject()
                    response.put_long("user_island", row["user_island_id"])
                    response.put_sfs_object("friend_object", friendPlayer.get_sfs_object())
                    response.put_bool("island_rated", False)
                    response.put_bool("success", True)

                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_rate_island":
                    liked = params.get("liked")
                    column = "likes" if liked else "dislikes"

                    friend_island_id = params.get("friend_island_id")

                    cur_player.execute(
                        f"UPDATE player_islands SET {column} = {column} + 1 WHERE user_island_id = ?",
                        (friend_island_id,)
                    )
                    db_player.commit()

                    response = SFSObject()
                    response.put_bool("success", True)
                    await send_extension_response(client, cmd, response)
                elif cmd_lower == "gs_currency_conversion":
                    # 50,1000000

                    if client.player.add_properties(diamonds=-50) != True:
                        continue

                    if client.player.add_properties(coins=1000000) != True:
                        continue
                    response = SFSObject()
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, "gs_update_properties", response)
                elif cmd_lower == "gs_currency_coins2eth_conversion":
                    # 500000,50

                    if client.player.add_properties(coins=-500000) != True:
                        continue

                    if client.player.add_properties(shards=50) != True:
                        continue

                    response = SFSObject()
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, "gs_update_properties", response)
                elif cmd_lower == "gs_currency_diamonds2eth_conversion":
                    # 50,100

                    if client.player.add_properties(diamonds=-50) != True:
                        continue

                    if client.player.add_properties(shards=100) != True:
                        continue
                    
                    response = SFSObject()
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, "gs_update_properties", response)
                elif cmd_lower == "gs_currency_eth2diamonds_conversion":
                    # 30000,1

                    if client.player.add_properties(shards=-30000) != True:
                        continue

                    if client.player.add_properties(diamonds=1) != True:
                        continue
                    
                    response = SFSObject()
                    response.put_sfs_array("properties", client.player.get_properties())

                    await send_extension_response(client, "gs_update_properties", response)
                elif cmd in ("g5_send_monster_home", "gs_send_monster_home"):
                    user_monster_id = params.get("user_monster_id")
                    response = SFSObject()

                    if not user_monster_id:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    cur_player.execute(
                        "SELECT * FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                        (user_monster_id, client.player.active_island)
                    )
                    monster_row = cur_player.fetchone()
                    was_gi_monster = False

                    if not monster_row:
                        cur_player.execute(
                            "SELECT * FROM player_gi_monsters WHERE user_monster_id = ? AND bbb_id = ?",
                            (user_monster_id, client.player.bbb_id)
                        )
                        gi_row = cur_player.fetchone()
                        if gi_row:
                            cur_player.execute(
                                "SELECT * FROM player_monsters WHERE user_monster_id = ?",
                                (gi_row["monster_parent_id"],)
                            )
                            monster_row = cur_player.fetchone()
                            was_gi_monster = True

                    if not monster_row:
                        response.put_bool("success", False)
                        response.put_utf_string("error", "Invalid monster ID")
                        await send_extension_response(client, cmd, response)
                        continue

                    monster_id = monster_row["monster"]
                    original_island_id = monster_row["user_island_id"]

                    if was_gi_monster:
                        cur_player.execute(
                            "DELETE FROM player_gi_monsters WHERE user_monster_id = ? AND bbb_id = ?",
                            (user_monster_id, client.player.bbb_id)
                        )
                    else:
                        cur_player.execute(
                            "DELETE FROM player_monsters WHERE user_monster_id = ? AND user_island_id = ?",
                            (user_monster_id, client.player.active_island)
                        )
                    db_player.commit()

                    target_island_id = SHUGGA_ISLAND_ID
                    cur_player.execute(
                        "SELECT island_id FROM player_islands WHERE user_island_id = ?",
                        (original_island_id,)
                    )
                    origin_island_row = cur_player.fetchone()
                    if origin_island_row:
                        static_cur.execute(
                            "SELECT dest_island FROM monster_island_2_island_map WHERE source_island = ? AND source_monster = ?",
                            (origin_island_row["island_id"], monster_id)
                        )
                        mapping_row = static_cur.fetchone()
                        if mapping_row:
                            target_island_id = mapping_row["dest_island"]

                    cur_player.execute(
                        "SELECT user_island_id FROM player_islands WHERE bbb_id = ? AND island_id = ?",
                        (client.player.bbb_id, target_island_id)
                    )
                    target_row = cur_player.fetchone()

                    if not target_row:
                        cur_player.execute(
                            "INSERT INTO player_islands (bbb_id, date_created, island_id) VALUES (?, ?, ?)",
                            (client.player.bbb_id, current_time_ms, target_island_id)
                        )
                        db_player.commit()
                        target_user_island_id = cur_player.lastrowid
                        new_island = Island(client.player.bbb_id, target_island_id, target_user_island_id)
                        new_island.create_structures()
                        client.player.add_island(new_island)
                    else:
                        target_user_island_id = target_row["user_island_id"]

                    cur_player.execute(
                        "SELECT * FROM player_structures WHERE user_island_id = ? AND structure = 1 AND is_complete = 1 LIMIT 1",
                        (target_user_island_id,)
                    )
                    structure_row = cur_player.fetchone()

                    if not structure_row:
                        cur_player.execute(
                            "INSERT INTO player_structures (user_island_id, date_created, pos_x, pos_y, flip, muted, is_complete, is_upgrading, structure, scale, building_completed, last_collection, obj_data, obj_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (target_user_island_id, current_time_ms, 35, 17, 0, 0, 0, 0, 1, 1.0, current_time_ms, current_time_ms, 0, 0)
                        )
                        db_player.commit()
                        cur_player.execute(
                            "SELECT * FROM player_structures WHERE user_island_id = ? AND structure = 1 AND user_structure_id = ?",
                            (target_user_island_id, cur_player.lastrowid)
                        )
                        structure_row = cur_player.fetchone()

                    hatch_time = current_time_ms
                    cur.execute("SELECT * FROM monsters WHERE monster_id = ?", (monster_id,))
                    static_monster = cur.fetchone()
                    if static_monster:
                        cur.execute("SELECT * FROM entities WHERE entity_id = ?", (static_monster["entity"],))
                        entity_row = cur.fetchone()
                        if entity_row:
                            hatch_time = current_time_ms + (entity_row["build_time"] * 1000)

                    user_egg_id = None
                    egg = None
                    if structure_row:
                        cur_player.execute(
                            "INSERT INTO player_eggs (user_island_id, laid_on, hatches_on, monster, user_structure_id) VALUES (?, ?, ?, ?, ?)",
                            (target_user_island_id, current_time_ms, hatch_time, monster_id, structure_row["user_structure_id"])
                        )
                        db_player.commit()
                        user_egg_id = cur_player.lastrowid
                        egg = Egg(target_user_island_id, current_time_ms, hatch_time, monster_id, user_egg_id, structure_row["user_structure_id"])

                        if client.player.active_island == target_user_island_id:
                            plrisland = client.player.get_active_island()
                            if plrisland:
                                plrisland.add_egg(egg)

                    if client.player.active_island != target_user_island_id:
                        cur_player.execute(
                            "UPDATE players SET active_island = ? WHERE bbb_id = ?",
                            (target_user_island_id, client.player.bbb_id)
                        )
                        db_player.commit()
                        client.player.active_island = target_user_island_id

                    response.put_bool("success", True)
                    response.put_long("user_monster_id", user_monster_id)
                    response.put_long("monster_id", monster_id)
                    response.put_long("user_island_id", target_user_island_id)
                    response.put_utf_string("animation", "egg_laying")
                    response.put_sfs_array("properties", client.player.get_properties())
                    if egg is not None:
                        response.put_long("user_egg_id", user_egg_id)
                        response.put_sfs_object("user_egg", egg.get_sfs_object())

                    await send_extension_response(client, cmd, response)

                    change_response = SFSObject()
                    change_response.put_bool("success", True)
                    change_response.put_long("user_island_id", target_user_island_id)

                    island_to_send = Island(client.player.bbb_id, target_island_id, target_user_island_id)
                    island_to_send.add_player_monsters()
                    island_to_send.add_player_structures()
                    island_to_send.add_player_eggs()
                    island_to_send.add_player_breedings()
                    change_response.put_sfs_object("user_island", island_to_send.get_sfs_object())

                    hidden_objects = SFSObject()
                    hidden_objects.put_sfs_array("objects", SFSArray())
                    change_response.put_sfs_object("hidden_objects", hidden_objects)
                    await send_extension_response(client, "gs_change_island", change_response)

                    system_msg = SFSObject()
                    system_msg.put_bool("force_logout", False)
                    system_msg.put_utf_string("msg", "Monster sent home and an egg appeared on Ethereal Island.")
                    await send_extension_response(client, "gs_display_generic_message", system_msg)
                elif cmd_lower == "keep_alive" or cmd_lower == "gs_multi_neighbors" or cmd_lower == "gs_get_messages" or cmd_lower == "gs_handle_facebook_help_instances" or cmd_lower == "gs_process_unclaimed_purchases":
                    response = SFSObject()
                    await send_extension_response(client, cmd, response)
                else:
                
                    response = SFSObject()

                    print(params)

                    msg = SFSObject()
                    msg.put_bool("force_logout", False)
                    msg.put_utf_string("msg", f"{cmd} is not implemented yet")

                    await send_extension_response(client, "gs_display_generic_message", msg)

                    await send_extension_response(client, cmd, response)
    except Exception as e:
        print(f"Error with client {client.host}:{client.port}: {e}")
        traceback.print_exc()
    finally:
        CURRENT_PLAYERS -= 1
        try:
            if hasattr(client, "player") and getattr(client, "player") is not None:
                CONNECTED_CLIENTS.pop(client.player.bbb_id, None)
        except Exception:
            pass
        print(f"Client {client.host}:{client.port} disconnected")

async def run_server(ip: str, port: int):
    global EVENT_LOOP
    EVENT_LOOP = asyncio.get_running_loop()
    threading.Thread(target=admin_console_loop, daemon=True).start()
    print(f"Began server at {ip}:{port}")
    asyncio.create_task(poll_pending_commands())
    async for client in server_from_url(f"tcp://{ip}:{port}"):
        print(f"New client connected: {client.host}:{client.port}")
        asyncio.create_task(handle_client(client))

if __name__ == "__main__":
    create_player_tables()
    reset_all_player_stats()
    load_static_data()
    tried_addrs = [GAME_SERVER_IP, "0.0.0.0", "127.0.0.1"]
    bound = False
    for addr in tried_addrs:
        try:
            print(f"Attempting to start game server on {addr}:9933")
            asyncio.run(run_server(addr, 9933))
            bound = True
            break
        except OSError as e:
            print(f"[!] failed to bind to {addr}:9933: {e}")
        except Exception as e:
            print(f"[!] server error while binding to {addr}:9933: {e}")

    if not bound:
        print("[!] could not bind game server to any address; check network interfaces and permissions")
