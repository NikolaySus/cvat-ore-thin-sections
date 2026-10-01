import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(os.environ.get("DATA_DIR", "data"))


def connect():
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ROOT / "state.sqlite", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS frames (job INTEGER, frame INTEGER, revision INTEGER, state TEXT, PRIMARY KEY(job, frame))")
    return conn


def load(job, frame):
    from .engine import defaults
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT revision, state FROM frames WHERE job=? AND frame=?", (job, frame)).fetchone()
    return {"revision": row["revision"], "state": json.loads(row["state"])} if row else {"revision": 0, "state": defaults()}


def save(job, frame, state, expected):
    from fastapi import HTTPException
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision FROM frames WHERE job=? AND frame=?", (job, frame)).fetchone()
        revision = row["revision"] if row else 0
        if revision != expected:
            raise HTTPException(409, "Настройки изменены в другой сессии. Откройте кадр заново.")
        revision += 1
        conn.execute("INSERT OR REPLACE INTO frames VALUES (?, ?, ?, ?)", (job, frame, revision, json.dumps(state)))
    return {"revision": revision, "state": state}


def frame_dir(job, frame):
    path = ROOT / "frames" / str(job) / str(frame)
    path.mkdir(parents=True, exist_ok=True)
    return path
