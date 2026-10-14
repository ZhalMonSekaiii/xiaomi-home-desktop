"""Tiny miIO device simulator (a fake Yeelight bulb) for offline testing of the LAN transport.

    python tests/fake_miio.py            # listens on 127.0.0.1:54321
"""
from __future__ import annotations

import hashlib
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xhome.crypto import miio_decrypt, miio_encrypt  # noqa: E402

TOKEN = "00112233445566778899aabbccddeeff"
DID = 123456789
MODEL = "yeelink.light.color1"


AC_MODEL = "xiaomi.airc.ra4r00"
AC_PROPS = {(2, 1): True, (2, 2): 2, (2, 4): 26.0, (2, 7): False, (2, 10): False, (2, 11): False, (2, 29): False,
            (3, 2): 0, (3, 3): False, (3, 4): True, (4, 7): 28.4, (5, 1): True, (6, 1): True, (12, 7): 31.2,
            (20, 1): 93.92}


class FakeDevice:
    def __init__(self, host: str = "127.0.0.1", token: str = TOKEN, model: str = MODEL):
        self.token = bytes.fromhex(token)
        self.model = model
        self.boot = time.monotonic() - 5000
        self.props = dict(AC_PROPS) if model == AC_MODEL else {(2, 1): False, (2, 2): 50, (2, 3): 0xFF8800, (2, 4): 4000, (2, 5): 0}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, 54321))
        self.sock.settimeout(0.3)
        self._stop = False

    def _header(self, length: int) -> bytes:
        return struct.pack(">HHIII", 0x2131, length, 0, DID, int(time.monotonic() - self.boot))

    def handle(self, method: str, params):
        if method == "miIO.info":
            return {"model": self.model, "fw_ver": "1.0.0", "mac": "AA:BB:CC:DD:EE:FF", "token": "<hidden>"}
        if method == "get_properties":
            return [{"did": p["did"], "siid": p["siid"], "piid": p["piid"], "code": 0,
                     "value": self.props[(p["siid"], p["piid"])]} if (p["siid"], p["piid"]) in self.props
                    else {"did": p["did"], "siid": p["siid"], "piid": p["piid"], "code": -4003} for p in params]
        if method == "set_properties":
            out = []
            for p in params:
                self.props[(p["siid"], p["piid"])] = p["value"]
                out.append({"did": p["did"], "siid": p["siid"], "piid": p["piid"], "code": 0})
            return out
        if method == "action":
            if (params["siid"], params["aiid"]) == (2, 1):  # light.toggle
                self.props[(2, 1)] = not self.props[(2, 1)]
            return {"did": params["did"], "siid": params["siid"], "aiid": params["aiid"], "code": 0, "out": []}
        raise LookupError(method)

    def serve(self):
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(4096)
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError:  # socket closed by stop()
                break
            if data[4:] == b"\xff" * 28:  # hello
                self.sock.sendto(self._header(32) + b"\xff" * 16, addr)
                continue
            body = data[32:]
            if hashlib.md5(data[:16] + self.token + body).digest() != data[16:32]:
                continue  # wrong token: real devices stay silent too
            req = json.loads(miio_decrypt(body, self.token).rstrip(b"\x00"))
            try:
                reply = {"id": req["id"], "result": self.handle(req["method"], req.get("params"))}
            except LookupError:
                reply = {"id": req["id"], "error": {"code": -9999, "message": "method not found"}}
            enc = miio_encrypt(json.dumps(reply).encode(), self.token)
            header = self._header(32 + len(enc))
            self.sock.sendto(header + hashlib.md5(header + self.token + enc).digest() + enc, addr)

    def start(self) -> "FakeDevice":
        threading.Thread(target=self.serve, daemon=True).start()
        return self

    def stop(self):
        self._stop = True
        self.sock.close()


if __name__ == "__main__":
    print(f"fake {MODEL} on 127.0.0.1:54321 token={TOKEN}")
    FakeDevice().serve()
