from sfs2x.protocol import Message, ControllerID
from sfs2x.core import SFSObject, SFSArray

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.decrepit.ciphers import modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding

import hashlib
import base64
import re
import json
import os

from tools.database import cur_player #type: ignore

def md5_sum(input_string):
    encoded_string = input_string.encode('utf-8')

    md5_hash = hashlib.md5(encoded_string)

    return md5_hash.hexdigest()

def debug_sfsobject(obj, path="root"):
    for key in obj.keys():
        value = obj.get(key)
        if value is None:
            print(f"❌ {path}.{key} is None!")
        elif isinstance(value, SFSObject):
            debug_sfsobject(value, path=f"{path}.{key}")
        elif isinstance(value, SFSArray):
            for i, item in enumerate(value):
                if isinstance(item, SFSObject):
                    debug_sfsobject(item, path=f"{path}.{key}[{i}]")
                elif item is None:
                    print(f"❌ {path}.{key}[{i}] is None!")

async def send_extension_response(client, cmd, params):
    if cmd in {"gs_promos", "gs_quest", "ga_quest"}:
        return

    ext_resp = SFSObject()
    ext_resp.put_utf_string("c", cmd)
    ext_resp.put_int("r", -1)
    ext_resp.put_sfs_object("p", params)

    debug_sfsobject(params)

    await client.send(Message(
        controller=ControllerID.EXTENSION,
        action=13,
        payload=ext_resp
    ))

def speedup_cost_diamonds(now_ms: int, end_ms: int) -> int:
    if now_ms >= end_ms:
        return 0
    return round((end_ms - now_ms) / 1_800_000) + 1

def player_exists(bbb_id):
    cur_player.execute("SELECT 1 FROM players WHERE bbb_id = ?", (bbb_id,))
    return cur_player.fetchone() is not None

def encrypt(message, initial_vector, secret_key):
    secret_key = secret_key.encode('utf-8')[:16]
    cipher = Cipher(algorithms.AES(secret_key), modes.CFB8(initial_vector.encode('utf-8')), backend=default_backend())
    padder = padding.PKCS7(128).padder()
    padded_message = padder.update(message.encode('utf-8')) + padder.finalize()
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded_message) + encryptor.finalize()
    return base64.b64encode(encrypted).decode('utf-8')

def decrypt(encrypted_message, initial_vector, secret_key):
    secret_key = secret_key.encode('utf-8')[:16]
    cipher = Cipher(algorithms.AES(secret_key), modes.CFB8(initial_vector.encode('utf-8')), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted_padded = decryptor.update(base64.b64decode(encrypted_message)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    decrypted = unpadder.update(decrypted_padded) + unpadder.finalize()
    return decrypted.decode('utf-8')

def _load_json_no_comments(path):
    with open(path, "r") as f:
        text = f.read()

    text = re.sub(r'//.*', '', text)
    return json.loads(text)

def get_config_value(key):
    # Use absolute path to ensure config.json is found regardless of working directory
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(script_dir, "config.json")
    config = _load_json_no_comments(config_path)
    return config.get(key)

def sanitize_name(name, alphabet):
    if not name:
        return ""
    sanitized = []
    for c in name:
        if c in alphabet:
            sanitized.append(c)
        elif c.isspace():
            sanitized.append(" ")
        else:
            sanitized.append("?")
    return "".join(sanitized)

# ----------------------
# Profanity filter
# ----------------------
LEET_MAP = str.maketrans({
    "@": "a",
    "4": "a",
    "3": "e",
    "1": "i",
    "!": "i",
    "0": "o",
    "$": "s",
    "5": "s",
    "7": "t"
})

SEPARATOR_REGEX = re.compile(r"[\s\.\-_]+")

def normalize_text(text: str) -> str:
    text = text.lower()
    text = text.translate(LEET_MAP)
    return text

def collapse_separators(text: str) -> str:
    return SEPARATOR_REGEX.sub("", text)

def collapse_repeats(text: str) -> str:
    return re.sub(r"(.)\1+", r"\1", text)

# Bad words list
BAD_WORDS = {
    "fuck","fucker","fucking","motherfucker","mf","shit","bullshit","bitch","bitches",
    "ass","asshole","dick","dildo","cock","cocksucker","pussy","pussies","slut",
    "whore","cum","cumming","jizz","jerkoff","handjob","blowjob","boob","boobs",
    "tits","tit","nipple","porn","porno","pornhub","sex","sexy","s3x","suck",
    "sucking","deepthroat","anal","anus","buttsex","butthole","balls","testicles",
    "scrotum","masturbate","masturbation","orgasm","orgy","fetish","bdsm",
    "bondage","spank","spanking","horny","hentai","rule34",
    "idiot","moron","dumbass","retard","retarded","stupid","loser","noob",
    "trash","garbage","clown","scumbag","dipshit","douche","douchebag",
    "jackass","prick","tool","twat","wanker","shithead","dirtbag",
    "kill","kys","die","suicide","murder","rapist","rape","raping",
    "terrorist","bomb","massacre","genocide",
    "cocaine","heroin","meth","weed","marijuana","crack","lsd","drugdealer",
    "nigger", "nigga", "fag", "faggot",
    "fuk","fuc","phuck","fucc","fuq","shiit","sh1t","b1tch","biatch",
    "azzhole","a55hole","d1ck","d!ck","c0ck","p0rn","s3xy", "f@g", "f@ggot", "n1gger", "n1gga",
    "nazi","hitler","kkk","satan","devil","racist","bigot",
}

def contains_bad_word(text: str) -> bool:
    if not text:
        return False
    text = normalize_text(text)
    collapsed = collapse_separators(text)
    collapsed = collapse_repeats(collapsed)
    for word in BAD_WORDS:
        if word in text or word in collapsed:
            return True
    return False

# ----------------------
# Name validation
# ----------------------
def invalid_name(name):
    if not name:
        return "INVALID_DISPLAY_NAME"
    if "%" in name:
        return "INVALID_CHAR_DISPLAY_NAME"
    elif "<c" in name:
        return "INVALID_CHAR_DISPLAY_NAME"
    elif "</" in name:
        return "INVALID_CHAR_DISPLAY_NAME"
    elif contains_bad_word(name):
        return "BAD_WORD_DISPLAY_NAME"
    elif re.match(r"^\s*$", name):
        return "INVALID_WHITESPACE_DISPLAY_NAME"
    return None