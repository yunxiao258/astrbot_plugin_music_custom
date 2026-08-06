"""网易云 weapi 加密参数构造（AES-CBC + RSA，用于需要登录态的接口）"""

import base64
import json
import random
import string

from Crypto.Cipher import AES
from Crypto.Util.number import bytes_to_long

PUB_KEY = "010001"
MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b72515"
    "2b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbd"
    "a92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cf"
    "e4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
NONCE = "0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"


def _aes_encrypt(text: str, key: str) -> str:
    key_b = key.encode()
    pad = 16 - len(text.encode()) % 16
    data = text.encode() + bytes([pad] * pad)
    cipher = AES.new(key_b, AES.MODE_CBC, IV)
    return base64.b64encode(cipher.encrypt(data)).decode()


def weapi_params(data: dict) -> dict:
    """构造网易云 weapi 请求参数 {params, encSecKey}"""
    sec_key = "".join(random.choices(string.digits + string.ascii_letters, k=16))
    text = json.dumps(data, ensure_ascii=False)
    params = _aes_encrypt(_aes_encrypt(text, NONCE), sec_key)
    enc = pow(bytes_to_long(sec_key.encode()), int(PUB_KEY, 16), int(MODULUS, 16))
    enc_sec_key = format(enc, "x").zfill(256)
    return {"params": params, "encSecKey": enc_sec_key}