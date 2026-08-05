import time
import os
import sqlite3
from sfs2x.core import SFSObject


script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
static_db_path = os.path.join(script_dir, "static_dbs.db")

AUTO_BIGGIFY_MONSTER_IDS = set()

try:
    conn = sqlite3.connect(static_db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT m.monster_id
        FROM monsters m
        JOIN entities e ON e.entity_id = m.entity
        WHERE e.view_in_market = 1 AND m.levelup_island = 'ethereal'
        """
    )
    AUTO_BIGGIFY_MONSTER_IDS = {row["monster_id"] for row in cur.fetchall()}
    conn.close()
except Exception:
    AUTO_BIGGIFY_MONSTER_IDS = set()

class MegaData:
    def __init__(self, user_monster_id: int, permamega: bool = True, currently_mega: bool = False, started_at: int = None, finishes_at: int = None):
        self.user_monster_id = user_monster_id
        self.permamega = permamega
        self.currently_mega = currently_mega
        self.started_at = started_at
        self.finishes_at = finishes_at
    def get_sfs_object(self) -> SFSObject:
        mega_data_obj = SFSObject()

        #mega_data_obj.put_long("user_monster_id", self.user_monster_id)
        mega_data_obj.put_bool("permamega", self.permamega)
        mega_data_obj.put_bool("currently_mega", self.currently_mega)

        if not self.permamega and self.started_at is not None and self.finishes_at is not None:
            mega_data_obj.put_long("started_at", self.started_at)
            mega_data_obj.put_long("finished_at", self.finishes_at)

        return mega_data_obj