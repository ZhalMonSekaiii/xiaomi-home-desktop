"""Offline self-test: crypto vectors, cloud request signing round-trip, LAN protocol against the simulator.

    python tests/test_offline.py
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fake_miio import TOKEN, FakeDevice  # noqa: E402

from xhome.crypto import RC4, build_encrypted_params, miio_decrypt, miio_encrypt, rc4_decrypt_b64  # noqa: E402
from xhome.hub import XHome, XHomeError  # noqa: E402
from xhome.local import MiIODevice, MiIOError  # noqa: E402


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


def test_crypto():
    check("rc4 vector", RC4(b"Key").crypt(b"Plaintext").hex().upper() == "BBF316E8D940AF0AD3")
    tok = bytes.fromhex(TOKEN)
    check("aes round trip", all(miio_decrypt(miio_encrypt(b"a" * n, tok), tok) == b"a" * n for n in range(40)))
    ssecurity = base64.b64encode(os.urandom(16)).decode()
    fields, snonce = build_encrypted_params("POST", "/v2/homeroom/gethome", {"data": '{"x":1}'}, ssecurity)
    check("cloud fields", set(fields) == {"data", "rc4_hash__", "signature", "ssecurity", "_nonce"})
    check("cloud rc4 round trip", rc4_decrypt_b64(snonce, fields["data"]) == b'{"x":1}')


def test_local():
    dev = FakeDevice().start()
    try:
        raw = MiIODevice("127.0.0.1", TOKEN, timeout=2)
        check("miIO.info", raw.info()["model"] == "yeelink.light.color1")
        try:
            MiIODevice("127.0.0.1", "ff" * 16, timeout=0.5, retries=0).info()
            check("wrong token is rejected", False)
        except MiIOError:
            check("wrong token is rejected", True)

        hub = XHome(tempfile.mkdtemp())
        rec = hub.add_manual("127.0.0.1", TOKEN, "Test Bulb")
        check("add manual", rec["did"] == "123456789" and hub.can_local(rec))
        out = hub.get(rec, via="local")
        values = {v["key"]: v["value"] for v in out["values"] if v["code"] == 0}
        check("get all via spec", values.get("light.brightness") == 50 and values.get("light.on") is False)
        hub.set(rec, "light.on", "on", via="local")
        hub.set(rec, "brightness", "80", via="local")
        values = {v["key"]: v["value"] for v in hub.get(rec, ["light.on", "2.2"], via="local")["values"]}
        check("set + read back", values == {"light.on": True, "light.brightness": 80})
        hub.action(rec, "light.toggle", via="local")
        check("action toggle", hub.get(rec, ["light.on"], via="local")["values"][0]["value"] is False)
        try:
            hub.get(rec, via="cloud")
            check("cloud without login fails cleanly", False)
        except XHomeError as exc:
            check("cloud without login fails cleanly", "not logged in" in str(exc))
    finally:
        dev.stop()


if __name__ == "__main__":
    test_crypto()
    test_local()
    print("all good")
