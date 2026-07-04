#!/usr/bin/env python
"""
GoldenProxies production server launcher (Waitress / Windows-friendly).

Run from the repository root:

    python serve_waitress.py

Environment variables (all optional):
    HOST     interface to bind (default 0.0.0.0)
    PORT     port to listen on (default 8000)
    THREADS  number of Waitress worker threads (default 8)

Notes:
- The Django project lives in artifacts/goldenproxies-django/. This launcher
  adds it to the path and runs from that directory so db.sqlite3, the TinyDB
  snapshot, and staticfiles all resolve correctly.
- If the project virtualenv (artifacts/goldenproxies-django/.venv) exists, this
  script re-executes itself with that interpreter, so it works whether you launch
  it with the system Python or the venv Python.
- Run setup_local_db.bat first to create the venv, build the database, and
  collect static files.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DJANGO_DIR = REPO_ROOT / "artifacts" / "goldenproxies-django"

if not (DJANGO_DIR / "manage.py").exists():
    sys.exit(f"[serve_waitress] Django project not found at: {DJANGO_DIR}")

# Re-exec inside the project's virtualenv if present and not already active.
_venv_python = (
    DJANGO_DIR
    / ".venv"
    / ("Scripts" if os.name == "nt" else "bin")
    / ("python.exe" if os.name == "nt" else "python")
)
if (
    _venv_python.exists()
    and Path(sys.executable).resolve() != _venv_python.resolve()
    and not os.environ.get("_GP_VENV_REEXEC")
):
    os.environ["_GP_VENV_REEXEC"] = "1"
    os.execv(
        str(_venv_python),
        [str(_venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
    )

# Run from the Django project directory and make it importable.
os.chdir(DJANGO_DIR)
sys.path.insert(0, str(DJANGO_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "goldenproxies.settings")

try:
    from waitress import serve
except ImportError:
    sys.exit(
        "[serve_waitress] 'waitress' is not installed.\n"
        "Run setup_local_db.bat first, or install dependencies with:\n"
        "    pip install -r artifacts/goldenproxies-django/requirements.txt"
    )

from goldenproxies.wsgi import application

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
THREADS = int(os.environ.get("THREADS", "8"))

if __name__ == "__main__":
    print(f"[serve_waitress] GoldenProxies running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    serve(application, host=HOST, port=PORT, threads=THREADS)
