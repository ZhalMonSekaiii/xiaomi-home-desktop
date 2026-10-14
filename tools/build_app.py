"""Build XHome.exe (PyInstaller) and optionally install it with Start Menu + Desktop shortcuts.

    python tools/build_app.py              # build only  -> dist/XHome/XHome.exe
    python tools/build_app.py --install    # build, copy to %LOCALAPPDATA%\\Programs\\XHome, add shortcuts
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "XHome"
INSTALL_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "XHome"


def _rmtree(path: Path) -> None:
    """rmtree that also removes read-only entries (some bundled dist-info folders are read-only)."""
    def clear_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if path.exists():
        shutil.rmtree(path, onexc=clear_readonly)


def build() -> None:
    import PyInstaller.__main__

    _rmtree(DIST)
    _rmtree(ROOT / "build")

    PyInstaller.__main__.run([
        str(ROOT / "app.py"),
        "--name", "XHome",
        "--windowed",  # no console window
        "--noconfirm", "--clean",
        "--icon", str(ROOT / "xhome" / "static" / "xhome.ico"),
        "--add-data", f"{ROOT / 'xhome' / 'static'}{os.pathsep}xhome/static",
        "--collect-submodules", "uvicorn",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ])
    print(f"built {DIST / 'XHome.exe'}")


def install() -> None:
    subprocess.run(["taskkill", "/IM", "XHome.exe", "/F"], capture_output=True)  # replace a running copy
    _rmtree(INSTALL_DIR)
    shutil.copytree(DIST, INSTALL_DIR)
    exe = INSTALL_DIR / "XHome.exe"
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) { "
        "  $s = $ws.CreateShortcut((Join-Path $dir 'XHome.lnk')); "
        f"  $s.TargetPath = '{exe}'; $s.WorkingDirectory = '{INSTALL_DIR}'; "
        f"  $s.IconLocation = '{exe},0'; $s.Description = 'Control Xiaomi Home devices (LAN + Cloud)'; $s.Save() }}"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    print(f"installed {exe} + Desktop & Start Menu shortcuts")


if __name__ == "__main__":
    build()
    if "--install" in sys.argv:
        install()
