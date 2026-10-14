"""Dashboard demo against the fake bulb (no Xiaomi account needed): python tests/demo_server.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import uvicorn  # noqa: E402
from fake_miio import AC_MODEL, TOKEN, FakeDevice  # noqa: E402

from xhome.hub import XHome  # noqa: E402
from xhome.web import create_app  # noqa: E402

AC = "--ac" in sys.argv  # simulate the Mijia AC instead of a Yeelight bulb
FakeDevice(model=AC_MODEL if AC else "yeelink.light.color1").start()
hub = XHome(tempfile.mkdtemp(prefix="xhome-demo-"))
hub.add_manual("127.0.0.1", TOKEN, "AC Demo (simulator)" if AC else "Demo Bulb (simulator)")
print("demo dashboard: http://127.0.0.1:8766")
uvicorn.run(create_app(hub), host="127.0.0.1", port=8766, log_level="warning")
