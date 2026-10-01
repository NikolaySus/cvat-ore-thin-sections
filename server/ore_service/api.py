from __future__ import annotations

import asyncio
import json
import math
import os
import hashlib
import shutil
import uuid
from dataclasses import fields
from io import BytesIO

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel

from . import segmentation as seg
from . import store
from .engine import defaults, COLORS
from .queue import Work, WorkQueue
from .settings import BOUNDS
from .sketch import normalize_state, import_digest, geometry, compose
from .preview import PreviewQueue

app = FastAPI(title="Ore annotation CVAT service")
CVAT_URL = os.environ.get("CVAT_URL", "http://cvat-server:8080").rstrip("/")
work_queue = WorkQueue()
preview_queue = PreviewQueue()
Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "1000000000"))
download_locks: dict[tuple[int, int], asyncio.Lock] = {}
edit_locks: dict[tuple[int, int], asyncio.Lock] = {}


def edit_lock(job, frame):
    return edit_locks.setdefault((job, frame), asyncio.Lock())


def auth_headers(request):
    return {key: request.headers[key] for key in ("cookie", "authorization", "x-organization") if key in request.headers}


async def cvat_get(request, path, params=None):
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        try:
            response = await client.get(CVAT_URL + path, params=params, headers=auth_headers(request))
        except httpx.RequestError:
            raise HTTPException(503, "CVAT недоступен.")
    if response.status_code != 200:
        raise HTTPException(response.status_code if response.status_code in (401, 403, 404) else 502,
                            "Нет доступа к данным CVAT." if response.status_code in (401, 403) else "Не удалось получить данные CVAT.")
    return response


async def check_access(request, job, frame):
    data = (await cvat_get(request, f"/api/jobs/{job}")).json()
    if not data["start_frame"] <= frame <= data["stop_frame"]:
        raise HTTPException(422, "Кадр вне задания.")
    return data


async def source(request, job, frame):
    await check_access(request, job, frame)
    folder = store.frame_dir(job, frame)
    path = folder / "source"
    lock = download_locks.setdefault((job, frame), asyncio.Lock())
    async with lock:
        if not path.exists():
            response = await cvat_get(request, f"/api/jobs/{job}/data", {"type": "frame", "number": frame, "quality": "original"})
            try:
                with Image.open(BytesIO(response.content)) as image:
                    image.verify()
            except Exception:
                raise HTTPException(422, "CVAT вернул неподдерживаемое изображение.")
            temp = folder / "source.tmp"
            temp.write_bytes(response.content)
            temp.replace(path)
    return path


class FrameState(BaseModel):
    revision: int
    state: dict


class PreviewState(FrameState):
    session: str
    generation: int


class TransitionState(BaseModel):
    revision: int
    action: str
    note: str = ""
    result_id: str | None = None


