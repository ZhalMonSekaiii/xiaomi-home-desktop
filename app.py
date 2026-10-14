"""Entry point for the packaged desktop app (PyInstaller) - same as `python -m xhome app`."""
import sys

from xhome.desktop import main

sys.exit(main())
