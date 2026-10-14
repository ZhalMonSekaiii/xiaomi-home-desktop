"""Desktop app: the dashboard in its own native window (WebView2), no console and no browser.

    python -m xhome app          (or the packaged XHome.exe)
"""
from __future__ import annotations

import socket
import sys
import threading
import time

from .hub import XHome


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(data_dir: str | None = None) -> int:
    hub = XHome(data_dir)
    if sys.stdout is None or sys.stderr is None:  # windowed exe: no console to write to
        log = open(hub.dir / "xhome-app.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log

    import uvicorn
    import webview

    from .web import create_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(hub), host="127.0.0.1", port=port,
                                           log_config=None, access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    webview.create_window("XHome", f"http://127.0.0.1:{port}/", width=1120, height=820,
                          min_size=(420, 560), background_color="#0E1014", text_select=True)
    # persistent profile so the UI remembers the last device / connection mode
    webview.start(private_mode=False, storage_path=str(hub.dir / "webview"))
    server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
