import locale
import os
import sys


def force_utf8_runtime() -> None:
    """
    Force UTF-8 runtime behavior for console/log output.
    Intended to reduce Korean text mojibake on Windows + Linux.
    """
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # Best-effort locale defaults (mostly effective on Unix).
    os.environ.setdefault("LANG", "C.UTF-8")
    os.environ.setdefault("LC_ALL", "C.UTF-8")

    try:
        locale.setlocale(locale.LC_CTYPE, "")
    except Exception:
        pass

    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleCP(65001)
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

