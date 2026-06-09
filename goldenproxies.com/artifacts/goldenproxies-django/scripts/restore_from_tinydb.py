"""Restore all persisted tables from TinyDB into the live Django DB.
Called from start.sh after migrate."""
import os, sys, django
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "goldenproxies.settings")
django.setup()

from core import persistence  # noqa: E402  (after django.setup)
import core.signals  # noqa: F401  (registers PERSISTED_MODELS)


def main():
    if not persistence.DB_PATH.exists():
        print(f"[restore] No TinyDB at {persistence.DB_PATH}, skipping.")
        return
    counts = persistence.restore_all()
    total = sum(counts.values())
    if total == 0:
        print("[restore] TinyDB present but nothing to restore (all rows already exist).")
        return
    print(f"[restore] Restored {total} rows from TinyDB:")
    for table, n in counts.items():
        if n:
            print(f"  + {table}: {n}")


if __name__ == "__main__":
    main()
