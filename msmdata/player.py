#
# player.py
# fuck you riot
#

from sfs2x.core import SFSObject, SFSArray
from .island import Island

import time

from tools.database import db_player, cur_player #type: ignore


MAX_DISPLAY_RESOURCE = 999_999_999
MAX_DIAMOND_RESOURCE = 9999
MAX_LEVEL_RESOURCE = 100
MAX_RESOURCE = 10_000_000_000
DISPLAY_INF_VALUE = MAX_DISPLAY_RESOURCE


def clamp_display(value):
    # Values at or above the display cap are shown as the max display sentinel.
    return min(value, DISPLAY_INF_VALUE)


class Player:
    def __init__(self, bbb_id: int, display_name: str, user_id: int):
        self.bbb_id = bbb_id
        self.user_id = user_id
        self.display_name = display_name
        self.scratch_off_purchased = False
        self.on_currency_change = None

        self.islands = []

        self.coins = 10000000
        self.diamonds = 1000000
        self.food = 50000000
        self.xp = 0
        self.level = 0
        self.shards = 10000000

        self.active_island = 1


    def add_island(self, island: Island):
        self.islands.append(island)


    def get_active_island(self):
        for island in self.islands:
            if island.user_island_id == self.active_island:
                return island
        return None


    def get_monster(self, user_monster_id: int):
        for island in self.islands:
            for monster in island.monsters:
                if monster.user_monster_id == user_monster_id:
                    return monster
        return None


    def refresh_monster(self, monster):
        for island in self.islands:
            if island.user_island_id == monster.user_island_id:
                for idx, existing in enumerate(island.monsters):
                    if existing.user_monster_id == monster.user_monster_id:
                        island.monsters[idx] = monster
                        return monster
                island.add_monster(monster)
                return monster
        return None


    def remove_monster(self, user_monster_id: int):
        for island in self.islands:
            for idx, monster in enumerate(island.monsters):
                if monster.user_monster_id == user_monster_id:
                    del island.monsters[idx]
                    return True
        return False


    def get_properties(self):
        properties = SFSArray()

        tmp = SFSObject()
        tmp.put_int("coins", clamp_display(self.coins))
        properties.add_sfs_object(tmp)

        tmp = SFSObject()
        tmp.put_int("diamonds", clamp_display(self.diamonds))
        properties.add_sfs_object(tmp)

        tmp = SFSObject()
        tmp.put_int("food", clamp_display(self.food))
        properties.add_sfs_object(tmp)

        tmp = SFSObject()
        tmp.put_int("xp", clamp_display(self.xp))
        properties.add_sfs_object(tmp)

        tmp = SFSObject()
        tmp.put_int("ethereal_currency", clamp_display(self.shards))
        properties.add_sfs_object(tmp)

        tmp = SFSObject()
        tmp.put_int("level", self.level)
        properties.add_sfs_object(tmp)

        return properties


    def _handle_level_up(self):
        if not self._levels:
            return

        while self.level < 100:
            next_level_data = self._levels.get(self.level + 1)
            if not next_level_data:
                break

            xp_needed = next_level_data.get("xp", 999999999)

            if self.xp >= xp_needed:
                # Level up
                self.level += 1
                self.xp = 0
            else:
                break


    def add_properties(self, coins=0, diamonds=0, food=0, xp=0, shards=0, level=0, set=False):
        coins = round(coins)
        diamonds = round(diamonds)
        food = round(food)
        xp = round(xp)
        shards = round(shards)

        # negative balances
        if self.diamonds + diamonds < 0:
            return False
        if self.coins + coins < 0:
            return False
        if self.food + food < 0:
            return False
        if self.xp + xp < 0:
            return False
        if self.shards + shards < 0:
            return False
        if self.level + level < 0:
            return False

        self.coins += coins
        self.diamonds += diamonds
        self.food += food
        self.xp += xp
        self.shards += shards
        self.level += level

        if set:
            self.coins = coins
            self.diamonds = diamonds
            self.food = food
            self.xp = 0
            self.shards = shards
            self.level = 99

        if xp > 0:
            self._handle_level_up()

        self.coins = min(self.coins, MAX_RESOURCE)
        self.diamonds = min(self.diamonds, MAX_RESOURCE)
        self.food = min(self.food, MAX_RESOURCE)
        self.xp = min(self.xp, MAX_RESOURCE)
        self.shards = min(self.shards, MAX_RESOURCE)
        self.level = min(self.level, MAX_RESOURCE)

        cur_player.execute(
            """UPDATE players 
               SET coins = ?, diamonds = ?, food = ?, xp = ?, level = ?, shards = ? 
               WHERE bbb_id = ?""",
            (self.coins, self.diamonds, self.food, self.xp, self.level, self.shards, self.bbb_id)
        )
        db_player.commit()

        if callable(self.on_currency_change):
            try:
                self.on_currency_change(self)
            except Exception:
                pass

        return True


    def get_sfs_object(self):
        current_time_ms = int(time.time()) * 1000
        player_object = SFSObject()

        coins = clamp_display(self.coins)
        diamonds = clamp_display(self.diamonds)
        food = clamp_display(self.food)
        xp = clamp_display(self.xp)
        shards = clamp_display(self.shards)

        self.coins = round(self.coins)
        self.diamonds = round(self.diamonds)
        self.food = round(self.food)
        self.xp = round(self.xp)
        self.shards = round(self.shards)

        player_object.put_int("coins", coins)
        player_object.put_int("diamonds", diamonds)
        player_object.put_int("food", food)
        player_object.put_int("ethereal_currency", shards)

        player_object.put_int("premium", 1)

        player_object.put_long("last_login", current_time_ms)

        player_object.put_int("xp", xp)
        player_object.put_int("level", self.level)
        player_object.put_int("max_level", 100)

        # use put_int for IDs to match SFSObject API in this environment
        player_object.put_int("bbb_id", int(self.bbb_id))
        player_object.put_int("user_id", int(self.user_id))
        player_object.put_int("referral", 0)
        player_object.put_long("active_island", self.active_island)

        player_object.put_int("fb_invite_reward", 1)
        player_object.put_int("twitter_invite_reward", 1)
        player_object.put_int("email_invite_reward", 1)
        player_object.put_long("last_fb_post_reward", current_time_ms)

        player_object.put_sfs_array("achievements", SFSArray())

        player_object.put_sfs_array("viewable_ads", SFSArray())
        player_object.put_utf_string("extra_ad_params", "")

        player_object.put_bool("third_party_ads", False)
        player_object.put_bool("third_party_video_ads", False)

        player_object.put_utf_string("display_name", self.display_name)

        islands = SFSArray()

        for island in self.islands:
            islands.add_sfs_object(island.get_sfs_object())

        player_object.put_sfs_array("islands", islands)

        #player_object.put_int("daily_bonus_diamonds", 0)
        #player_object.put_int("daily_bonus_coins", 200)
        #player_object.put_int("reward_day", 1)


        return player_object

