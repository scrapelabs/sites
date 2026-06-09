"""Django AppConfig for the ``core`` app.

Intentionally a no-op ``ready()`` — earlier versions of this hook ran
``sweep_orphan_runs()`` synchronously at gunicorn boot, which on
production correlated with worker boot hangs (every gunicorn worker
racing on ``_ensure_scrapers_table()`` DDL plus a heavy
``scraper_accela`` import chain). The orphan-detection logic still
exists and runs lazily on the Stop / Force-stop endpoints, which is
where it actually matters for the user-visible bug.

If we ever need a background sweep again, run it from a one-shot
management command or a cron job — never inline in ``ready()``.
"""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'
