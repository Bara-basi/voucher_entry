"""Small transactional store; one shared SQLite database on the server disk."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("WORKBENCH_DB", ROOT / "data/workbench.sqlite3"))
TZ = timezone(timedelta(hours=8))


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@contextmanager
def connect(write=False):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS objects (
        kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(kind,id))""")
    if write:
        db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def get(db, kind, key):
    row = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", (kind, key)).fetchone()
    return json.loads(row["body"]) if row else None


def put(db, kind, key, body):
    db.execute("INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
               (kind, key, dumps(body)))


def all_items(db, kind):
    return [json.loads(r["body"]) for r in db.execute(
        "SELECT body FROM objects WHERE kind=? ORDER BY rowid DESC", (kind,))]


def audit(db, entity, session, action, before, after):
    import uuid
    key = uuid.uuid4().hex
    put(db, "audit", key, {"id": key, "entity": entity, "session": session[:100],
                         "action": action, "at": now(), "before": before, "after": after})
