"""Local LAN transport: the miIO binary protocol over UDP port 54321.

Packet layout (all big endian):

    0      2      4          8         12        16                       32
    +------+------+----------+---------+---------+------------------------+-------------
    |2131  |length|unknown   |device id|stamp    |checksum (MD5, 16 bytes)| AES payload
    +------+------+----------+---------+---------+------------------------+-------------

* "hello" = 0x2131 0x0020 followed by 28 x 0xFF. Every device answers it with its
  device id + stamp (seconds since boot), even without a token.
* checksum = MD5(header[0:16] + token + encrypted_payload)
* payload  = AES-128-CBC(JSON + b"\\x00"), key = MD5(token), iv = MD5(key + token)
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import random
import re
import socket
import struct
import time

from .crypto import miio_decrypt, miio_encrypt

PORT = 54321
MAGIC = 0x2131
HELLO = bytes.fromhex("21310020") + b"\xff" * 28


class MiIOError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message if code is None else f"{message} (code {code})")
        self.code = code


class MiIOTimeout(MiIOError):
    pass


def _parse_header(data: bytes) -> tuple[int, int, int, bytes]:
    if len(data) < 32:
        raise MiIOError("packet too short")
    magic, length, _unknown, did, stamp = struct.unpack(">HHIII", data[:16])
    if magic != MAGIC:
        raise MiIOError(f"bad magic 0x{magic:04x}")
    return length, did, stamp, data[16:32]


def discover(timeout: float = 4.0, target: str | None = None) -> list[dict]:
    """Broadcast a hello packet and collect every miIO device that answers.

    Returns [{ip, did, stamp, token}] - `token` is only set for devices that are still
    un-provisioned (they leak it in the checksum field); provisioned ones return 0xff/0x00.
    """
    targets = [target] if target else _broadcast_addresses()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    found: dict[str, dict] = {}
    try:
        deadline = time.monotonic() + timeout
        next_send = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:  # resend a few times, UDP is lossy
                for addr in targets:
                    try:
                        sock.sendto(HELLO, (addr, PORT))
                    except OSError:
                        pass
                next_send = time.monotonic() + 1.5
            try:
                data, (ip, _port) = sock.recvfrom(4096)
            except (socket.timeout, ConnectionResetError):
                continue
            try:
                _length, did, stamp, checksum = _parse_header(data)
            except MiIOError:
                continue
            if data == HELLO:  # our own broadcast echoed back
                continue
            token = checksum.hex() if checksum not in (b"\xff" * 16, b"\x00" * 16) else None
            found[ip] = {"ip": ip, "did": str(did), "stamp": stamp, "token": token}
    finally:
        sock.close()
    return sorted(found.values(), key=lambda d: tuple(int(p) for p in d["ip"].split(".")))


def _broadcast_addresses() -> list[str]:
    addrs = {"255.255.255.255"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                # assume /24 - by far the most common home network size
                addrs.add(str(ipaddress.ip_network(f"{ip}/24", strict=False).broadcast_address))
    except OSError:
        pass
    return sorted(addrs)


class MiIODevice:
    """Talks to one device over the LAN with its 32-hex-char token."""

    def __init__(self, ip: str, token: str, timeout: float = 4.0, retries: int = 2):
        if not token or len(token) != 32:
            raise MiIOError("token must be 32 hex characters")
        self.ip = ip
        self.token = bytes.fromhex(token)
        self.timeout = timeout
        self.retries = retries
        self._did: int | None = None
        self._stamp = 0
        self._stamp_at = 0.0
        self._msg_id = random.randint(1, 9000)

    # ----------------------------------------------------------------- low level
    def _sock(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.timeout)
        return s

    def handshake(self) -> dict:
        with self._sock() as s:
            s.sendto(HELLO, (self.ip, PORT))
            try:
                data, _ = s.recvfrom(4096)
            except (socket.timeout, ConnectionResetError) as exc:
                raise MiIOTimeout(f"{self.ip} did not answer the handshake") from exc
        _length, did, stamp, _checksum = _parse_header(data)
        self._did, self._stamp, self._stamp_at = did, stamp, time.monotonic()
        return {"did": did, "stamp": stamp}

    def _packet(self, payload: bytes) -> bytes:
        encrypted = miio_encrypt(payload, self.token)
        stamp = self._stamp + int(time.monotonic() - self._stamp_at) + 1
        header = struct.pack(">HHIII", MAGIC, 32 + len(encrypted), 0, self._did, stamp)
        checksum = hashlib.md5(header + self.token + encrypted).digest()
        return header + checksum + encrypted

    def _decode(self, data: bytes) -> dict | None:
        length, _did, _stamp, checksum = _parse_header(data)
        body = data[32:length]
        if not body:
            return None
        if hashlib.md5(data[:16] + self.token + body).digest() != checksum:
            raise MiIOError("checksum mismatch - wrong token?")
        raw = miio_decrypt(body, self.token).rstrip(b"\x00")
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # some firmwares emit slightly broken JSON
        text = text.replace(',,"otu_stat"', ',"otu_stat"').replace('"value":00', '"value":0')
        # xiaomi.airc.ra4r00 double-quotes strings: "value":""12345/ABCDEFG""
        text = re.sub(r'"value":""([^"]+)""', r'"value":"\1"', text)
        return json.loads(text)

    def send(self, method: str, params=None) -> object:
        """Send one RPC call and return its `result` (raises MiIOError on device error)."""
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            # stamps drift / devices reboot: re-handshake on first call and after errors
            if self._did is None or attempt > 0 or time.monotonic() - self._stamp_at > 120:
                self.handshake()
            self._msg_id = (self._msg_id + (100 if attempt else 1)) % 1_000_000 or 1
            msg_id = self._msg_id
            payload = json.dumps(
                {"id": msg_id, "method": method, "params": [] if params is None else params},
                separators=(",", ":"),
            ).encode() + b"\x00"
            with self._sock() as s:
                s.sendto(self._packet(payload), (self.ip, PORT))
                deadline = time.monotonic() + self.timeout
                try:
                    while True:
                        s.settimeout(max(0.1, deadline - time.monotonic()))
                        data, _ = s.recvfrom(16384)
                        reply = self._decode(data)
                        if reply is None or reply.get("id") != msg_id:
                            continue  # stale reply for an older request
                        if "error" in reply:
                            err = reply["error"]
                            raise MiIOError(err.get("message", str(err)), err.get("code"))
                        return reply.get("result")
                except (socket.timeout, ConnectionResetError) as exc:
                    last_exc = MiIOTimeout(f"{self.ip} did not answer '{method}'")
                    continue
        raise last_exc or MiIOTimeout(f"{self.ip} did not answer")

    # ---------------------------------------------------------------- helpers
    def info(self) -> dict:
        return self.send("miIO.info")

    def miot_get(self, props: list[tuple[int, int]], chunk: int = 12) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(props), chunk):
            params = [{"did": f"{s}-{p}", "siid": s, "piid": p} for s, p in props[i:i + chunk]]
            out += self.send("get_properties", params) or []
        return out

    def miot_set(self, siid: int, piid: int, value) -> list[dict]:
        return self.send("set_properties", [{"did": f"{siid}-{piid}", "siid": siid, "piid": piid, "value": value}])

    def miot_action(self, siid: int, aiid: int, ins: list | None = None) -> dict:
        return self.send("action", {"did": f"{siid}-{aiid}", "siid": siid, "aiid": aiid, "in": ins or []})
