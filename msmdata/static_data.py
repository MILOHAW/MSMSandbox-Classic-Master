from sfs2x.core import SFSObject, SFSArray
import json
import time
from msmdata.get_data import cur_store, cur, skip_monster_ids

PERMANENT_MARKET_MONSTER_BIN_IDS = {
    32: "S01", 33: "CR", 34: "V", 49: "W", 52: "X",
    50: "G", 51: "K", 54: "J", 55: "KM", 56: "M",
    57: "L", 58: "LM", 59: "GM", 75: "G", 76: "GM",
    77: "L", 78: "LM", 82: "001_E_rare.bin",
}

ETHEREAL_MARKET_MONSTER_BIN_IDS = {
    50: None, 51: None, 54: None, 58: None, 76: None,
}


def _build_market_item(row, current_time_ms: int) -> SFSObject:
    item = SFSObject()

    item.put_int("id", int(row["storeitem_id"]))
    item.put_int("item_id", int(row["storeitem_id"]))
    item.put_utf_string("item_name", row["item_name"])
    item.put_utf_string("item_title", row["item_title"])
    item.put_utf_string("item_desc", row["item_desc"])
    item.put_int("price", int(row["price"]))
    item.put_int("consumable", int(row["consumable"]))
    item.put_int("amount", int(row["amount"]))
    
    # FIX: Keep only the database 'max' limit, removed the hardcoded -1 overwrite
    item.put_int("max", int(row["max"]))
    
    item.put_int("group_id", int(row["group_id"]))
    item.put_int("sale_amount", 0)
    item.put_int("currency_id", int(row["currency_id"]))
    item.put_utf_string("sheet_id", row["sheet_id"])
    item.put_utf_string("image_id", row["image_id"])
    item.put_utf_string("ios_platform_id", row["ios_platform_id"])
    item.put_utf_string("android_platform_id", row["android_platform_id"])
    item.put_utf_string("amazon_platform_id", "")
    item.put_long("last_changed", int(current_time_ms))
    item.put_int("enabled", int(row["enabled"]))
    item.put_utf_string("min_server_version", row["min_server_version"])
    
    # FIX: Standard items need bin_id to show up in the correct UI tab
    if "bins_id" in row.keys() and row["bins_id"]:
        item.put_utf_string("bins_id", row["bins_id"])
        item.put_utf_string("bin_id", row["bins_id"])
        
    return item


def _add_permanent_market_item(
    store_items: SFSArray,
    monster_id: int,
    bins_id: str | None,
    current_time_ms: int,
    item_id_offset: int = 100000,
    max_limit: int = -1,
) -> None:
    cur.execute(
        """
        SELECT m.monster_id, m.entity, e.name, e.cost_coins, e.cost_diamonds
        FROM monsters m
        JOIN entities e ON m.entity = e.entity_id
        WHERE m.monster_id = ?
        """,
        (monster_id,)
    )
    monster_row = cur.fetchone()
    if monster_row is None:
        return

    monster_store_id = item_id_offset + int(monster_row["monster_id"])

    # Prevent duplicates
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
    item.put_utf_string("item_desc", "")
    
    # FIX: Handle Diamond costs properly instead of forcing 0 Coins
    cost_coins = int(monster_row["cost_coins"] or 0)
    cost_diamonds = int(monster_row["cost_diamonds"] or 0)
    
    if cost_coins > 0:
        item.put_int("price", cost_coins)
        item.put_int("currency_id", 1)  # Coins
    elif cost_diamonds > 0:
        item.put_int("price", cost_diamonds)
        item.put_int("currency_id", 2)  # Diamonds
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
    
    # Only pin monsters to a specific tab when requested.
    if bins_id:
        item.put_utf_string("bins_id", bins_id)
        item.put_utf_string("bin_id", bins_id)
    item.put_int("enabled", 1)
    item.put_utf_string("min_server_version", "0.0")

    store_items.add_sfs_object(item)


def get_store_items():
    store_items = SFSArray()

    cur_store.execute("SELECT * FROM store_items")
    rows = cur_store.fetchall()

    current_time_ms = int(time.time()) * 1000

    for row in rows:
        if row["currency"] not in ["coins", "diamonds", "food"]:
            continue

        if "warm" in row["item_title"].lower():
            continue

        if row["min_server_version"] != "0.0":
            continue

        store_items.add_sfs_object(_build_market_item(row, current_time_ms))

    added = 0
    for monster_id, bins_id in PERMANENT_MARKET_MONSTER_BIN_IDS.items():
        before_count = len(store_items.value)
        _add_permanent_market_item(store_items, monster_id, bins_id, current_time_ms)
        if len(store_items.value) > before_count:
            added += 1

    for monster_id, bins_id in ETHEREAL_MARKET_MONSTER_BIN_IDS.items():
        before_count = len(store_items.value)
        _add_permanent_market_item(
            store_items,
            monster_id,
            bins_id,
            current_time_ms,
            item_id_offset=200000,
            max_limit=1,
        )
        if len(store_items.value) > before_count:
            added += 1

    if added:
        print(f"Added {added} permanent monster-based store items to stock")

    print(f"Loaded {len(store_items.value)} store items")

    return store_items


def get_goals():
    goals = SFSArray()

    cur.execute("SELECT goals FROM quests")
    rows = cur.fetchall()

    for row in rows:
        if not row["goals"] or not row["goals"].strip():
            continue

        try:
            goals_list = json.loads(row["goals"])
        except Exception:
            continue

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
            goals.add_sfs_object(goal_obj)

    print(f"Loaded {len(goals.value)} goals")
    return goals