def validate_state(state):
    state = normalize_state(state)
    template = defaults()
    if set(state) != set(template):
        raise HTTPException(422, "Неполные или неизвестные настройки.")
    if state["algorithm"] not in ("approach1", "approach2") or state["regionMode"] not in ("inside", "outside"):
        raise HTTPException(422, "Неизвестный режим.")
    if not isinstance(state["corrected"], bool):
        raise HTTPException(422, "Некорректный режим коррекции.")
    for name, settings_type in (("approach1", seg.Approach1Settings), ("approach2", seg.Approach2Settings),
                                ("sketch", seg.SketchSettings), ("correction", seg.CorrectionSettings)):
        values = state[name]
        if not isinstance(values, dict) or set(values) != {field.name for field in fields(settings_type)}:
            raise HTTPException(422, f"Некорректные настройки {name}.")
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise HTTPException(422, f"Некорректное число: {key}.")
            if not BOUNDS[key][0] <= value <= BOUNDS[key][1]:
                raise HTTPException(422, f"Значение вне диапазона: {key}.")
            if isinstance(getattr(settings_type(), key), int) and value != int(value):
                raise HTTPException(422, f"Требуется целое число: {key}.")
            if isinstance(getattr(settings_type(), key), int):
                values[key] = int(value)
    if not isinstance(state["strokes"], list) or not isinstance(state["segments"], list):
        raise HTTPException(422, "Некорректный эскиз.")
    if len(json.dumps(state)) > 2_000_000:
        raise HTTPException(413, "Слишком большой эскиз.")
    for stroke in state["strokes"]:
        if not isinstance(stroke, dict) or set(stroke) != {"id", "points"} or not isinstance(stroke["id"], str) or not 1 <= len(stroke["id"]) <= 128:
            raise HTTPException(422, "Некорректный штрих.")
        if not isinstance(stroke["points"], list) or len(stroke["points"]) < 2:
            raise HTTPException(422, "Линия должна содержать хотя бы две точки.")
        validate_points(stroke["points"])
    if len({s["id"] for s in state["strokes"]}) != len(state["strokes"]):
        raise HTTPException(422, "Повторяющиеся ID штрихов.")
    deleted = state["deletedImportedComponents"]
    if not isinstance(deleted, list) or any(not isinstance(v, str) or not v.startswith("import-") or len(v) != 39 for v in deleted):
        raise HTTPException(422, "Некорректные удалённые компоненты.")
    for segment in state["segments"]:
        if not isinstance(segment, dict) or any(key not in segment for key in ("id", "a", "b", "control", "tangent", "aEndpointId", "bEndpointId")):
            raise HTTPException(422, "Некорректное соединение.")
        validate_points([segment[key] for key in ("a", "b", "control", "tangent")])
    return state


def validate_points(points):
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1_000_000 for v in point):
            raise HTTPException(422, "Некорректные координаты.")


@app.get("/ore/api/health")
def health():
    return {"status": "ok", "classes": list(COLORS), "defaults": defaults(), "bounds": BOUNDS}


@app.get("/ore/api/frames/{job}/{frame}")
async def get_frame(request: Request, job: int, frame: int):
    path = await source(request, job, frame)
    with Image.open(path) as image:
        size = image.size
    return {**store.load(job, frame), "width": size[0], "height": size[1],
            "hasSketch": (path.parent / "sketch.png").exists(), "importDigest": import_digest(path.parent / "sketch.png"), "bounds": BOUNDS}


@app.put("/ore/api/frames/{job}/{frame}")
async def put_frame(request: Request, job: int, frame: int, payload: FrameState):
    await check_access(request, job, frame)
    async with edit_lock(job, frame):
        return store.save(job, frame, validate_state(payload.state), payload.revision)


