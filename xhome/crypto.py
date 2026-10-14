"""Crypto primitives used by the Xiaomi Home protocols.

* Cloud API (Mi Home app)  -> RC4 (first 1024 keystream bytes dropped) + SHA1/SHA256 signing
* Local miIO (UDP 54321)    -> AES-128-CBC, key = MD5(token), iv = MD5(key + token)
"""
from __future__ import annotations

import base64
import hashlib
import os
import time

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# --------------------------------------------------------------------------- RC4
class RC4:
    """Plain RC4 stream cipher (not in `cryptography` anymore, so done by hand)."""

    def __init__(self, key: bytes):
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self._s, self._i, self._j = s, 0, 0

    def crypt(self, data: bytes) -> bytes:
        s, i, j = self._s, self._i, self._j
        out = bytearray(len(data))
        for n, b in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out[n] = b ^ s[(s[i] + s[j]) & 0xFF]
        self._i, self._j = i, j
        return bytes(out)


def _xiaomi_rc4(signed_nonce_b64: str) -> RC4:
    rc4 = RC4(base64.b64decode(signed_nonce_b64))
    rc4.crypt(bytes(1024))  # Xiaomi drops the first 1024 bytes of keystream
    return rc4


def rc4_encrypt_b64(signed_nonce_b64: str, text: str) -> str:
    return base64.b64encode(_xiaomi_rc4(signed_nonce_b64).crypt(text.encode())).decode()


def rc4_decrypt_b64(signed_nonce_b64: str, payload_b64: str) -> bytes:
    return _xiaomi_rc4(signed_nonce_b64).crypt(base64.b64decode(payload_b64))


# ------------------------------------------------------------- cloud request signing
def generate_nonce() -> str:
    """8 random bytes + current time in minutes (4 bytes, big endian), base64."""
    minutes = int(time.time() * 1000 / 60000)
    return base64.b64encode(os.urandom(8) + minutes.to_bytes(4, "big")).decode()


def signed_nonce(ssecurity_b64: str, nonce_b64: str) -> str:
    digest = hashlib.sha256(base64.b64decode(ssecurity_b64) + base64.b64decode(nonce_b64)).digest()
    return base64.b64encode(digest).decode()


def enc_signature(method: str, path: str, params: dict, snonce: str) -> str:
    """SHA1( METHOD & /path & k=v & ... & signed_nonce ), base64. `path` has no /app prefix."""
    parts = [method.upper(), path]
    parts += [f"{k}={v}" for k, v in params.items()]
    parts.append(snonce)
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()


def build_encrypted_params(method: str, path: str, params: dict, ssecurity: str) -> tuple[dict, str]:
    """Return (fields to send, signed_nonce needed to decrypt the response)."""
    nonce = generate_nonce()
    snonce = signed_nonce(ssecurity, nonce)
    fields = dict(params)
    fields["rc4_hash__"] = enc_signature(method, path, fields, snonce)
    fields = {k: rc4_encrypt_b64(snonce, str(v)) for k, v in fields.items()}
    fields["signature"] = enc_signature(method, path, fields, snonce)
    fields["ssecurity"] = ssecurity
    fields["_nonce"] = nonce
    return fields, snonce


# --------------------------------------------------------------------- local miIO AES
def _miio_key_iv(token: bytes) -> tuple[bytes, bytes]:
    key = hashlib.md5(token).digest()
    iv = hashlib.md5(key + token).digest()
    return key, iv


def miio_encrypt(plaintext: bytes, token: bytes) -> bytes:
    key, iv = _miio_key_iv(token)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def miio_decrypt(ciphertext: bytes, token: bytes) -> bytes:
    key, iv = _miio_key_iv(token)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    data = dec.update(ciphertext) + dec.finalize()
    pad = data[-1] if data else 0
    if 1 <= pad <= 16 and data.endswith(bytes([pad]) * pad):
        data = data[:-pad]
    return data
