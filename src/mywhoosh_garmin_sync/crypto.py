"""CryptoJS-compatible AES payload encryption (OpenSSL "Salted__" format)."""

import base64
import hashlib
import json
import os
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

_MAGIC = b"Salted__"
_SALT_LEN = 8
_KEY_LEN = 32
_IV_LEN = 16


def _evp_bytes_to_key(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """Derive key and IV like OpenSSL's EVP_BytesToKey with MD5 and one round."""
    derived = b""
    block = b""
    while len(derived) < _KEY_LEN + _IV_LEN:
        block = hashlib.md5(block + passphrase + salt).digest()
        derived += block
    return derived[:_KEY_LEN], derived[_KEY_LEN : _KEY_LEN + _IV_LEN]


def encrypt_payload(payload: dict[str, Any], passphrase: str) -> str:
    """Serialize payload as compact JSON and encrypt it as base64 OpenSSL salted AES-256-CBC."""
    salt = os.urandom(_SALT_LEN)
    key, iv = _evp_bytes_to_key(passphrase.encode(), salt)
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    encrypted = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))
    return base64.b64encode(_MAGIC + salt + encrypted).decode()


def decrypt_payload(ciphertext: str, passphrase: str) -> dict[str, Any]:
    """Decrypt a base64 OpenSSL salted AES-256-CBC string back into the JSON payload."""
    raw = base64.b64decode(ciphertext)
    if not raw.startswith(_MAGIC):
        raise ValueError("ciphertext is not in OpenSSL salted format")
    salt = raw[len(_MAGIC) : len(_MAGIC) + _SALT_LEN]
    key, iv = _evp_bytes_to_key(passphrase.encode(), salt)
    decrypted = AES.new(key, AES.MODE_CBC, iv).decrypt(raw[len(_MAGIC) + _SALT_LEN :])
    return json.loads(unpad(decrypted, AES.block_size))
