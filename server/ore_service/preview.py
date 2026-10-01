"""One warm geometry process, with latest-only pending work per panel."""
import multiprocessing as mp
import os
import shutil
import threading
import time
import uuid
from collections import OrderedDict

from fastapi import HTTPException

from . import store
from .sketch import import_digest


def worker(pipe):
    from PIL import Image
    from .sketch import geometry
    Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "1000000000"))
    while True:
        image, sketch, state = pipe.recv()
        try:
            with Image.open(image) as source:
                size = source.size
            pipe.send({"result": geometry(size, state, sketch)})
        except Exception as exc:
            pipe.send({"error": str(exc)})


class PreviewQueue:
    def __init__(self):
        self.condition = threading.Condition()
        self.pending = OrderedDict()
        self.items = {}
        self.active = None
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, session, job, frame, revision, generation, image, sketch, state):
        with self.condition:
            self._prune()
            if session not in self.pending and len(self.pending) >= 32:
                raise HTTPException(429, "Очередь предпросмотра заполнена.")
            newer = [v for v in self.items.values() if v["session"] == session and v["generation"] >= generation]
            if newer:
                raise HTTPException(409, "Устаревшее поколение предпросмотра.")
            old = self.pending.pop(session, None)
            if old:
                old["status"] = "cancelled"
            key = uuid.uuid4().hex
            folder = store.ROOT / "previews" / key
            folder.mkdir(parents=True, exist_ok=True)
            copied = folder / "sketch.png"
            if sketch.exists():
                shutil.copyfile(sketch, copied)
            item = {"id": key, "session": session, "job": job, "frame": frame, "revision": revision,
                    "generation": generation, "importDigest": import_digest(copied), "created": time.time(),
                    "status": "queued", "image": str(image), "sketch": str(copied), "state": state}
            self.items[key] = item
            self.pending[session] = item
            self.condition.notify()
            return self.describe(item)

    def _prune(self):
        finished = [v for v in self.items.values() if v["status"] not in ("queued", "running")]
        for item in finished:
            if time.time()-item["created"] > 300 or len(self.items) > 64:
                self.items.pop(item["id"], None)
                shutil.rmtree(store.ROOT / "previews" / item["id"], ignore_errors=True)
        for folder in (store.ROOT / "previews").glob("*"):
            if folder.is_dir() and folder.name not in self.items and time.time()-folder.stat().st_mtime > 300:
                shutil.rmtree(folder, ignore_errors=True)

    def get(self, key):
        with self.condition:
            item = self.items.get(key)
            if not item:
                raise HTTPException(404, "Предпросмотр не найден.")
            return item

    def describe(self, item):
        return {k: item[k] for k in ("id", "job", "frame", "revision", "generation", "importDigest", "status", "result", "error") if k in item}

    def cancel(self, key):
        with self.condition:
            item = self.get(key)
            item["status"] = "cancelled"
            if self.pending.get(item["session"]) is item:
                del self.pending[item["session"]]

    def _loop(self):
        process = pipe = None
        context = mp.get_context("spawn")
        while True:
            with self.condition:
                self.condition.wait_for(lambda: bool(self.pending))
                _, item = self.pending.popitem(last=False)
                item["status"] = "running"
                self.active = item
            try:
                if process is None or not process.is_alive():
                    if pipe:
                        pipe.close()
                    pipe, child = context.Pipe()
                    process = context.Process(target=worker, args=(child,), daemon=True)
                    process.start()
                    child.close()
                pipe.send((item["image"], item["sketch"], item["state"]))
                deadline = time.monotonic()+900
                while not pipe.poll(.1):
                    if item["status"] == "cancelled" or time.monotonic() > deadline or not process.is_alive():
                        raise RuntimeError("Расчёт контуров прерван.")
                reply = pipe.recv()
                with self.condition:
                    if item["status"] != "cancelled":
                        item.update(reply)
                        item["status"] = "failed" if "error" in reply else "completed"
            except Exception as exc:
                if process:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)
                    if process.is_alive():
                        process.kill()
                        process.join()
                process = None
                with self.condition:
                    if item["status"] != "cancelled":
                        item.update(status="failed", error=str(exc))
            finally:
                with self.condition:
                    self.active = None
                    self._prune()
