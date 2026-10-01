import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(os.environ.get("DATA_DIR", "data"))


def connect():
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ROOT / "state.sqlite", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS frames (job INTEGER, frame INTEGER, revision INTEGER, state TEXT, PRIMARY KEY(job, frame))")
    conn.execute("CREATE TABLE IF NOT EXISTS workflows (job INTEGER, frame INTEGER, document TEXT, PRIMARY KEY(job, frame))")
    return conn


def workflow(conn, job, frame, state):
    row = conn.execute("SELECT document FROM workflows WHERE job=? AND frame=?", (job, frame)).fetchone()
    if row:
        return json.loads(row["document"])
    populated = bool(state.get("strokes") or state.get("segments") or (frame_dir(job, frame) / "sketch.png").exists())
    document = {"stage": "refinement" if populated else "sketch", "expert": None, "review": None, "note": "", "events": []}
    conn.execute("INSERT OR IGNORE INTO workflows VALUES (?, ?, ?)", (job, frame, json.dumps(document)))
    return json.loads(conn.execute("SELECT document FROM workflows WHERE job=? AND frame=?", (job, frame)).fetchone()["document"])


def load(job, frame):
    from .engine import defaults
    from .sketch import normalize_state
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT revision, state FROM frames WHERE job=? AND frame=?", (job, frame)).fetchone()
        state = normalize_state(json.loads(row["state"])) if row else defaults()
        document = workflow(conn, job, frame, state)
    return {"revision": row["revision"] if row else 0, "state": state, "workflow": document}


def save(job, frame, state, expected):
    from fastapi import HTTPException
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision,state FROM frames WHERE job=? AND frame=?", (job, frame)).fetchone()
        revision = row["revision"] if row else 0
        if revision != expected:
            raise HTTPException(409, "Настройки изменены в другой сессии. Откройте кадр заново.")
        document = workflow(conn, job, frame, json.loads(row["state"]) if row else {})
        if document["stage"] in ("review", "accepted"):
            raise HTTPException(409, "Сначала верните кадр на доработку.")
        revision += 1
        conn.execute("INSERT OR REPLACE INTO frames VALUES (?, ?, ?, ?)", (job, frame, revision, json.dumps(state)))
    return {"revision": revision, "state": state, "workflow": document}


def transition(job, frame, expected, action, actor, note="", snapshot=None):
    from fastapi import HTTPException
    from .engine import defaults
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision,state FROM frames WHERE job=? AND frame=?", (job, frame)).fetchone()
        revision = row["revision"] if row else 0
        state = json.loads(row["state"]) if row else defaults()
        if revision != expected:
            raise HTTPException(409, "Кадр изменён в другой сессии. Откройте его заново.")
        document = workflow(conn, job, frame, state)
        allowed = {"handoff": ("sketch", "refinement"), "submit": ("refinement", "review"),
                   "accept": ("review", "accepted"), "reject": ("review", "refinement"),
                   "reopen": ("accepted", "refinement")}
        if action not in allowed or document["stage"] != allowed[action][0]:
            raise HTTPException(409, "Этот переход недоступен на текущем этапе.")
        if action == "reject" and not note.strip():
            raise HTTPException(422, "Укажите замечание.")
        if action == "handoff":
            document["expert"] = snapshot
        if action == "submit":
            document["review"] = snapshot
        if action in ("reject", "reopen"):
            document["note"] = note.strip()
        document["stage"] = allowed[action][1]
        document["events"].append({"action": action, "actor": actor, "time": time.time(), "note": note.strip(), "revision": revision + 1})
        conn.execute("INSERT OR REPLACE INTO frames VALUES (?, ?, ?, ?)", (job, frame, revision + 1, json.dumps(state)))
        conn.execute("UPDATE workflows SET document=? WHERE job=? AND frame=?", (json.dumps(document), job, frame))
    return {"revision": revision + 1, "state": state, "workflow": document}


def frame_dir(job, frame):
    path = ROOT / "frames" / str(job) / str(frame)
    path.mkdir(parents=True, exist_ok=True)
    return path
