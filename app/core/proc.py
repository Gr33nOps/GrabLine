"""Subprocess helpers shared by every tool GrabLine shells out to.

On Windows a GUI app that launches a console program (FFmpeg, 7-Zip, Deno, a
virus scanner) pops a black console window unless the child is created with
CREATE_NO_WINDOW - which is what :func:`hidden` supplies. On other platforms
it is a no-op.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any


def clean_env() -> dict[str, str] | None:
    """Environment for launching a *system* program from a frozen build, with the
    app's own bundled-library path removed from ``LD_LIBRARY_PATH``.

    A PyInstaller/AppImage build prepends its bundled lib directory to
    ``LD_LIBRARY_PATH`` so the app finds its own Qt. That variable is inherited
    by every child process, and it makes system tools load the app's
    incompatible libraries and misbehave: the confirmed symptom was "Open
    folder" launching a browser or a terminal instead of the file manager,
    because ``xdg-open``'s helper (``nemo``) crashed under the bundled libs and
    xdg-open fell back to a generic handler. Strip the bundle paths so children
    use the system's own libraries.

    Returns ``None`` when not frozen (nothing to clean - inherit the environment
    unchanged), so callers can pass ``env=clean_env()`` unconditionally.
    """
    if not getattr(sys, "frozen", False):
        return None
    roots = [p for p in (getattr(sys, "_MEIPASS", None), os.environ.get("APPDIR")) if p]
    if not roots:
        return None
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG"):
        value = env.get(key)
        if not value:
            continue
        kept = [
            part
            for part in value.split(os.pathsep)
            if part and not any(part == r or part.startswith(r + os.sep) for r in roots)
        ]
        if kept:
            env[key] = os.pathsep.join(kept)
        else:
            env.pop(key, None)
    return env


def hidden() -> dict[str, Any]:
    """Keyword arguments for ``subprocess.run/Popen`` that keep the child's
    console window from appearing (Windows); empty elsewhere."""
    if sys.platform == "win32":  # pragma: no cover - Windows-only
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {
            "startupinfo": startupinfo,
            "creationflags": subprocess.CREATE_NO_WINDOW,
        }
    return {}
