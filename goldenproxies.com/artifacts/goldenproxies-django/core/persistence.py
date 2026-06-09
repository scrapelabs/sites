"""TinyDB-backed persistence layer.

Mirrors every important Django model into a TinyDB JSON file
(`data/persistent.json`) so data survives `db.sqlite3` wipes on workflow
restart. Mapping is 1:1 with Django table names so migrating to PostgreSQL
later is just: point Django at Postgres, run `restore_from_tinydb.py` once,
optionally drop this layer.

Layout:
- One TinyDB "table" per Django model (named after `model._meta.db_table`).
- Each row is a dict of `{field.attname: serialized_value}`.
- ForeignKeys are stored as raw `_id` ints (e.g. `user_id: 1`).
- Datetimes -> ISO 8601 strings, Decimals -> str.
"""
from __future__ import annotations

import os
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from threading import Lock

from tinydb import TinyDB
from django.db import models
from django.utils.dateparse import parse_datetime, parse_date

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "persistent.json"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_db: TinyDB | None = None
_db_lock = Lock()
_SUPPRESS = False  # set True during restore to avoid signal feedback loops


def db() -> TinyDB:
    global _db
    with _db_lock:
        if _db is None:
            _db = TinyDB(DB_PATH, indent=2, ensure_ascii=False, sort_keys=True)
    return _db


def _serialize_value(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    return val


def _deserialize_value(field, val):
    if val is None:
        return None
    if isinstance(field, models.DateTimeField):
        return parse_datetime(val) if isinstance(val, str) else val
    if isinstance(field, models.DateField):
        return parse_date(val) if isinstance(val, str) else val
    if isinstance(field, models.DecimalField):
        return Decimal(val)
    return val


def serialize_instance(obj) -> dict:
    """Convert a Django model instance to a JSON-safe dict.
    Uses `attname` so FKs are stored as `<field>_id` integers."""
    out = {}
    for f in obj._meta.fields:
        if f.many_to_many:
            continue
        out[f.attname] = _serialize_value(getattr(obj, f.attname))
    return out


# Registry: list of Django model classes whose data we mirror to TinyDB.
PERSISTED_MODELS: list[type[models.Model]] = []


def register(model: type[models.Model]):
    if model not in PERSISTED_MODELS:
        PERSISTED_MODELS.append(model)
    return model


def _table_for(model) -> str:
    return model._meta.db_table


# -------- write path (called from signal handlers) --------

def upsert_row(instance):
    """Write one Django row into TinyDB."""
    if _SUPPRESS:
        return
    from tinydb import Query
    tbl = db().table(_table_for(type(instance)))
    row = serialize_instance(instance)
    tbl.upsert(row, Query().id == row.get("id"))


def delete_row(instance):
    if _SUPPRESS:
        return
    from tinydb import Query
    tbl = db().table(_table_for(type(instance)))
    tbl.remove(Query().id == instance.pk)


# -------- bulk dump / restore --------

def dump_all() -> dict[str, int]:
    """Replace TinyDB contents with a full snapshot of every persisted model."""
    counts = {}
    for model in PERSISTED_MODELS:
        tbl = db().table(_table_for(model))
        tbl.truncate()
        rows = [serialize_instance(obj) for obj in model.objects.all()]
        if rows:
            tbl.insert_multiple(rows)
        counts[_table_for(model)] = len(rows)
    return counts


def restore_all() -> dict[str, int]:
    """Re-create rows from TinyDB into the live DB. Skips PKs that already exist."""
    global _SUPPRESS
    _SUPPRESS = True
    counts = {}
    try:
        for model in PERSISTED_MODELS:
            tname = _table_for(model)
            tbl = db().table(tname)
            rows = tbl.all()
            if not rows:
                counts[tname] = 0
                continue
            existing_pks = set(model.objects.values_list("pk", flat=True))
            field_map = {f.attname: f for f in model._meta.fields}
            to_create = []
            for d in rows:
                pk = d.get("id")
                if pk in existing_pks:
                    continue
                kwargs = {}
                for attname, raw in d.items():
                    field = field_map.get(attname)
                    if field is None:
                        continue
                    kwargs[attname] = _deserialize_value(field, raw)
                to_create.append(model(**kwargs))
            if to_create:
                model.objects.bulk_create(to_create)
                # Bump SQLite sequence so future inserts don't collide
                _bump_sequence(model)
            counts[tname] = len(to_create)
    finally:
        _SUPPRESS = False
    return counts


def _bump_sequence(model):
    """Reset the SQLite/Postgres sequence for `model` past max(id)."""
    from django.db import connection
    table = model._meta.db_table
    pk_col = model._meta.pk.column
    with connection.cursor() as cur:
        cur.execute(f"SELECT MAX({pk_col}) FROM {table}")
        max_id = cur.fetchone()[0] or 0
        if connection.vendor == "sqlite":
            # NOTE: inline values (table=safe model meta name, max_id=int) to
            # avoid Django's last_executed_query() crashing on `?` placeholders
            # when DEBUG SQL logging is on.
            max_id_int = int(max_id)
            table_safe = table.replace("'", "")
            cur.execute(
                f"UPDATE sqlite_sequence SET seq = {max_id_int} WHERE name = '{table_safe}'"
            )
            if cur.rowcount == 0:
                cur.execute(
                    f"INSERT INTO sqlite_sequence (name, seq) VALUES ('{table_safe}', {max_id_int})"
                )
        elif connection.vendor == "postgresql":
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{pk_col}'), {max_id}, true)"
            )
