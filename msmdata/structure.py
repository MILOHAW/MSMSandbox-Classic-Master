#
# structure.py
# fuck you riot
#

import time
from sfs2x.core import SFSObject, SFSArray
import json


def sfs_to_plain(value):
    if value.__class__.__name__ == "SFSArray":
        return [sfs_to_plain(v) for v in value.value]

    if value.__class__.__name__ == "SFSObject":
        return {k: sfs_to_plain(v) for k, v in value.value.items()}

    if hasattr(value, "value"):
        return sfs_to_plain(value.value)

    return value


def sfs_to_json(sfs_array):
    return json.dumps(sfs_to_plain(sfs_array), indent=2)


class Structure:
    def __init__(self, user_island_id: int, user_structure_id: int, structure_id: int, x: int, y: int, flip: int, scale: float, date_created: int,
                 building_completed=None, last_collection=None, obj_data=None, obj_end=None, muted=0):
        self.user_island_id = user_island_id
        self.user_structure_id = user_structure_id
        self.structure_id = structure_id
        self.x = x
        self.y = y
        self.flip = flip
        self.scale = scale
        self.date_created = date_created
        self.building_completed = building_completed
        self.last_collection = last_collection if last_collection is not None else date_created
        self.obj_data = obj_data
        self.obj_end = obj_end
        self.muted = muted

    def get_sfs_object(self):
        structure_obj = SFSObject()

        structure_obj.put_long("user_structure_id", self.user_structure_id)
        structure_obj.put_long("user_island_id", self.user_island_id)
        structure_obj.put_long("island", self.user_island_id)

        structure_obj.put_long("structure", self.structure_id)
        structure_obj.put_float("scale", self.scale)
        structure_obj.put_double("size", self.scale)

        structure_obj.put_int("pos_x", self.x)
        structure_obj.put_int("pos_y", self.y)
        structure_obj.put_int("flip", 1 if self.flip else 0)
        structure_obj.put_int("muted", 1 if getattr(self, "muted", 0) else 0)

        structure_obj.put_int("is_complete", 1)
        structure_obj.put_int("is_upgrading", 0)
        structure_obj.put_int("in_warehouse", 0)

        structure_obj.put_long("date_created", self.date_created)
        #structure_obj.put_long("building_completed", self.date_created)
        structure_obj.put_long("last_collection", self.last_collection if hasattr(self, "last_collection") else self.date_created)

        structure_obj.put_double("diamonds_collected", 0)

        inventory = SFSArray()
        item = SFSObject()
        item.put_int("m", 68)
        inventory.add_sfs_object(item)

        structure_obj.put_sfs_array("inv", inventory)
        structure_obj.put_utf_string("req", sfs_to_json(inventory))

        return structure_obj

