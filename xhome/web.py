"""Local web dashboard (FastAPI). Run with:  python -m xhome serve"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .cloud import SERVER_NAMES, CloudError, MiCloud
from .hub import XHome, XHomeError
from .local import MiIOError

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class SetBody(BaseModel):
    key: str
    value: Any
    via: str = "auto"


class ActionBody(BaseModel):
    key: str
    args: list[Any] = []
    via: str = "auto"


class RawBody(BaseModel):
    method: str
    params: Any = None
    via: str = "auto"


class AddBody(BaseModel):
    ip: str
    token: str
    name: str | None = None


class ServerBody(BaseModel):
    server: str


def _public(dev: dict) -> dict:
    out = {k: v for k, v in dev.items() if k != "token"}
    out["has_token"] = bool(dev.get("token"))
    out["can_local"] = XHome.can_local(dev)
    return out


def create_app(hub: XHome) -> FastAPI:
    app = FastAPI(title="XHome", docs_url="/api/docs", redoc_url=None)
    login = {"status": "idle", "message": "", "png": b"", "login_url": None, "token": None}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # The API controls real devices: refuse DNS-rebinding hosts and cross-site writes.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "host not allowed"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-xhome") != "1":
            return JSONResponse({"detail": "X-XHome header required"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(XHomeError)
    @app.exception_handler(CloudError)
    @app.exception_handler(MiIOError)
    @app.exception_handler(KeyError)
    @app.exception_handler(ValueError)
    async def errors(_request: Request, exc: Exception):
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return JSONResponse({"detail": msg}, status_code=400)

    def device(did: str) -> dict:
        try:
            return hub.find(did)
        except XHomeError as exc:
            raise HTTPException(404, str(exc)) from exc

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(STATIC / "xhome.ico")

    # ------------------------------------------------------------------ cloud
    @app.get("/api/state")
    def state():
        return {"logged_in": hub.cloud.logged_in, "user_id": hub.cloud.user_id, "server": hub.cloud.server,
                "servers": SERVER_NAMES, "device_count": len(hub.devices()), "login": login["status"]}

    @app.post("/api/server")
    def set_server(body: ServerBody):
        if body.server not in SERVER_NAMES:
            raise HTTPException(400, "unknown server")
        hub.cloud.server = body.server
        hub.save_session()
        return state()

    @app.post("/api/login/qr")
    def login_qr():
        cloud = MiCloud({"server": hub.cloud.server})
        qr = cloud.qr_login_start()
        token = object()  # a newer QR request makes older waiters give up
        login.update(status="waiting", message="", png=cloud.qr_image(qr["qr"]),
                     login_url=qr["login_url"], token=token)

        def wait():
            try:
                cloud.qr_login_wait(qr["lp"], qr["timeout"], cancel=lambda: login.get("token") is not token)
                hub.cloud = cloud
                hub.save_session()
                login.update(status="syncing")
                devs = hub.refresh_devices()
                if not devs:
                    counts = cloud.find_server()
                    best = max(counts, key=counts.get)
                    if counts[best] > 0:
                        cloud.server = best
                        hub.refresh_devices()
                login.update(status="ok", message=f"{len(hub.devices())} device(s) synced")
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                if login.get("token") is token:
                    login.update(status="error", message=str(exc))

        threading.Thread(target=wait, daemon=True).start()
        return {"login_url": qr["login_url"], "timeout": qr["timeout"]}

    @app.get("/api/login/qr.png")
    def login_qr_png():
        return Response(login["png"], media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/login/status")
    def login_status():
        return {"status": login["status"], "message": login["message"]}

    @app.post("/api/logout")
    def logout():
        hub.cloud.logout()
        hub.save_session()
        login.update(status="idle", message="")
        return state()

    # ---------------------------------------------------------------- devices
    @app.get("/api/devices")
    def devices():
        return [_public(d) for d in hub.devices()]

    @app.post("/api/devices/refresh")
    def refresh():
        return [_public(d) for d in hub.refresh_devices()]

    @app.post("/api/discover")
    def scan():
        found = hub.discover(4.0)
        return {"found": found, "devices": [_public(d) for d in hub.devices()]}

    @app.post("/api/devices/add")
    def add(body: AddBody):
        return _public(hub.add_manual(body.ip.strip(), body.token.strip(), body.name))

    @app.get("/api/devices/{did}/spec")
    def spec(did: str):
        s = hub.spec(device(did))
        return s.to_dict() if s else None

    @app.get("/api/devices/{did}/status")
    def status(did: str, via: str = "auto", keys: str = ""):
        return hub.get(device(did), [k for k in keys.split(",") if k] or None, via)

    @app.post("/api/devices/{did}/set")
    def set_prop(did: str, body: SetBody):
        return hub.set(device(did), body.key, body.value, body.via)

    @app.post("/api/devices/{did}/action")
    def action(did: str, body: ActionBody):
        return hub.action(device(did), body.key, body.args, body.via)

    @app.post("/api/devices/{did}/raw")
    def raw(did: str, body: RawBody):
        return hub.raw(device(did), body.method, body.params, body.via)

    return app
