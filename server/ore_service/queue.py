from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from .store import ROOT


def run_worker(image, sketch, state, output, kind):
    try:
        from .engine import execute
        execute(image, sketch, state, output, kind)
    except Exception as exc:
        Path(output).mkdir(parents=True, exist_ok=True)
        (Path(output) / "error.json").write_text(json.dumps({"error": str(exc)}))
        raise


@dataclass
class Work:
    job: int
    frame: int
    revision: int
    kind: str
    image: str
    sketch: str
    state: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "queued"
    error: str | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)

    @property
    def output(self):
        return ROOT / "results" / self.id


class WorkQueue:
    def __init__(self):
        self.items: dict[str, Work] = {}
        self.pending = queue.Queue(maxsize=8)
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def submit(self, item):
        with self.lock:
            self.items[item.id] = item
            try:
                self.pending.put_nowait(item)
            except queue.Full:
                del self.items[item.id]
                raise HTTPException(429, "Очередь заполнена. Повторите позже.")
            self.persist(item)
        return item

    def persist(self, item):
        item.output.mkdir(parents=True, exist_ok=True)
        keys = ("job", "frame", "revision", "kind", "image", "sketch", "state", "id", "status", "error", "created", "started", "finished")
        temp = item.output / f"meta-{uuid.uuid4().hex}.tmp"
        temp.write_text(json.dumps({key: getattr(item, key) for key in keys}))
        temp.replace(item.output / "meta.json")

    def get(self, key):
        with self.lock:
            if key not in self.items:
                if len(key) != 32 or any(char not in "0123456789abcdef" for char in key):
                    raise HTTPException(404, "Расчёт не найден.")
                path = ROOT / "results" / key / "meta.json"
                if not path.exists():
                    raise HTTPException(404, "Расчёт не найден.")
                item = Work(**json.loads(path.read_text()))
                if item.status in {"queued", "running"}:
                    item.status = "failed"
                    item.error = "Расчёт прерван перезапуском сервиса. Запустите его заново."
                    item.finished = time.time()
                    self.persist(item)
                self.items[key] = item
            return self.items[key]

    def describe(self, item):
        with self.lock:
            ahead = sum(other.status in {"queued", "running"} and other.created < item.created
                        for other in self.items.values())
        return {"id": item.id, "job": item.job, "frame": item.frame, "revision": item.revision,
                "kind": item.kind, "status": item.status, "error": item.error,
                "position": ahead + 1 if item.status == "queued" else 0,
                "elapsed": round((item.finished or time.time()) - (item.started or item.created), 1)}

    def _loop(self):
        context = mp.get_context("spawn")
        while True:
            item = self.pending.get()
            process = None
            try:
                if item.cancelled.is_set():
                    item.status = "cancelled"
                    continue
                item.started = time.time()
                item.status = "running"
                self.persist(item)
                item.output.mkdir(parents=True, exist_ok=True)
                process = context.Process(target=run_worker, args=(item.image, item.sketch, item.state,
                                                                  str(item.output), item.kind))
                process.start()
                deadline = time.monotonic() + int(os.environ.get("JOB_TIMEOUT", "900"))
                while process.is_alive():
                    if item.cancelled.is_set() or time.monotonic() > deadline:
                        process.terminate()
                        item.status = "cancelled" if item.cancelled.is_set() else "failed"
                        if item.status == "failed":
                            item.error = "Превышено время расчёта. Уменьшите рабочий размер изображения."
                        break
                    process.join(.2)
                process.join(5)
                if item.status == "running":
                    if process.exitcode == 0:
                        item.status = "completed"
                    else:
                        item.status = "failed"
                        error_file = item.output / "error.json"
                        item.error = json.loads(error_file.read_text())["error"] if error_file.exists() else "Ошибка вычислительного процесса."
            except Exception as exc:
                item.status, item.error = "failed", str(exc)
            finally:
                if process and process.is_alive():
                    process.kill()
                    process.join()
                item.finished = time.time()
                self.persist(item)
                self.pending.task_done()
