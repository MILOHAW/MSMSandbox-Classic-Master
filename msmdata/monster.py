#
# monster.py
# fuck you riot
#

import time
from sfs2x.core import SFSObject

from .megadata import AUTO_BIGGIFY_MONSTER_IDS, MegaData # type: ignore


MONSTER_POSITION_CACHE = {}


def record_monster_position(user_island_id: int, user_monster_id: int, x: int, y: int):
    key = (int(user_island_id), int(user_monster_id))
    current_position = (int(x), int(y))
    state = MONSTER_POSITION_CACHE.get(key)

    if state is None:
        state = {
            "current": current_position,
            "previous": current_position,
        }
    elif state["current"] != current_position:
        state["previous"] = state["current"]
        state["current"] = current_position

    MONSTER_POSITION_CACHE[key] = state
    return state


def get_monster_position_state(user_island_id: int, user_monster_id: int):
    return MONSTER_POSITION_CACHE.get((int(user_island_id), int(user_monster_id)))


class Monster:
    def __init__(
        self,
        user_island_id: int,
        user_monster_id: int,
        monster_id: int,
        x: int = 1,
        y: int = 1,
        flip: int = 0,
        level: int = 1,
        happiness: int = 50,
        collected_coins: int = 0,
        times_fed: int = 0,
        volume: float = 1.0,
        date_created: int = None,
        last_collection: int = None,
        muted: int = 0,
        mega_data: dict = None,
        parent_island_id: int = None,
        parent_monster_id: int = None,
        limited: bool = True,
        name: str = "Monster"
    ):
        self.user_island_id = user_island_id
        self.user_monster_id = user_monster_id
        self.monster_id = monster_id
        self.x = int(x) if x is not None and x != "" else 1
        self.y = int(y) if y is not None and y != "" else 1
        self.flip = flip
        self.level = level
        self.happiness = happiness
        self.collected_coins = collected_coins
        self.times_fed = times_fed
        self.volume = 1.0 if volume is None or volume == 0 else volume
        self.date_created = date_created or int(time.time() * 1000)
        self.last_collection = last_collection or int(time.time() * 1000)
        self.muted = muted
        self.limited = bool(limited)
        self.name = name or "Monster"

        self.mega_data = mega_data or None

        if self.mega_data is None and self.monster_id in AUTO_BIGGIFY_MONSTER_IDS:
            self.mega_data = MegaData(self.user_monster_id, False, True)

        self.parent_island_id = parent_island_id or None
        self.parent_monster_id = parent_monster_id or None

        record_monster_position(self.user_island_id, self.user_monster_id, self.x, self.y)


    def get_sfs_object(self):
        monster_obj = SFSObject()

        monster_obj.put_long("user_monster_id", self.user_monster_id)
        monster_obj.put_long("user_island_id", self.user_island_id)
        monster_obj.put_long("island", self.user_island_id)

        monster_obj.put_int("monster", self.monster_id)

        monster_obj.put_int("pos_x", self.x)
        monster_obj.put_int("pos_y", self.y)
        monster_obj.put_int("flip", self.flip)

        monster_obj.put_int("level", self.level)
        monster_obj.put_int("happiness", self.happiness)

        monster_obj.put_int("collected_coins", self.collected_coins)
        monster_obj.put_int("collected_ethereal", 0)
        monster_obj.put_int("collected_diamonds", 0)
        monster_obj.put_int("collected_food", 0)

        monster_obj.put_int("times_fed", self.times_fed)

        monster_obj.put_int("volume", int(self.volume if self.volume else 1))
        monster_obj.put_int("muted", self.muted)
        monster_obj.put_int("in_hotel", 0)
        monster_obj.put_bool("limited", self.limited)

        monster_obj.put_long("last_feeding", self.date_created)
        monster_obj.put_long("date_created", self.date_created)
        monster_obj.put_long("last_collection", self.last_collection)

        monster_obj.put_utf_string("name", self.name)

        if self.mega_data:
            monster_obj.put_sfs_object("megamonster", self.mega_data.get_sfs_object())

        if self.parent_island_id:
            monster_obj.put_long("parent_island", self.parent_island_id)

        if self.parent_monster_id:
            monster_obj.put_long("parent_monster", self.parent_monster_id)

        return monster_obj