@app.post("/ore/api/frames/{job}/{frame}/transition")
async def transition_frame(request: Request, job: int, frame: int, payload: TransitionState):
    path = await source(request, job, frame)
    user = (await cvat_get(request, "/api/users/self")).json()
    actor = {"id": user.get("id"), "username": user.get("username", "unknown")}
    if len(payload.note) > 4000:
        raise HTTPException(422, "Замечание слишком длинное.")
    async with edit_lock(job, frame):
        current = store.load(job, frame)
        if current["revision"] != payload.revision:
            raise HTTPException(409, "Кадр изменён в другой сессии. Откройте его заново.")
        snapshot = None
        folder = None
        try:
            if payload.action in ("handoff", "submit"):
                required = "sketch" if payload.action == "handoff" else "refinement"
                if current["workflow"]["stage"] != required:
                    raise HTTPException(409, "Этот переход недоступен на текущем этапе.")
                with Image.open(path) as image:
                    size = image.size
                state = current["state"]
                sketch = path.parent / "sketch.png"
                snapshot = {"id": uuid.uuid4().hex, "revision": payload.revision, "state": state, "actor": actor}
                if payload.action == "submit":
                    if not payload.result_id:
                        raise HTTPException(422, "Сначала рассчитайте маски.")
                    item = work_queue.get(payload.result_id)
                    if (item.job, item.frame, item.revision, item.kind, item.status) != (job, frame, payload.revision, "segmentation", "completed") or item.state != state:
                        raise HTTPException(409, "Маски не соответствуют текущей версии.")
                    if import_digest(item.sketch) != import_digest(sketch):
                        raise HTTPException(409, "Эскиз изменился после расчёта.")
                    contours = await asyncio.to_thread(geometry, size, state, sketch)
                    if not contours["lines"] or any(not line["closed"] for line in contours["lines"]):
                        raise HTTPException(422, "Замкните все контуры перед передачей на проверку.")
                    snapshot.update({"geometry": contours, "result_id": item.id})
                folder = path.parent / "snapshots" / snapshot["id"]
                folder.mkdir(parents=True)
                if payload.action == "handoff":
                    if sketch.exists():
                        shutil.copyfile(sketch, folder / "import.png")
                    # Preserve the expert's raw raster and confirmed manual connections, not cleaned contours.
                    def capture():
                        raw = compose(size, state, sketch)[0]
                        from . import segmentation as segmentation
                        from PIL import ImageDraw
                        drawer = ImageDraw.Draw(raw)
                        for segment in state["segments"]:
                            points = (segmentation._perimeter_route_points(segment["a"], segment["b"], *size)
                                      if segment.get("uiRoute") == "perimeter" else
                                      segmentation._cubic_curve_points(segment["a"], segment["control"], segment["tangent"], segment["b"], *size))
                            drawer.line([tuple(p) for p in points], fill=(0, 55, 255), width=max(3, round(max(size) / 300)))
                        raw.thumbnail((2000, 2000))
                        raw.save(folder / "expert.png")
                    await asyncio.to_thread(capture)
                (folder / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
            return store.transition(job, frame, payload.revision, payload.action, actor, payload.note, snapshot)
        except Exception:
            if folder:
                shutil.rmtree(folder, ignore_errors=True)
            raise


@app.get("/ore/api/frames/{job}/{frame}/expert.png")
async def expert_asset(request: Request, job: int, frame: int):
    await check_access(request, job, frame)
    expert = store.load(job, frame)["workflow"]["expert"]
    if not expert:
        raise HTTPException(404, "Исходные наметки отсутствуют.")
    return FileResponse(store.frame_dir(job, frame) / "snapshots" / expert["id"] / "expert.png", headers={"Cache-Control": "no-store"})


@app.get("/ore/api/frames/{job}/{frame}/image")
async def get_image(request: Request, job: int, frame: int):
    path = await source(request, job, frame)
    preview = path.parent / "preview.jpg"
    if not preview.exists():
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((2000, 2000))
            image.save(preview)
    return FileResponse(preview)


@app.get("/ore/api/frames/{job}/{frame}/sketch")
async def get_sketch(request: Request, job: int, frame: int):
    await check_access(request, job, frame)
    path = store.frame_dir(job, frame) / "sketch.png"
    if not path.exists():
        raise HTTPException(404, "Импортированный эскиз отсутствует.")
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.post("/ore/api/frames/{job}/{frame}/sketch")
async def upload_sketch(request: Request, job: int, frame: int, revision: int, file: UploadFile = File(...)):
    path = await source(request, job, frame)
    data = await file.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise HTTPException(413, "Эскиз больше 64 МБ.")
    try:
        sketch = Image.open(BytesIO(data))
        with Image.open(path) as image:
            if sketch.size != image.size:
                raise HTTPException(422, "Размер эскиза должен совпадать с кадром.")
        sketch = sketch.convert("RGB")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Не удалось прочитать эскиз.")
    async with edit_lock(job, frame):
        current = store.load(job, frame)
        current["state"]["segments"] = []
        current["state"]["strokes"] = []
        current["state"]["deletedImportedComponents"] = []
        saved = store.save(job, frame, current["state"], revision)
        sketch.save(path.parent / "sketch.png")
        return {**saved, "importDigest": import_digest(path.parent / "sketch.png")}


@app.post("/ore/api/frames/{job}/{frame}/jobs")
async def start_job(request: Request, job: int, frame: int, revision: int, kind: str = "segmentation"):
    if kind not in ("segmentation", "geometry"):
        raise HTTPException(422, "Неизвестный тип расчёта.")
    path = await source(request, job, frame)
    async with edit_lock(job, frame):
        current = store.load(job, frame)
        if current["revision"] != revision:
            raise HTTPException(409, "Настройки изменились. Откройте кадр заново.")
        item = Work(job, frame, revision, kind, str(path), "", current["state"])
        item.output.mkdir(parents=True)
        frozen = item.output / "sketch.png"
        if (path.parent / "sketch.png").exists():
            shutil.copyfile(path.parent / "sketch.png", frozen)
        item.sketch = str(frozen)
        return work_queue.describe(work_queue.submit(item))


@app.delete("/ore/api/frames/{job}/{frame}/sketch")
async def clear_sketch(request: Request, job: int, frame: int, revision: int):
    await check_access(request, job, frame)
    async with edit_lock(job, frame):
        current = store.load(job, frame)
        current["state"]["segments"] = []
        current["state"]["strokes"] = []
        current["state"]["deletedImportedComponents"] = []
        saved = store.save(job, frame, current["state"], revision)
        (store.frame_dir(job, frame) / "sketch.png").unlink(missing_ok=True)
        return saved


@app.post("/ore/api/frames/{job}/{frame}/preview")
async def start_preview(request: Request, job: int, frame: int, payload: PreviewState):
    path = await source(request, job, frame)
    if not 1 <= len(payload.session) <= 128 or payload.generation < 0:
        raise HTTPException(422, "Некорректная сессия предпросмотра.")
    if store.load(job, frame)["revision"] != payload.revision:
        raise HTTPException(409, "Настройки изменились в другой сессии. Откройте кадр заново.")
    owner = hashlib.sha256(json.dumps(auth_headers(request), sort_keys=True).encode()).hexdigest()
    return preview_queue.submit((owner, job, frame, payload.session), job, frame, payload.revision,
                                payload.generation, path, path.parent / "sketch.png", validate_state(payload.state))


@app.get("/ore/api/previews/{key}")
async def get_preview(request: Request, key: str):
    item = preview_queue.get(key)
    await check_access(request, item["job"], item["frame"])
    return preview_queue.describe(item)


@app.delete("/ore/api/previews/{key}")
async def cancel_preview(request: Request, key: str):
    item = preview_queue.get(key)
    await check_access(request, item["job"], item["frame"])
    preview_queue.cancel(key)
    return {"status": "cancelled"}


async def accessible_work(request, key):
    item = work_queue.get(key)
    await check_access(request, item.job, item.frame)
    return item


@app.get("/ore/api/jobs/{key}")
async def job_status(request: Request, key: str):
    return work_queue.describe(await accessible_work(request, key))


@app.delete("/ore/api/jobs/{key}")
async def cancel_job(request: Request, key: str):
    item = await accessible_work(request, key)
    item.cancelled.set()
    return {"status": "cancellation_requested"}


@app.get("/ore/api/jobs/{key}/result")
async def job_result(request: Request, key: str):
    item = await accessible_work(request, key)
    if item.status != "completed":
        raise HTTPException(409, "Расчёт ещё не завершён.")
    return {"revision": item.revision, "result": json.loads((item.output / "result.json").read_text())}


@app.get("/ore/api/jobs/{key}/{asset}")
async def job_asset(request: Request, key: str, asset: str):
    item = await accessible_work(request, key)
    if asset not in ("overlay.png", "mask.png") or item.status != "completed" or not (item.output / asset).exists():
        raise HTTPException(404)
    return FileResponse(item.output / asset)
