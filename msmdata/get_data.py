from sfs2x.core import SFSObject, SFSArray
import time
import sqlite3
import json
import random
import os
from datetime import datetime

from tools.database import cur_player

# Use absolute path to ensure database is found regardless of working directory
script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
static_db_path = os.path.join(script_dir, "static_dbs.db")
store_db_path = os.path.join(script_dir, "store_data.db")

conn = sqlite3.connect(static_db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

conn_store = sqlite3.connect(store_db_path)
conn_store.row_factory = sqlite3.Row
cur_store = conn_store.cursor()

skip_monster_ids = [30, 79, 80]
skip_structure_ids = [232,233,234,235,236]

MONSTER_BIN_IDS = {
    32: "S01",
    33: "CR",
    34: "V",
    49: "W",
    52: "X",
    50: "L",
    55: "G",
    56: "M",
    57: "KM",
    59: "GM",
    75: "G",
    76: "M",
    77: "L",
    78: "LM",
    82: "001_E_rare.bin",
}

PERMANENT_MARKET_MONSTER_BIN_IDS = {
    82: MONSTER_BIN_IDS[82],
}

ETHEREAL_BIN_IDS = {
    50: "G",
    54: "J",
    56: "M",
    57: "L",
    58: "LM",
    76: "GM",
}

bbs_urls = {
    "placeholder": "https://127.0.0.1:9933",
    "placeholder": "https://127.0.0.1:9933",
    "placeholder": "https://127.0.0.1:9933",
    "placeholder": "https://127.0.0.1:9933",
    "placeholder": "https://127.0.0.1:9933",
    "placeholder": "https://127.0.0.1:9933"
}

def add_entity_data(sfs_obj: SFSObject, entity_id: int, is_extra=False, extra_entity_data=None):
    if is_extra:
        entity = extra_entity_data
    else:
        cur.execute("SELECT * FROM entities WHERE entity_id = ?", (entity_id,))
        row = cur.fetchone()
        entity = dict(row) if row else None

    if not entity:
        return

    # basic fields
    sfs_obj.put_int("entity_id", int(entity["entity_id"]))
    sfs_obj.put_utf_string("name", entity["name"] if entity["name"] is not None else "")
    sfs_obj.put_utf_string("description", entity["description"] if entity["description"] is not None else "")
    sfs_obj.put_utf_string("entity_type", entity["entity_type"] if entity["entity_type"] is not None else "")

    # graphic JSON
    if entity.get("graphic"):
        try:
            graphic_data = json.loads(entity["graphic"])
            graphic = SFSObject()
            for key, value in graphic_data.items():
                if isinstance(value, bool):
                    graphic.put_bool(key, value)
                elif isinstance(value, int):
                    graphic.put_int(key, value)
                elif isinstance(value, float):
                    graphic.put_double(key, value)
                else:
                    graphic.put_utf_string(key, str(value))
            sfs_obj.put_sfs_object("graphic", graphic)
        except Exception:
            pass

    # size / level / costs / timings
    sfs_obj.put_int("size_x", int(entity["size_x"]))
    sfs_obj.put_int("size_y", int(entity["size_y"]))
    sfs_obj.put_int("level", int(entity["level"]))
    sfs_obj.put_int("buildTime", int(entity["build_time"] * 1000))
    sfs_obj.put_int("build_time", int(entity["build_time"] * 1000))
    sfs_obj.put_int("cost_coins", int(entity["cost_coins"]))
    sfs_obj.put_int("cost_eth_currency", int(entity["cost_eth_currency"]))
    sfs_obj.put_int("cost_diamonds", int(entity["cost_diamonds"]))
    sfs_obj.put_int("cost_sale", int(entity["cost_sale"]))

    sfs_obj.put_utf_string("keywords", entity["keywords"] or "")
    sfs_obj.put_utf_string("min_server_version", entity["min_server_version"] or "0.0")

    # flags
    sfs_obj.put_bool("movable", entity["movable"])
    sfs_obj.put_bool("view_in_market", entity["view_in_market"])
    sfs_obj.put_bool("premium", entity["premium"])

    # requirements
    reqs = entity.get("requirements") or []
    if isinstance(reqs, str):
        reqs = json.loads(reqs)

    req_arr = SFSArray()
    for req in reqs:
        req_obj = SFSObject()
        if isinstance(req, dict):
            req_obj.put_int("entity", int(req.get("entity", 0)))
        else:
            req_obj.put_int("entity", int(req))
        req_arr.add_sfs_object(req_obj)
    sfs_obj.put_sfs_array("requirements", req_arr)

    # last changed / unknown / offset
    sfs_obj.put_long("last_changed", int(time.time()) * 1000)
    sfs_obj.put_int("xp", int(entity.get("xp", 0)))
    sfs_obj.put_int("y_offset", int(entity["y_offset"]))
    sfs_obj.put_int("sticker_offset", int(entity["y_offset"]))
    sfs_obj.put_utf_string("fb_object_id", "")
    sfs_obj.put_int("tier", 1)


def _add_permanent_structure_market_item(
    store_items: SFSArray,
    structure_row,
    current_time_ms: int,
    item_id_offset: int = 300000,
    max_limit: int = 1,
) -> None:
    structure_store_id = item_id_offset + int(structure_row["structure_id"])

    for existing in store_items.value:
        try:
            if existing.get("item_id") == structure_store_id:
                return
        except Exception:
            pass

    cur.execute("SELECT * FROM entities WHERE entity_id = ?", (structure_row["entity"],))
    entity_row = cur.fetchone()
    if entity_row is None:
        return

    item = SFSObject()
    item.put_int("id", structure_store_id)
    item.put_int("item_id", structure_store_id)
    item.put_utf_string("item_name", entity_row["name"] or f"STRUCTURE_{structure_row['structure_id']}")
    item.put_utf_string("item_title", entity_row["name"] or "")
    item.put_utf_string("item_desc", entity_row["description"] or "")

    cost_coins = int(entity_row["cost_coins"] or 0)
    cost_diamonds = int(entity_row["cost_diamonds"] or 0)
    cost_eth_currency = int(entity_row["cost_eth_currency"] or 0)

    if cost_coins > 0:
        item.put_int("price", cost_coins)
        item.put_utf_string("currency", "coins")
    elif cost_diamonds > 0:
        item.put_int("price", cost_diamonds)
        item.put_utf_string("currency", "diamonds")
    elif cost_eth_currency > 0:
        item.put_int("price", cost_eth_currency)
        item.put_utf_string("currency", "ethereal")
    else:
        item.put_int("price", 0)
        item.put_utf_string("currency", "coins")

    item.put_int("consumable", 0)
    item.put_int("amount", 1)
    item.put_int("max", int(max_limit))
    item.put_int("group_id", 1)
    item.put_int("sale_amount", 0)
    item.put_int("currency_id", 1)
    item.put_utf_string("sheet_id", "")
    item.put_utf_string("image_id", "")
    item.put_utf_string("ios_platform_id", "")
    item.put_utf_string("android_platform_id", "")
    item.put_utf_string("amazon_platform_id", "")
    item.put_long("last_changed", int(current_time_ms))
    item.put_int("enabled", 1)
    item.put_utf_string("min_server_version", entity_row["min_server_version"] or "0.0")

    add_entity_data(item, structure_row["entity"])
    item.put_utf_string("structure_type", structure_row["structure_type"])
    item.put_int("upgrades_to", int(structure_row["upgrades_to"] or 0))

    store_items.add_sfs_object(item)


def _add_permanent_monster_market_item(
    store_items: SFSArray,
    monster_id: int,
    bins_id: str | None,
    current_time_ms: int,
    item_id_offset: int = 100000,
    max_limit: int = -1,
) -> None:
    cur.execute(
        """
        SELECT m.monster_id, m.entity, e.name, e.description, e.cost_coins, e.cost_diamonds, e.min_server_version
        FROM monsters m
        JOIN entities e ON m.entity = e.entity_id
        WHERE m.monster_id = ?
        """,
        (monster_id,),
    )
    monster_row = cur.fetchone()
    if monster_row is None:
        return

    monster_store_id = item_id_offset + int(monster_row["monster_id"])

    for existing in store_items.value:
        try:
            if existing.get("item_id") == monster_store_id:
                return
        except Exception:
            pass

    item = SFSObject()
    item.put_int("id", monster_store_id)
    item.put_int("item_id", monster_store_id)
    item.put_utf_string("item_name", monster_row["name"] or f"Monster_{monster_row['monster_id']}")
    item.put_utf_string("item_title", monster_row["name"] or "")
    item.put_utf_string("item_desc", monster_row["description"] or "")

    cost_coins = int(monster_row["cost_coins"] or 0)
    cost_diamonds = int(monster_row["cost_diamonds"] or 0)

    if cost_coins > 0:
        item.put_int("price", cost_coins)
        item.put_int("currency_id", 1)
    elif cost_diamonds > 0:
        item.put_int("price", cost_diamonds)
        item.put_int("currency_id", 2)
    else:
        item.put_int("price", 0)
        item.put_int("currency_id", 1)

    item.put_int("consumable", 0)
    item.put_int("amount", 1)
    item.put_int("max", int(max_limit))
    item.put_int("group_id", 1)
    item.put_int("sale_amount", 0)
    item.put_utf_string("sheet_id", "")
    item.put_utf_string("image_id", "")
    item.put_utf_string("ios_platform_id", "")
    item.put_utf_string("android_platform_id", "")
    item.put_utf_string("amazon_platform_id", "")
    item.put_long("last_changed", int(current_time_ms))
    item.put_int("enabled", 1)
    item.put_utf_string("min_server_version", monster_row["min_server_version"] or "0.0")

    if bins_id:
        item.put_utf_string("bins_id", bins_id)
        item.put_utf_string("bin_id", bins_id)

    add_entity_data(item, monster_row["entity"])
    store_items.add_sfs_object(item)

def get_genes():
    genes = SFSArray()
    cur.execute("SELECT * FROM genes")
    rows = cur.fetchall()
    current_time_ms = int(time.time()) * 1000

    for row in rows:
        gene = SFSObject()
        gene.put_utf_string("gene_letter", row["gene_letter"])
        gene.put_utf_string("gene_graphic", row["gene_graphic"])
        gene.put_utf_string("min_server_version", row["min_server_version"])
        gene.put_int("gene_id", row["gene_id"])
        gene.put_long("last_changed", current_time_ms)
        genes.add_sfs_object(gene)

    print(f"Loaded {len(genes.value)} genes")
    return genes

def _load_player_quest_progress(bbb_id: int | None) -> dict[int, list[sqlite3.Row]]:
    if bbb_id is None:
        return {}

    try:
        cur_player.execute(
            """
            SELECT quest_id, goal_index, status, collected
            FROM player_quest_progress
            WHERE bbb_id = ?
            """,
            (bbb_id,),
        )
    except sqlite3.OperationalError:
        return {}

    progress_by_quest: dict[int, list[sqlite3.Row]] = {}
    for row in cur_player.fetchall():
        quest_id = int(row["quest_id"])
        progress_by_quest.setdefault(quest_id, []).append(row)

    return progress_by_quest


def get_quests(bbb_id: int | None = None):
    quests = SFSArray()
    progress_by_quest = _load_player_quest_progress(bbb_id)
    cur.execute("SELECT * FROM quests")
    rows = cur.fetchall()

    for row in rows:
        quest_wrapper = SFSObject()
        quest_progress = progress_by_quest.get(int(row["id"]), [])
        log = SFSObject()
        log.put_int("id", row["id"])
        log.put_int("quest_id", row["id"])
        log.put_int("user", int(bbb_id or 0))
        completed = bool(quest_progress) and all(int(progress_row["status"] or 0) > 0 for progress_row in quest_progress)
        collected = sum(int(progress_row["collected"] or 0) for progress_row in quest_progress)
        log.put_utf_string("status", "true" if completed else "false")
        log.put_int("collected", collected)
        log.put_int("new", row["initial"])

        static_data = SFSObject()
        static_data.put_int("id", row["id"])
        static_data.put_utf_string("name", row["name"])
        static_data.put_utf_string("description", row["description"])
        static_data.put_utf_string("type", row["type"])

        goals_array = SFSArray()
        if row["goals"] and row["goals"].strip():
            try:
                goals_list = json.loads(row["goals"])
                for goal_dict in goals_list:
                    goal_obj = SFSObject()
                    for key, value in goal_dict.items():
                        if isinstance(value, int):
                            goal_obj.put_int(key, value)
                        elif isinstance(value, str):
                            goal_obj.put_utf_string(key, value)
                        elif isinstance(value, list):
                            sub_array = SFSArray()
                            for item in value:
                                if isinstance(item, int):
                                    sub_array.add_int(item)
                                else:
                                    sub_array.add_utf_string(str(item))
                            goal_obj.put_sfs_array(key, sub_array)
                    goals_array.add_sfs_object(goal_obj)
            except:
                pass 
        static_data.put_sfs_array("goals", goals_array)

        next_array = SFSArray()
        if row["next"] and row["next"].strip():
            try:
                next_list = json.loads(row["next"])
                for item in next_list:
                    if isinstance(item, int):
                        next_array.add_int(item)
                    else:
                        next_array.add_utf_string(str(item))
            except:
                pass
        static_data.put_sfs_array("next", next_array)

        rewards_obj = SFSObject()
        if row["rewards"] and row["rewards"].strip():
            try:
                rewards_dict = json.loads(row["rewards"])
                for key, value in rewards_dict.items():
                    if isinstance(value, int):
                        rewards_obj.put_int(key, value)
                    elif isinstance(value, str):
                        rewards_obj.put_utf_string(key, value)
            except:
                pass
        static_data.put_sfs_object("rewards", rewards_obj)

        static_data.put_utf_string("sheet", row["sheet"])
        static_data.put_utf_string("image", row["image"])
        static_data.put_int("visible", row["visible"])
        static_data.put_utf_string("min_server_version", row["min_server_version"])

        if row["comment"]:
            static_data.put_utf_string("comment", row["comment"])

        new_array = SFSArray()
        new_array.add_sfs_object(log)
        new_array.add_sfs_object(static_data)

        quest_wrapper.put_sfs_array("new", new_array)
        quests.add_sfs_object(quest_wrapper)

    print(f"Loaded {len(quests.value)} quests")
    return quests

def get_islands():
    islands = SFSArray()
    cur.execute("SELECT * FROM islands")
    rows = cur.fetchall()
    current_time_ms = int(time.time()) * 1000

    for row in rows:
        island = SFSObject()
        island_id = row["island_id"]

        island.put_int("id", island_id)
        island.put_int("island_id", island_id)
        island.put_int("island_type", island_id)
        island.put_utf_string("name", row["name"])
        island.put_utf_string("description", row["description"])
        island.put_utf_string("genes", row["genes"])
        island.put_utf_string("midi", row["midi"])
        island.put_utf_string("min_server_version", row["min_server_version"])
        island.put_long("last_changed", current_time_ms)
        island.put_utf_string("fb_object_id", "")
        island.put_int("enabled", 1)

        island.put_int("level", row["level"])
        island.put_int("cost_coins", row["cost_coins"])
        island.put_int("cost_diamonds", row["cost_diamonds"])
        island.put_int("castle_structure_id", row["castle_structure_id"])

        title, url = random.choice(list(bbs_urls.items()))
        island.put_utf_string("remix_url", url)
        island.put_utf_string("remix_url_2", url)

        graphic_data = json.loads(row["graphic"])
        graphic = SFSObject()
        graphic.put_utf_string("file", graphic_data["file"])
        graphic.put_utf_string("tileset", graphic_data["tileset"])
        graphic.put_utf_string("grid", "main_grid.bin")
        graphic.put_utf_string("bg", graphic_data["bg"])
        island.put_sfs_object("graphic", graphic)

        island.put_utf_string("grid", "main_grid.bin")

        monsters = SFSArray()
        cur.execute("SELECT * FROM island_monsters WHERE island = ?", (island_id,))
        monster_rows = cur.fetchall()
        for m in monster_rows:
            if m["monster"] in skip_monster_ids:
                continue
            mo = SFSObject()
            mo.put_int("monster", m["monster"])
            mo.put_utf_string("instrument", m["instrument"])
            monsters.add_sfs_object(mo)
        island.put_sfs_array("monsters", monsters)

        structures = SFSArray()
        cur.execute("SELECT * FROM island_structures WHERE island = ?", (island_id,))
        structure_rows = cur.fetchall()
        for s in structure_rows:
            so = SFSObject()
            so.put_int("structure", s["structure"])
            so.put_utf_string("instrument", s["instrument"])
            structures.add_sfs_object(so)
        island.put_sfs_array("structures", structures)

        islands.add_sfs_object(island)

    print(f"Loaded {len(islands.value)} islands")
    return islands

def get_structures():
    structures = SFSArray()
    cur.execute("SELECT * FROM structures")
    rows = cur.fetchall()
    current_time_ms = int(time.time()) * 1000

    for row in rows:
        if row["structure_id"] in skip_structure_ids:
            continue

        structure = SFSObject()
        structure.put_int("structure_id", row["structure_id"])
        structure.put_int("id", row["structure_id"])
        structure.put_int("entity_id", row["entity"])
        structure.put_utf_string("structure_type", row["structure_type"])
        structure.put_int("upgrades_to", row["upgrades_to"])
        structure.put_utf_string("sound", row["sound"])
        structure.put_long("last_changed", current_time_ms)
        structure.put_int("limit_to_island", row["limit_to_island"])

        extra = SFSObject()
        if row["extra"]:
            extra_data = json.loads(row["extra"])
            for k, v in extra_data.items(): #commented out lines are for infinite beds
                #if k == "beds":
                   # v = 999
                if isinstance(v, int):
                    extra.put_int(k, v)
                elif isinstance(v, float):
                    extra.put_double(k, v)
                else:
                    extra.put_utf_string(k, str(v))

        structure.put_sfs_object("extra", extra)
        add_entity_data(structure, row["entity"])
        structures.add_sfs_object(structure)

    print(f"Loaded {len(structures.value)} structures")
    return structures

extra_monsters = []

def get_monsters():
    monsters = SFSArray()
    cur.execute("SELECT * FROM monsters")
    rows = cur.fetchall()
    db_rows = [dict(zip([col[0] for col in cur.description], r)) for r in rows]
    all_rows = db_rows + extra_monsters
    current_time_ms = int(time.time()) * 1000

    for row in all_rows:
        if row["monster_id"] in skip_monster_ids:
            continue

        monster = SFSObject()
        is_extra = row.get("is_extra", False)

        monster.put_int("monster_id", row["monster_id"])
        monster.put_int("id", row["monster_id"])
        monster.put_int("entity_id", row["entity"])
        monster.put_utf_string("genes", row["genes"])
        monster.put_utf_string("common_name", "Monster")
        monster.put_utf_string("spore_graphic", f"spore_{row['genes']}")
        monster.put_bool("limited", True)
        monster.put_long("last_changed", current_time_ms)
        monster.put_int("beds", row["beds"])
        monster.put_int("hide_friends", 0)

        happiness_data = row["happiness"] if is_extra else (json.loads(row["happiness"]) if row["happiness"] else [])
        happiness_array = SFSArray()
        for h in happiness_data:
            h_obj = SFSObject()
            h_obj.put_int("entity", h["entity"])
            h_obj.put_int("value", h["value"])
            happiness_array.add_sfs_object(h_obj)

        monster.put_sfs_array("happiness", happiness_array)
        monster.put_sfs_array("likes", happiness_array)
        monster.put_sfs_array("dislikes", SFSArray())

        names_data = row["names"] if is_extra else (json.loads(row["names"]) if row["names"] else [])
        names_array = SFSArray()
        for name in names_data:
            names_array.add_utf_string(name)

        monster.put_sfs_array("names", names_array)
        monster.put_int("level_up_xp", row["level_up_xp"])
        
        # Ensure safely converted string for ethereal check
        levelup_island = row.get("levelup_island", "")
        monster.put_utf_string("levelup_island", levelup_island)
        if levelup_island and levelup_island.lower() == "ethereal":
            bins_id = ETHEREAL_BIN_IDS.get(row["monster_id"])
        else:
            bins_id = MONSTER_BIN_IDS.get(row["monster_id"])
            
        if bins_id:
            monster.put_utf_string("bins_id", bins_id)
            monster.put_utf_string("bin_id", bins_id)

        title, url = random.choice(list(bbs_urls.items()))
        monster.put_utf_string("link_title", title)
        monster.put_utf_string("link_address", url)
        add_entity_data(monster, row["entity"], is_extra=is_extra, extra_entity_data=row.get("entity_data"))

        if is_extra:
            lvl_data = row["levels"]
        else:
            cur.execute("SELECT * FROM monster_levels WHERE monster = ?", (row["monster_id"],))
            lvl_data = cur.fetchall()

        levels_array = SFSArray()
        for lvl in lvl_data:
            lo = SFSObject()
            lo.put_int("max_coins", lvl["max_coins"])
            lo.put_int("coins", lvl["coins"])
            lo.put_int("level", lvl["level"])
            lo.put_int("monster_level_id", lvl["monster_level_id"])
            lo.put_int("food", lvl["food"])
            lo.put_int("ethereal_currency", lvl["ethereal_currency"])
            lo.put_int("max_ethereal", lvl["max_ethereal"])
            levels_array.add_sfs_object(lo)

        monster.put_sfs_array("levels", levels_array)
        monsters.add_sfs_object(monster)

    print(f"Loaded {len(monsters.value)} monsters")
    return monsters

def get_levels():
    levels = SFSArray()
    cur.execute("SELECT * FROM level_xp")
    rows = cur.fetchall()

    for row in rows:
        level = SFSObject()
        level.put_int("level", row["level"])
        level.put_int("xp", row["xp"])
        level.put_int("max_bakeries", row["max_bakeries"])
        levels.add_sfs_object(level)

    print(f"Loaded {len(levels.value)} levels")
    return levels

def get_levels_dict():
    levels_dict = {}
    cur.execute("""
        SELECT level, xp, max_bakeries 
        FROM level_xp 
        ORDER BY level
    """)
    rows = cur.fetchall()

    for row in rows:
        level_id = row["level"]
        levels_dict[level_id] = {
            "level": row["level"],
            "xp": row["xp"],
            "max_bakeries": row["max_bakeries"]
        }
    return levels_dict

def get_scratchoffs():
    scratchoffs = SFSArray()
    cur.execute("SELECT * FROM scratch_offs")
    rows = cur.fetchall()

    for row in rows:
        scratch_offer = SFSObject()
        scratch_offer.put_int("id", row["id"])
        scratch_offer.put_int("scratch_id", row["id"])
        scratch_offer.put_utf_string("type", row["type"])
        scratch_offer.put_utf_string("prize", row["prize"])
        scratch_offer.put_int("amount", row["amount"])
        scratch_offer.put_int("probability", row["probability"])
        scratch_offer.put_int("is_top_prize", row["is_top_prize"])
        scratch_offer.put_utf_string("min_server_version", row["min_server_version"])
        scratchoffs.add_sfs_object(scratch_offer)
    
    print(f"Loaded {len(scratchoffs.value)} scratch-offs")
    return scratchoffs

def get_game_settings():
    cur.execute("SELECT setting, value FROM game_settings")
    rows = cur.fetchall()
    existing = {row["setting"]: row["value"] for row in rows}

    defaults = {
        "USER_SELLING_PERCENTAGE":                  "0.75",
        "USER_MAX_NUM_TORCHES_PER_ISLAND":          "10",
        "USER_DIAMOND_COST_PER_LIT_TORCH":          "2",
        "USER_DIAMOND_COST_PER_PERMALIT_TORCH":     "100",
        "USER_DIAMOND_COST_PER_DAILY_MEGAFY":       "50",
        "USER_DIAMOND_COST_PER_PERMALIT_MEGAMONSTER":"20",
        "USER_COIN_COST_PER_DAILY_MEGAMONSTER":     "25000",
        "USER_COIN_COST_PER_PERMALIT_MEGAMONSTER":  "250000",
        "USER_ETHEREAL_ISLAND_HATCH_XP_MODIFIER":   "0.027",
        "MEMORY_DIAMOND_PRICE":                     "2",
        "MEMORY_COIN_PRICE":                        "0",
        "USER_SCRATCHOFF_PRICE":                    "2",
        "USER_MONSTER_SCRATCHOFF_PRICE":            "10",
        "USER_MORE_GAMES_IOS":                      "playhaven",
        "USER_MORE_GAMES_ANDROID":                  "playhaven",
        "USER_MORE_GAMES_AMAZON":                   "chartboost",
        "USER_FB_ACHIEVEMENTS_URL":                 "http://www.bbbarcade.com/mysingingmonsters/msm_facebook/admin/post_achievement.php",
        "USER_FB_MONSTERS_URL":                     "http://www.bbbarcade.com/mysingingmonsters/msm_facebook/content/monsters/jpg/",
        "USER_FB_CUSTOM_EVENTS_URL":                "http://www.mysingingmonsters.com/facebook/actions/",
        "USER_FB_PLATFORM_REDIRECT_URL":            "http://www.bbbarcade.com/mysingingmonsters/msm_facebook/platform_redirect.php",
        "USER_FB_POST_REWARD_REFRESH":              "24",
        "USER_COIN_ETH_EXCHANGE_RATE":              "500000,50",
        "USER_DIAMOND_ETH_EXCHANGE_RATE":           "50,100",
        "USER_ETH_DIAMOND_EXCHANGE_RATE":           "30000,1",
        "USER_NEWS_DATA":                           "0",
    }

    game_settings = SFSArray()
    for setting_name, value in existing.items():
        obj = SFSObject()
        obj.put_utf_string("key", setting_name)
        obj.put_utf_string("value", str(value))
        game_settings.add_sfs_object(obj)

    for key, default_value in defaults.items():
        if key not in existing:
            obj = SFSObject()
            obj.put_utf_string("key", key)
            obj.put_utf_string("value", default_value)
            game_settings.add_sfs_object(obj)

    print(f"Loaded {len(game_settings.value)} game settings")
    return game_settings

def get_torch_data():
    torch_data = SFSArray()
    cur.execute("SELECT * FROM island_torches")
    rows = cur.fetchall()

    def parse_last_changed(value):
        if value is None:
            return int(time.time() * 1000)
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
        try:
            return int(datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
        except (TypeError, ValueError):
            return int(time.time() * 1000)

    for row in rows:
        torch = SFSObject()
        torch.put_int("island_id", row["island_id"])
        torch.put_utf_string("torch_graphic", row["torch_graphic"])
        torch.put_long("last_changed", parse_last_changed(row["last_changed"]))
        torch_data.add_sfs_object(torch)

    print(f"Loaded {len(torch_data.value)} torches")
    return torch_data

def get_timed_events():
    timed_events = SFSArray()
    cur.execute("SELECT * FROM entities")
    rows = cur.fetchall()
    current_time_ms = int(time.time()) * 1000
    end_date = current_time_ms + (((60 * 60) * 24) * 365) * 1000

    for row in rows:
        if row["view_in_market"] == 1:
            continue

        event = SFSObject()
        event.put_long("end_date", end_date)
        event.put_long("last_updated", current_time_ms)
        event.put_utf_string("event_type", "EntityStoreAvailability")
        event.put_int("event_id", 3)

        timed_event_data_array = SFSArray()
        timed_entity_data = SFSObject()
        timed_entity_data.put_int("entity", row["entity_id"])
        timed_event_data_array.add_sfs_object(timed_entity_data)

        event.put_sfs_array("data", timed_event_data_array)
        event.put_long("id", 200000 + row["entity_id"])
        event.put_long("start_date", current_time_ms)

        timed_events.add_sfs_object(event)

    print(f"Loaded {len(timed_events.value)} timed events")
    return timed_events

def get_store_groups():
    store_groups = SFSArray()
    cur.execute("SELECT * FROM store_groups")
    rows = cur.fetchall()
    current_time_ms = int(time.time()) * 1000

    for row in rows:
        group = SFSObject()
        group.put_int("id", row["storegroup_id"])
        group.put_int("storegroup_id", row["storegroup_id"])
        group.put_utf_string("name", row["group_name"])
        group.put_int("currency", row["currency"])
        group.put_utf_string("group_title", row["group_title"])
        group.put_long("last_changed", current_time_ms)
        group.put_utf_string("min_server_version", row["min_server_version"])
        store_groups.add_sfs_object(group)

    print(f"Loaded {len(store_groups.value)} store groups")
    return store_groups

def get_store_currencys():
    store_currencys = SFSArray()
    cur.execute("SELECT * FROM store_currency")
    rows = cur.fetchall()
    current_time_ms = int(time.time()) * 1000

    for row in rows:
        currency = SFSObject()
        currency.put_int("storecur_id", row["storecur_id"])
        currency.put_int("id", row["storecur_id"])
        currency.put_utf_string("currency_name", row["currency_name"])
        currency.put_int("starting_amount", row["starting_amount"])
        currency.put_long("last_changed", current_time_ms)
        currency.put_utf_string("min_server_version", row["min_server_version"])
        store_currencys.add_sfs_object(currency)

    print(f"Loaded {len(store_currencys.value)} store currencies")
    return store_currencys

def get_store_items():
    store_items = SFSArray()
    cur_store.execute("SELECT * FROM store_items")
    rows = cur_store.fetchall()

    for row in rows:
        # ---- FIX: Allow "ethereal" currency to stop filtering out Ethereal monsters from the store ----
        if row["currency"] not in ["coins", "diamonds", "food", "ethereal"]:
            continue

        if "warm" in row["item_title"].lower():
            continue

        if row["min_server_version"] != "0.0":
            continue

        item = SFSObject()
        item.put_int("id", int(row["storeitem_id"]))
        item.put_int("item_id", int(row["storeitem_id"]))
        item.put_utf_string("item_name", row["item_name"])
        item.put_utf_string("item_title", row["item_title"])
        item.put_utf_string("item_desc", row["item_desc"])
        item.put_int("price", int(row["price"]))
        item.put_int("consumable", int(row["consumable"]))
        item.put_int("amount", int(row["amount"]))
        
        # Avoid overriding the correct item 'max' with a static -1
        max_val = row["max"] if row["max"] is not None else -1
        item.put_int("max", int(max_val))
        item.put_int("group_id", int(row["group_id"]))
        item.put_int("sale_amount", 0)
        item.put_int("currency_id", int(row["currency_id"]))
        
        # ---- FIX: Completed the truncated code from your original script ----
        sheet_id = row["sheet_id"] if row.keys() and "sheet_id" in row.keys() and row["sheet_id"] else ""
        item.put_utf_string("sheet_id", sheet_id)
        
        store_items.add_sfs_object(item)

    current_time_ms = int(time.time()) * 1000

    cur.execute(
        """
        SELECT *
        FROM structures
        WHERE structure_type = 'castle'
        ORDER BY structure_id
        """
    )
    for structure_row in cur.fetchall():
        _add_permanent_structure_market_item(store_items, structure_row, current_time_ms, max_limit=1)

    cur.execute(
        """
        SELECT *
        FROM structures
        WHERE structure_id = 2
        LIMIT 1
        """
    )
    breeding_row = cur.fetchone()
    if breeding_row is not None:
        _add_permanent_structure_market_item(store_items, breeding_row, current_time_ms, max_limit=10)

    for monster_id, bins_id in PERMANENT_MARKET_MONSTER_BIN_IDS.items():
        _add_permanent_monster_market_item(store_items, monster_id, bins_id, current_time_ms, max_limit=1)

    print(f"Loaded {len(store_items.value)} store items")
    return store_items