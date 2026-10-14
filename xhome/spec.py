"""MIoT-Spec: Xiaomi's public device description (services -> properties / actions / events).

The Xiaomi Home app renders every MIoT device from this spec. Properties are addressed as
(siid, piid) and actions as (siid, aiid); this module maps those numbers to readable names
like `light.on` or `fan.mode`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

INSTANCES_URL = "https://miot-spec.org/miot-spec-v2/instances?status=all"
INSTANCE_URL = "https://miot-spec.org/miot-spec-v2/instance"


def _urn_name(urn: str) -> str:
    # urn:miot-spec-v2:property:brightness:0000000D:yeelink-color1:1 -> brightness
    parts = urn.split(":")
    return parts[3] if len(parts) > 3 else urn


class Spec:
    def __init__(self, raw: dict):
        self.raw = raw
        self.type = raw.get("type", "")
        self.description = raw.get("description", "")
        self.services: list[dict] = []
        self.props: dict[str, dict] = {}
        self.actions: dict[str, dict] = {}
        names = [_urn_name(s["type"]) for s in raw.get("services", [])]
        for svc, sname in zip(raw.get("services", []), names):
            if names.count(sname) > 1:
                sname = f"{sname}_{svc['iid']}"
            service = {"siid": svc["iid"], "name": sname, "description": svc.get("description", ""),
                       "properties": [], "actions": []}
            for p in svc.get("properties", []):
                prop = {
                    "key": f"{sname}.{_urn_name(p['type'])}", "siid": svc["iid"], "piid": p["iid"],
                    "description": p.get("description", ""), "format": p.get("format", ""),
                    "access": p.get("access", []), "unit": p.get("unit"),
                    "range": p.get("value-range"), "values": p.get("value-list"),
                }
                if prop["key"] in self.props:
                    prop["key"] = f"{prop['key']}_{p['iid']}"
                self.props[prop["key"]] = prop
                service["properties"].append(prop)
            for a in svc.get("actions", []):
                piid_names = {p["iid"]: _urn_name(p["type"]) for p in svc.get("properties", [])}
                act = {
                    "key": f"{sname}.{_urn_name(a['type'])}", "siid": svc["iid"], "aiid": a["iid"],
                    "description": a.get("description", ""),
                    "in": [{"piid": i, "name": piid_names.get(i, str(i))} for i in a.get("in", [])],
                    "out": a.get("out", []),
                }
                if act["key"] in self.actions:
                    act["key"] = f"{act['key']}_{a['iid']}"
                self.actions[act["key"]] = act
                service["actions"].append(act)
            self.services.append(service)

    # ------------------------------------------------------------------ lookup
    def prop(self, key: str) -> dict:
        """Accepts `light.on`, `2.1` (siid.piid) or a unique suffix such as `brightness`."""
        return _lookup(key, self.props, "piid", "property")

    def action(self, key: str) -> dict:
        return _lookup(key, self.actions, "aiid", "action")

    def readable(self) -> list[dict]:
        return [p for p in self.props.values() if "read" in p["access"]]

    def to_dict(self) -> dict:
        return {"type": self.type, "description": self.description, "services": self.services}


def _lookup(key: str, table: dict[str, dict], iid_field: str, label: str) -> dict:
    if key in table:
        return table[key]
    if "." in key and all(part.isdigit() for part in key.split(".", 1)):
        siid, iid = (int(x) for x in key.split(".", 1))
        for item in table.values():
            if item["siid"] == siid and item[iid_field] == iid:
                return item
    hits = [v for k, v in table.items() if k.split(".", 1)[-1] == key or k.endswith("." + key)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise KeyError(f"{label} '{key}' is ambiguous: {', '.join(h['key'] for h in hits)}")
    raise KeyError(f"{label} '{key}' does not exist. Options: {', '.join(table) or '-'}")


def coerce(prop: dict, value):
    """Convert a CLI/web string into the type the property expects."""
    fmt = prop.get("format", "")
    if isinstance(value, str):
        v = value.strip()
        if fmt == "bool":
            if v.lower() in ("1", "true", "on", "yes"):
                return True
            if v.lower() in ("0", "false", "off", "no"):
                return False
            raise ValueError(f"invalid bool value: {value}")
        if prop.get("values") and not v.lstrip("-").isdigit():
            for item in prop["values"]:
                if item.get("description", "").lower() == v.lower():
                    return item["value"]
            opts = ", ".join(f"{i['value']}={i['description']}" for i in prop["values"])
            raise ValueError(f"invalid option: {value} ({opts})")
        if fmt.startswith(("int", "uint")):
            return int(float(v))
        if fmt == "float":
            return float(v)
        return value
    if fmt == "bool":
        return bool(value)
    return value


def describe_value(prop: dict, value) -> str:
    if prop.get("values"):
        for item in prop["values"]:
            if item.get("value") == value:
                return f"{value} ({item.get('description')})"
    unit = {"percentage": "%", "celsius": "°C", "kelvin": "K", "seconds": "s", "minutes": "min",
            "hours": "h", "days": "d", "rgb": "", "none": ""}.get(prop.get("unit") or "", prop.get("unit"))
    return f"{value}{unit}" if unit == "%" else (f"{value} {unit}" if unit else str(value))


class SpecStore:
    """Downloads specs from miot-spec.org once and caches them under data/specs/."""

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._instances: dict[str, str] | None = None
        self._mem: dict[str, Spec | None] = {}

    def _instance_index(self) -> dict[str, str]:
        if self._instances is not None:
            return self._instances
        path = self.dir / "_instances.json"
        if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
            try:
                data = requests.get(INSTANCES_URL, timeout=60).json()["instances"]
                best: dict[str, dict] = {}
                for inst in data:
                    cur = best.get(inst["model"])
                    rank = (inst.get("status") == "released", inst.get("version", 0))
                    if cur is None or rank > (cur.get("status") == "released", cur.get("version", 0)):
                        best[inst["model"]] = inst
                path.write_text(json.dumps({m: i["type"] for m, i in best.items()}), encoding="utf-8")
            except (requests.RequestException, ValueError, KeyError):
                if not path.exists():
                    raise
        self._instances = json.loads(path.read_text(encoding="utf-8"))
        return self._instances

    def get(self, model: str, urn: str | None = None) -> Spec | None:
        """Spec for a model, or None if the model is a legacy (non-MIoT) miIO device."""
        if model in self._mem:
            return self._mem[model]
        path = self.dir / f"{model}.json"
        raw = None
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            urn = urn or self._instance_index().get(model)
            if urn:
                raw = requests.get(INSTANCE_URL, params={"type": urn}, timeout=30).json()
                path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        spec = Spec(raw) if raw and raw.get("services") else None
        self._mem[model] = spec
        return spec
