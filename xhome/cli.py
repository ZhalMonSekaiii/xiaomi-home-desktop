"""Command line interface:  python -m xhome <command> ...  (python -m xhome -h for help)"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time

from .cloud import SERVER_NAMES, SERVERS, CloudError
from .hub import VIAS, XHome, XHomeError
from .local import MiIOError
from .spec import describe_value


def _table(rows: list[list], headers: list[str]) -> None:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))


def _parse_value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _via_note(out: dict) -> str:
    return f"[via {out['via']}, {out['ms']} ms]"


# ----------------------------------------------------------------------- commands
def cmd_login(hub: XHome, a) -> None:
    if a.server:
        hub.cloud.server = a.server
    if a.password:
        user = input("Email / phone number / Xiaomi ID: ").strip()
        pw = getpass.getpass("Password (hidden): ")
        hub.cloud.password_login(user, pw)
    else:
        qr = hub.cloud.qr_login_start()
        path = hub.dir / "login_qr.png"
        path.write_bytes(hub.cloud.qr_image(qr["qr"]))
        if a.browser:
            # the link behind the QR redirects to Xiaomi's own login page; signing in there
            # approves this login ticket, no app scanner needed
            print("Xiaomi's official login page was opened in your browser. Sign in there with your Xiaomi Home account.")
            print(f"If no browser opened, paste this link into one:\n  {qr['login_url']}")
        else:
            print(f"Login QR opened ({path}). Either:")
            print("  a) scan the QR with your PHONE CAMERA / Google Lens, open the link, sign in and confirm")
            print("  b) or open this link in any browser and sign in on Xiaomi's official page:")
            print(f"     {qr['login_url']}")
            print("  (Do not use the app's 'Add device' scanner - it only understands device QR codes.)")
        print(f"Waiting up to {qr['timeout'] // 60} minutes for the login... (Ctrl+C to cancel)")
        if not a.no_open:
            try:
                if a.browser:
                    import webbrowser
                    webbrowser.open(qr["login_url"])
                else:
                    os.startfile(path)  # type: ignore[attr-defined]  (Windows)
            except (AttributeError, OSError):
                pass
        try:
            hub.cloud.qr_login_wait(qr["lp"], qr["timeout"])
        finally:
            path.unlink(missing_ok=True)
    hub.save_session()
    print(f"Logged in. server={hub.cloud.server}")
    devs = hub.refresh_devices()
    if not devs:
        print("No devices on this server, checking the other regions...")
        counts = hub.cloud.find_server()
        best = max(counts, key=counts.get)
        if counts[best] > 0:
            hub.cloud.server = best
            devs = hub.refresh_devices()
    print(f"{len(devs)} device(s) synced from server '{hub.cloud.server}'. List them: python -m xhome devices")


def cmd_logout(hub: XHome, a) -> None:
    hub.cloud.logout()
    hub.save_session()
    print("Cloud session removed (the device list and LAN tokens are kept).")


def cmd_servers(hub: XHome, a) -> None:
    counts = hub.cloud.find_server()
    _table([[s, SERVER_NAMES[s], "error" if n < 0 else n, "<- active" if s == hub.cloud.server else ""]
            for s, n in counts.items()], ["server", "region", "devices", ""])
    if a.use:
        hub.cloud.server = a.use
        hub.save_session()
        print(f"Active server set to {a.use}")


def cmd_devices(hub: XHome, a) -> None:
    if a.refresh or a.all_servers:
        servers = SERVERS if a.all_servers else [a.server or hub.cloud.server]
        hub.refresh_devices(servers)
    devs = hub.devices()
    if a.json:
        if not a.tokens:  # LAN tokens are credentials: only print them when asked to
            devs = [{k: v for k, v in d.items() if k != "token"} for d in devs]
        print(json.dumps(devs, indent=2, ensure_ascii=False))
        return
    rows = []
    for d in devs:
        mode = "local+cloud" if XHome.can_local(d) else ("cloud" if d.get("source") == "cloud" else "local?")
        row = [d.get("name"), d.get("room_name"), d.get("model"), d["did"], d.get("localip"),
               "online" if d.get("isOnline") else "offline", mode]
        if a.tokens:
            row.append(d.get("token"))
        rows.append(row)
    headers = ["name", "room", "model", "did", "ip", "status", "path"] + (["token"] if a.tokens else [])
    _table(rows, headers)
    print(f"\n{len(devs)} device(s). Cloud: {'logged in (' + hub.cloud.server + ')' if hub.cloud.logged_in else 'not logged in'}")


def cmd_discover(hub: XHome, a) -> None:
    print(f"Broadcasting miIO hello on the LAN for {a.timeout} s...")
    found = hub.discover(a.timeout)
    _table([[f["ip"], f["did"], f.get("name") or "(unknown)", f.get("model"),
             "yes" if f["has_token"] else "no", f["token"] or ""] for f in found],
           ["ip", "did", "name", "model", "token?", "leaked token (unprovisioned)"])
    print(f"\n{len(found)} device(s) answered.")


def cmd_add(hub: XHome, a) -> None:
    dev = hub.add_manual(a.ip, a.token, a.name)
    print(f"Added: {dev['name']} model={dev['model']} did={dev['did']}")


def cmd_remove(hub: XHome, a) -> None:
    dev = hub.remove(a.device)
    print(f"Removed: {dev.get('name')} [{dev['did']}]")


def cmd_info(hub: XHome, a) -> None:
    dev = hub.find(a.device)
    out = hub.info(dev, a.via)
    print(json.dumps(out["result"], indent=2, ensure_ascii=False))
    print(_via_note(out))


def cmd_token(hub: XHome, a) -> None:
    dev = hub.find(a.device)
    print(f"{dev.get('name')}  ip={dev.get('localip')}  token={dev.get('token')}")


def cmd_spec(hub: XHome, a) -> None:
    try:
        dev = hub.find(a.device)
        spec = hub.spec(dev)
        model = dev.get("model")
    except XHomeError:
        if "." not in a.device:  # not a model id like yeelink.light.color1 either
            raise
        model, spec = a.device, hub.specs.get(a.device)
    if spec is None:
        print(f"{model}: no MIoT spec (legacy miIO device). Use the `raw` command.")
        return
    if a.json:
        print(json.dumps(spec.to_dict(), indent=2, ensure_ascii=False))
        return
    print(f"{model}  {spec.type}\n")
    for svc in spec.services:
        print(f"[{svc['siid']}] {svc['name']} - {svc['description']}")
        for p in svc["properties"]:
            acc = "".join(x[0] for x in p["access"])  # r w n
            extra = ""
            if p["values"]:
                extra = " {" + ", ".join(f"{v['value']}={v['description']}" for v in p["values"]) + "}"
            elif p["range"]:
                extra = f" [{p['range'][0]}..{p['range'][1]} step {p['range'][2]}]"
            unit = f" {p['unit']}" if p["unit"] and p["unit"] != "none" else ""
            print(f"    {p['siid']}.{p['piid']:<3} {p['key']:<36} {p['format']:<7} {acc:<3}{unit}{extra}")
        for act in svc["actions"]:
            ins = ", ".join(i["name"] for i in act["in"])
            print(f"    a{act['siid']}.{act['aiid']:<2} {act['key']:<36} action({ins})")
    print("\nAccess: r=read w=write n=notify. Use the key name (e.g. light.on) or siid.piid (e.g. 2.1).")


def cmd_get(hub: XHome, a) -> None:
    dev = hub.find(a.device)
    out = hub.get(dev, a.keys or None, a.via)
    spec = hub.spec(dev)
    rows = []
    for v in out["values"]:
        val = describe_value(spec.props[v["key"]], v["value"]) if v["code"] == 0 else f"error {v['code']}"
        rows.append([v["key"], f"{v['siid']}.{v['piid']}", val])
    if a.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    _table(rows, ["property", "id", "value"])
    print(_via_note(out))


def cmd_set(hub: XHome, a) -> None:
    dev = hub.find(a.device)
    out = hub.set(dev, a.key, a.value, a.via)
    print(f"OK {out['key']} = {out['value']!r} {_via_note(out)}")


def cmd_action(hub: XHome, a) -> None:
    dev = hub.find(a.device)
    out = hub.action(dev, a.key, a.args, a.via)
    print(f"OK {out['key']} -> {json.dumps(out['result'], ensure_ascii=False)} {_via_note(out)}")


def cmd_raw(hub: XHome, a) -> None:
    dev = hub.find(a.device)
    params = _parse_value(a.params) if a.params is not None else []
    out = hub.raw(dev, a.method, params, a.via)
    print(json.dumps(out["result"], indent=2, ensure_ascii=False))
    print(_via_note(out))


def cmd_watch(hub: XHome, a) -> None:
    dev = hub.find(a.device)
    spec = hub.spec(dev)
    print(f"Watching {dev.get('name')} every {a.interval}s (Ctrl+C to stop)")
    last: dict[str, object] = {}
    while True:
        try:
            out = hub.get(dev, a.keys or None, a.via)
            stamp = time.strftime("%H:%M:%S")
            for v in out["values"]:
                if v["code"] != 0:
                    continue
                if last.get(v["key"], object()) != v["value"]:
                    tag = "initial" if v["key"] not in last else "CHANGED"
                    print(f"{stamp} [{out['via']}] {tag:<7} {v['key']} = "
                          f"{describe_value(spec.props[v['key']], v['value'])}")
                    last[v["key"]] = v["value"]
        except (XHomeError, CloudError, MiIOError) as exc:
            print(f"{time.strftime('%H:%M:%S')} error: {exc}")
        time.sleep(a.interval)


def cmd_serve(hub: XHome, a) -> None:
    import uvicorn

    from .web import create_app

    url = f"http://{a.host}:{a.port}"
    print(f"Dashboard: {url}  (Ctrl+C to stop)")
    if not a.no_open:
        import threading
        import webbrowser
        threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run(create_app(hub), host=a.host, port=a.port, log_level="warning")


def cmd_app(hub: XHome, a) -> None:
    from .desktop import main as app_main
    app_main(str(hub.dir))


# -------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m xhome",
                                description="Xiaomi Home client: control devices over the LAN (miIO) and the cloud (Mi Home API)")
    p.add_argument("--data", help="data folder (default: %%LOCALAPPDATA%%\\XHome)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, device=False, via=False):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        if device:
            sp.add_argument("device", help="device name / did / ip")
        if via:
            sp.add_argument("--via", choices=VIAS, default="auto", help="connection path (default auto: LAN first, then cloud)")
        return sp

    sp = add("login", cmd_login, "log in to your Xiaomi account (QR, --browser or --password)")
    sp.add_argument("--password", action="store_true", help="log in with a password instead of QR")
    sp.add_argument("--browser", action="store_true", help="open Xiaomi's official login page in the browser (no scanning)")
    sp.add_argument("--server", choices=SERVERS, help="server region (Southeast Asia is usually sg)")
    sp.add_argument("--no-open", action="store_true", help="do not open the QR image / browser automatically")
    add("logout", cmd_logout, "delete the cloud session")
    sp = add("servers", cmd_servers, "count devices on every server region")
    sp.add_argument("--use", choices=SERVERS, help="set the active server")

    sp = add("devices", cmd_devices, "list devices")
    sp.add_argument("--refresh", action="store_true", help="re-sync from the cloud")
    sp.add_argument("--server", choices=SERVERS)
    sp.add_argument("--all-servers", action="store_true", help="fetch from every region")
    sp.add_argument("--tokens", action="store_true", help="show LAN tokens (credentials!)")
    sp.add_argument("--json", action="store_true")

    sp = add("discover", cmd_discover, "scan the LAN for miIO devices")
    sp.add_argument("--timeout", type=float, default=4.0)
    sp = add("add", cmd_add, "add a LAN device manually (ip + token)")
    sp.add_argument("ip")
    sp.add_argument("token")
    sp.add_argument("--name")
    add("remove", cmd_remove, "remove a device from the local list", device=True)
    add("token", cmd_token, "show a device's LAN token (credential!)", device=True)
    add("info", cmd_info, "miIO.info (firmware, wifi, ...)", device=True, via=True)

    sp = add("spec", cmd_spec, "show the features (MIoT spec) of a device/model", device=True)
    sp.add_argument("--json", action="store_true")
    sp = add("get", cmd_get, "read properties (all if none given)", device=True, via=True)
    sp.add_argument("keys", nargs="*", help="e.g. light.on light.brightness")
    sp.add_argument("--json", action="store_true")
    sp = add("set", cmd_set, "change a property", device=True, via=True)
    sp.add_argument("key")
    sp.add_argument("value")
    sp = add("action", cmd_action, "run a MIoT action", device=True, via=True)
    sp.add_argument("key")
    sp.add_argument("args", nargs="*")
    sp = add("raw", cmd_raw, "send a raw miIO command", device=True, via=True)
    sp.add_argument("method", help="e.g. miIO.info, get_prop, set_power")
    sp.add_argument("params", nargs="?", help='JSON, e.g. \'["power","bright"]\'')
    sp = add("watch", cmd_watch, "watch for state changes (polling)", device=True, via=True)
    sp.add_argument("keys", nargs="*")
    sp.add_argument("--interval", type=float, default=3.0)

    add("app", cmd_app, "open the desktop app (own window)")
    sp = add("serve", cmd_serve, "run the local web dashboard")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    hub = XHome(args.data)
    try:
        args.fn(hub, args)
    except KeyboardInterrupt:
        print("\nstopped.")
        return 130
    except (XHomeError, CloudError, MiIOError, KeyError, ValueError) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"Error: {msg}", file=sys.stderr)
        return 1
    return 0
