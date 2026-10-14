"""High level facade: one device list, two transports (local LAN / cloud), automatic routing."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

from .cloud import CloudError, MiCloud
from .local import MiIODevice, MiIOError, MiIOTimeout, discover
from .spec import Spec, SpecStore, coerce

VIAS = ("auto", "local", "cloud")
# One data folder shared by the CLI and the desktop app (also outside any git checkout).
DEFAULT_DATA = Path(os.environ.get("XHOME_DATA") or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "XHome")
LEGACY_DATA = Path(__file__).resolve().parent.parent / "data"  # location used by early versions
LOCAL_COOLDOWN = 60  # s: after a LAN timeout, `auto` goes straight to the cloud for this long

# fields kept from the (very verbose) cloud device record
KEEP = ("did", "name", "model", "localip", "token", "mac", "isOnline", "parent_id", "parent_model",
        "room_name", "home_name", "server", "ssid", "rssi", "fw_version", "spec_type")


class XHomeError(Exception):
    pass


class XHome:
    def __init__(self, data_dir: Path | str | None = None):
        self.dir = Path(data_dir or DEFAULT_DATA)
        self.dir.mkdir(parents=True, exist_ok=True)
        if data_dir is None:
            _migrate_legacy(self.dir)
        self.cloud = MiCloud(self._read("session.json"))
        self.specs = SpecStore(self.dir / "specs")
        self._devices: dict[str, dict] = self._read("devices.json") or {}
        self._locals: dict[str, MiIODevice] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._local_down: dict[str, float] = {}  # did -> monotonic time until LAN is retried
        self._lock = threading.Lock()

    # ------------------------------------------------------------ persistence
    def _read(self, name: str):
        path = self.dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _write(self, name: str, data) -> None:
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / name)

    def save_session(self) -> None:
        self._write("session.json", self.cloud.to_dict())

    def save_devices(self) -> None:
        self._write("devices.json", self._devices)

    # --------------------------------------------------------------- devices
    def devices(self) -> list[dict]:
        return sorted(self._devices.values(), key=lambda d: (d.get("room_name") or "~", d.get("name") or ""))

    def refresh_devices(self, servers: list[str] | None = None) -> list[dict]:
        """Pull the device list (with tokens + local IPs) from the cloud and merge it."""
        servers = servers or [self.cloud.server]
        fresh: dict[str, dict] = {}
        for server in servers:
            for dev in self.cloud.devices(server):
                fresh[dev["did"]] = {k: dev.get(k) for k in KEEP} | {"source": "cloud"}
        with self._lock:
            for did, dev in self._devices.items():
                if dev.get("source") != "cloud" and did not in fresh:
                    fresh[did] = dev  # keep manually added devices
            self._devices = fresh
            self._locals.clear()
            self.save_devices()
            self.save_session()
        return self.devices()

    def add_manual(self, ip: str, token: str, name: str | None = None) -> dict:
        """Register a device without the cloud (token obtained elsewhere)."""
        dev = MiIODevice(ip, token)
        info = dev.info()
        did = str(dev.handshake()["did"])
        record = self._devices.get(did, {})
        record.update({"did": did, "name": name or record.get("name") or info.get("model"),
                       "model": info.get("model"), "localip": ip, "token": token,
                       "mac": info.get("mac"), "fw_version": info.get("fw_ver"), "isOnline": True,
                       "source": record.get("source", "manual")})
        with self._lock:
            self._devices[did] = record
            self._locals.pop(did, None)
            self.save_devices()
        return record

    def remove(self, query: str) -> dict:
        dev = self.find(query)
        with self._lock:
            self._devices.pop(dev["did"], None)
            self.save_devices()
        return dev

    def discover(self, timeout: float = 4.0) -> list[dict]:
        """LAN scan; also fixes stale IPs of known devices (DHCP changes)."""
        found = discover(timeout)
        changed = False
        for item in found:
            dev = self._devices.get(item["did"])
            item["name"] = dev.get("name") if dev else None
            item["model"] = dev.get("model") if dev else None
            item["has_token"] = bool(dev and dev.get("token")) or bool(item["token"])
            if dev and dev.get("localip") != item["ip"]:
                dev["localip"] = item["ip"]
                self._locals.pop(item["did"], None)
                changed = True
        if changed:
            self.save_devices()
        return found

    def find(self, query: str) -> dict:
        q = str(query).strip().lower()
        if q in self._devices:
            return self._devices[q]
        exact = [d for d in self._devices.values()
                 if q in (str(d.get("name", "")).lower(), str(d.get("localip", "")), str(d.get("mac", "")).lower())]
        if len(exact) == 1:
            return exact[0]
        partial = [d for d in self._devices.values()
                   if q in str(d.get("name", "")).lower() or q in str(d.get("model", "")).lower()
                   or q in str(d.get("room_name") or "").lower() or q in _aliases(d)]
        if len(partial) == 1:
            return partial[0]
        if not partial and not exact:
            raise XHomeError(f"device '{query}' not found - run `devices --refresh` first")
        names = ", ".join(f"{d.get('name')} [{d['did']}]" for d in (exact or partial))
        raise XHomeError(f"'{query}' matches several devices: {names}")

    def spec(self, dev: dict) -> Spec | None:
        return self.specs.get(dev["model"], dev.get("spec_type")) if dev.get("model") else None

    # --------------------------------------------------------------- routing
    @staticmethod
    def can_local(dev: dict) -> bool:
        token = dev.get("token") or ""
        return (len(token) == 32 and bool(dev.get("localip")) and str(dev.get("did", "")).isdigit()
                and not dev.get("parent_id"))

    def _local(self, dev: dict) -> MiIODevice:
        did = dev["did"]
        if did not in self._locals:
            self._locals[did] = MiIODevice(dev["localip"], dev["token"])
        return self._locals[did]

    def _run(self, dev: dict, via: str, local_fn, cloud_fn):
        """Execute on the requested transport; `auto` = LAN first, cloud as fallback."""
        if via not in VIAS:
            raise XHomeError(f"via must be one of {VIAS}")
        order = {"auto": ["local", "cloud"], "local": ["local"], "cloud": ["cloud"]}[via]
        errors = []
        lock = self._locks.setdefault(dev["did"], threading.Lock())
        for transport in order:
            if transport == "local" and not self.can_local(dev):
                errors.append("local: device has no LAN IP/token (BLE/Zigbee/not synced)")
                continue
            if (transport == "local" and via == "auto" and self.cloud.logged_in
                    and time.monotonic() < self._local_down.get(dev["did"], 0)):
                errors.append("local: skipped (timed out recently)")
                continue
            if transport == "cloud" and not self.cloud.logged_in:
                errors.append("cloud: not logged in")
                continue
            t0 = time.perf_counter()
            try:
                if transport == "local":
                    with lock:
                        local = self._local(dev)
                        # in auto mode fail fast, the cloud is the safety net
                        local.timeout, local.retries = (1.5, 1) if via == "auto" else (4.0, 2)
                        result = local_fn(local)
                    self._local_down.pop(dev["did"], None)
                else:
                    result = cloud_fn(self.cloud, dev.get("server") or self.cloud.server)
                return {"via": transport, "ms": round((time.perf_counter() - t0) * 1000), "result": result}
            except MiIOTimeout as exc:
                self._local_down[dev["did"]] = time.monotonic() + LOCAL_COOLDOWN
                errors.append(f"{transport}: {exc}")
            except (MiIOError, CloudError, OSError) as exc:
                errors.append(f"{transport}: {exc}")
        raise XHomeError("; ".join(errors))

    # --------------------------------------------------------------- actions
    def get(self, dev: dict, keys: list[str] | None = None, via: str = "auto") -> dict:
        spec = self.spec(dev)
        if spec is None:
            raise XHomeError(f"{dev.get('model')} is not a MIoT device - use the `raw` command")
        props = [spec.prop(k) for k in keys] if keys else spec.readable()
        pairs = [(p["siid"], p["piid"]) for p in props]
        out = self._run(dev, via,
                        lambda d: d.miot_get(pairs),
                        lambda c, srv: _chunked(lambda chunk: c.miot_get(dev["did"], chunk, srv), pairs))
        by_id = {(r.get("siid"), r.get("piid")): r for r in out["result"]}
        out["values"] = []
        for p in props:
            r = by_id.get((p["siid"], p["piid"]), {})
            out["values"].append({"key": p["key"], "siid": p["siid"], "piid": p["piid"],
                                  "value": r.get("value"), "code": r.get("code", -1)})
        del out["result"]
        return out

    def set(self, dev: dict, key: str, value, via: str = "auto") -> dict:
        spec = self.spec(dev)
        if spec is None:
            raise XHomeError(f"{dev.get('model')} is not a MIoT device - use the `raw` command")
        prop = spec.prop(key)
        if "write" not in prop["access"]:
            raise XHomeError(f"property {prop['key']} is read-only")
        value = coerce(prop, value)
        out = self._run(dev, via,
                        lambda d: d.miot_set(prop["siid"], prop["piid"], value),
                        lambda c, srv: c.miot_set(dev["did"], prop["siid"], prop["piid"], value, srv))
        code = (out["result"] or [{}])[0].get("code", 0)
        if code not in (0, 1):
            raise XHomeError(f"device rejected {prop['key']}={value} (code {code})")
        out.update(key=prop["key"], value=value)
        return out

    def action(self, dev: dict, key: str, args: list | None = None, via: str = "auto") -> dict:
        spec = self.spec(dev)
        if spec is None:
            raise XHomeError(f"{dev.get('model')} is not a MIoT device - use the `raw` command")
        act = spec.action(key)
        args = list(args or [])
        if len(args) != len(act["in"]):
            names = ", ".join(i["name"] for i in act["in"]) or "no arguments"
            raise XHomeError(f"action {act['key']} needs {len(act['in'])} argument(s) ({names})")
        ins = []
        for item, raw in zip(act["in"], args):
            prop = next((p for p in spec.props.values()
                         if p["siid"] == act["siid"] and p["piid"] == item["piid"]), {})
            ins.append(coerce(prop, raw) if prop else raw)  # "in" is a positional list of values
        out = self._run(dev, via,
                        lambda d: d.miot_action(act["siid"], act["aiid"], ins),
                        lambda c, srv: c.miot_action(dev["did"], act["siid"], act["aiid"], ins, srv))
        out["key"] = act["key"]
        return out

    def raw(self, dev: dict, method: str, params=None, via: str = "auto") -> dict:
        return self._run(dev, via, lambda d: d.send(method, params),
                         lambda c, srv: c.rpc(dev["did"], method, params, srv))

    def info(self, dev: dict, via: str = "auto") -> dict:
        return self.raw(dev, "miIO.info", [], via)


def _migrate_legacy(target: Path) -> None:
    """Move session/devices from the old ./data folder into the shared data folder (once)."""
    if (target / "session.json").exists() or not (LEGACY_DATA / "session.json").exists():
        return
    for name in ("session.json", "devices.json"):
        if (LEGACY_DATA / name).exists():
            shutil.move(str(LEGACY_DATA / name), str(target / name))
    if (LEGACY_DATA / "specs").is_dir():
        shutil.copytree(LEGACY_DATA / "specs", target / "specs", dirs_exist_ok=True)


def _aliases(dev: dict) -> list[str]:
    """Short names people actually type: `ac` for .airc. / .aircondition. models, etc."""
    model = str(dev.get("model", ""))
    table = {".airc.": "ac", ".aircondition.": "ac", ".light.": "light", ".plug.": "plug",
             ".vacuum.": "vacuum", ".airpurifier.": "purifier", ".fan.": "fan", ".camera.": "camera"}
    return [alias for frag, alias in table.items() if frag in model]


def _chunked(fn, items: list, size: int = 20) -> list:
    out = []
    for i in range(0, len(items), size):
        out += fn(items[i:i + size]) or []
    return out
