"""
MASYS CelebTimeline — Portable Windows EXE Launcher (`Cách 1`).

Bundles Python 3.14 + FastAPI + Uvicorn + OpenCV + yt-dlp + Pillow into `_internal/`,
while loading the application's own `.py` modules and `static/` folder dynamically from
the directory alongside `MASYS-CelebTimeline.exe` so 1-Click Auto-Update (`ver 0.1 -> 0.2`)
updates live code without rebuilding the entire EXE.
"""

import os
import sys
import time
import threading
import webbrowser
import importlib

# Explicit imports so PyInstaller collects all runtime dependencies cleanly into `_internal/`
import cv2  # noqa: F401
import numpy  # noqa: F401
import PIL  # noqa: F401
import PIL.Image  # noqa: F401
import PIL.ImageDraw  # noqa: F401
import PIL.ImageFont  # noqa: F401
import requests  # noqa: F401
import fastapi  # noqa: F401
import fastapi.staticfiles  # noqa: F401
import fastapi.responses  # noqa: F401
import starlette  # noqa: F401
import pydantic  # noqa: F401
import uvicorn  # noqa: F401
import multipart  # noqa: F401
import yt_dlp  # noqa: F401
import youtube_transcript_api  # noqa: F401


def _get_app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _is_already_running(url: str = "http://127.0.0.1:8765/api/update/version") -> bool:
    try:
        r = requests.get(url, timeout=1.5)
        return r.ok
    except Exception:
        return False


def main():
    app_root = _get_app_root()
    os.chdir(app_root)
    if app_root not in sys.path:
        sys.path.insert(0, app_root)

    no_browser = ("--no-browser" in sys.argv) or (os.environ.get("MASYS_NO_BROWSER") == "1")

    if _is_already_running():
        print("MASYS CelebTimeline dang chay san tai http://127.0.0.1:8765")
        if not no_browser:
            webbrowser.open("http://127.0.0.1:8765")
        return

    app_mod = importlib.import_module("app")
    ver_info = app_mod._get_local_version_info()
    ver_label = ver_info.get("version") or "ver 0.1"

    print("=" * 64)
    print(f"  MASYS CelebTimeline ({ver_label})")
    print("  Dang khoi dong may chu tai: http://127.0.0.1:8765")
    print("  (Giu cua so nay mo khi su dung; dong cua so de tat app)")
    print("=" * 64)

    if not no_browser:
        def _open_ui():
            time.sleep(1.3)
            webbrowser.open("http://127.0.0.1:8765")

        threading.Thread(target=_open_ui, daemon=True).start()

    uvicorn.run(app_mod.app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
