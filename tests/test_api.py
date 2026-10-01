from io import BytesIO

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from ore_service import api, store
from ore_service.engine import defaults


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    async def cvat(request, path, params=None):
        if request.headers.get("authorization") != "Token test":
            raise HTTPException(403, "Нет доступа")
        if path.endswith("/data"):
            output = BytesIO()
            Image.new("RGB", (160, 128), "gray").save(output, format="PNG")
            return httpx.Response(200, content=output.getvalue())
        return httpx.Response(200, json={"start_frame": 0, "stop_frame": 1})
    monkeypatch.setattr(api, "cvat_get", cvat)
    return TestClient(api.app)


def test_access_and_revision_conflict(client):
    assert client.get("/ore/api/frames/1/0").status_code == 403
    headers = {"Authorization": "Token test"}
    assert client.get("/ore/api/frames/1/2", headers=headers).status_code == 422
    initial = client.get("/ore/api/frames/1/0", headers=headers).json()
    assert initial["width"] == 160
    payload = {"revision": 0, "state": defaults()}
    assert client.put("/ore/api/frames/1/0", headers=headers, json=payload).json()["revision"] == 1
    assert client.put("/ore/api/frames/1/0", headers=headers, json=payload).status_code == 409


def test_invalid_parameters_and_sketch(client):
    headers = {"Authorization": "Token test"}
    state = defaults()
    state["approach2"]["max_work_side"] = 0
    assert client.put("/ore/api/frames/1/0", headers=headers, json={"revision": 0, "state": state}).status_code == 422
    blob = BytesIO()
    Image.new("RGB", (20, 20)).save(blob, format="PNG")
    response = client.post("/ore/api/frames/1/0/sketch?revision=0", headers=headers,
                           files={"file": ("sketch.png", blob.getvalue(), "image/png")})
    assert response.status_code == 422


def test_imported_sketch_preview_requires_access(client):
    path = "/ore/api/frames/1/0/sketch"
    headers = {"Authorization": "Token test"}
    assert client.get(path).status_code == 403
    assert client.get(path, headers=headers).status_code == 404
    blob = BytesIO()
    Image.new("RGB", (160, 128), "white").save(blob, format="PNG")
    assert client.post(path + "?revision=0", headers=headers,
                       files={"file": ("sketch.png", blob.getvalue(), "image/png")}).status_code == 200
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.content == blob.getvalue()
    assert response.headers["cache-control"] == "no-store"
    assert client.delete(path + "?revision=1", headers=headers).status_code == 200
    assert client.get(path, headers=headers).status_code == 404


def test_legacy_state_and_preview_does_not_save(client, monkeypatch):
    headers = {"Authorization": "Token test"}
    path = "/ore/api/frames/1/0"
    state = defaults()
    state.pop("deletedImportedComponents")
    state["strokes"] = [[[10,20],[130,20]]]
    saved = client.put(path, headers=headers, json={"revision":0,"state":state}).json()
    assert "id" in saved["state"]["strokes"][0]
    monkeypatch.setattr(api.preview_queue, "submit", lambda *args: {"id":"test", "status":"queued"})
    payload = {"revision":1, "session":"panel", "generation":1, "state":saved["state"]}
    assert client.post(path+"/preview", json=payload).status_code == 403
    assert client.post(path+"/preview", headers=headers, json=payload).status_code == 200
    assert client.get(path, headers=headers).json()["revision"] == 1
    payload["revision"] = 0
    assert client.post(path+"/preview", headers=headers, json=payload).status_code == 409


HEADERS = {"Authorization": "Token test"}
FRAME = "/ore/api/frames/1/0"


def move(client, revision, action, **extra):
    return client.post(FRAME + "/transition", headers=HEADERS,
                       json={"revision": revision, "action": action, **extra})


def test_workflow_expert_snapshot_and_conflicts(client):
    client.get(FRAME, headers=HEADERS)
    state = defaults()
    state["strokes"] = [{"id": "expert", "points": [[10, 20], [140, 30]]}]
    client.put(FRAME, headers=HEADERS, json={"revision": 0, "state": state}).raise_for_status()
    assert move(client, 0, "handoff").status_code == 409
    assert client.post(FRAME + "/transition", json={"revision": 1, "action": "handoff"}).status_code == 403
    response = move(client, 1, "handoff")
    assert response.status_code == 200, response.text
    original = response.json()["workflow"]["expert"]
    assert original["state"] == state
    raster = client.get(FRAME + "/expert.png", headers=HEADERS).content
    assert client.get(FRAME + "/expert.png").status_code == 403
    state["strokes"] = []
    client.put(FRAME, headers=HEADERS, json={"revision": 2, "state": state}).raise_for_status()
    assert client.get(FRAME, headers=HEADERS).json()["workflow"]["expert"] == original
    assert client.get(FRAME + "/expert.png", headers=HEADERS).content == raster
    assert move(client, 3, "handoff").status_code == 409


def fake_result(client, monkeypatch):
    from types import SimpleNamespace
    current = client.get(FRAME, headers=HEADERS).json()
    item = SimpleNamespace(job=1, frame=0, revision=current["revision"], kind="segmentation", status="completed",
                           state=current["state"], sketch=str(store.frame_dir(1, 0) / "missing.png"), id="a" * 32)
    monkeypatch.setattr(api.work_queue, "get", lambda key: item)
    monkeypatch.setattr(api, "geometry", lambda *args: {"lines": [{"closed": True}], "segments": []})
    return item


def test_review_accept_reopen_and_read_only(client, monkeypatch):
    client.get(FRAME, headers=HEADERS)
    move(client, 0, "handoff").raise_for_status()
    item = fake_result(client, monkeypatch)
    review = move(client, 1, "submit", result_id=item.id)
    assert review.status_code == 200, review.text
    assert store.load(1, 0)["workflow"]["stage"] == "review"
    assert client.put(FRAME, headers=HEADERS, json={"revision": 2, "state": defaults()}).status_code == 409
    assert move(client, 1, "accept").status_code == 409
    assert move(client, 2, "reject").status_code == 422
    accepted = move(client, 2, "accept").json()
    assert accepted["workflow"]["stage"] == "accepted"
    assert accepted["workflow"]["review"] == review.json()["workflow"]["review"]
    assert client.delete(FRAME + "/sketch?revision=3", headers=HEADERS).status_code == 409
    assert move(client, 3, "reopen").json()["workflow"]["stage"] == "refinement"
    client.put(FRAME, headers=HEADERS, json={"revision": 4, "state": defaults()}).raise_for_status()


def test_review_rejection_and_resubmission(client, monkeypatch):
    client.get(FRAME, headers=HEADERS)
    move(client, 0, "handoff").raise_for_status()
    item = fake_result(client, monkeypatch)
    move(client, 1, "submit", result_id=item.id).raise_for_status()
    rejected = move(client, 2, "reject", note="Исправить границу").json()
    assert rejected["workflow"]["note"] == "Исправить границу"
    assert rejected["workflow"]["stage"] == "refinement"
    assert move(client, 3, "submit", result_id=item.id).status_code == 409
    item.revision = 3
    assert move(client, 3, "submit", result_id=item.id).status_code == 200
    assert len(store.load(1, 0)["workflow"]["events"]) == 4


def test_review_gate_masks_contours_and_import(client, monkeypatch):
    client.get(FRAME, headers=HEADERS)
    move(client, 0, "handoff").raise_for_status()
    assert move(client, 1, "submit").status_code == 422
    item = fake_result(client, monkeypatch)
    monkeypatch.setattr(api, "geometry", lambda *args: {"lines": [{"closed": False}]})
    assert move(client, 1, "submit", result_id=item.id).status_code == 422
    monkeypatch.setattr(api, "geometry", lambda *args: {"lines": []})
    assert move(client, 1, "submit", result_id=item.id).status_code == 422
    monkeypatch.setattr(api, "geometry", lambda *args: {"lines": [{"closed": True}]})
    item.job = 2
    assert move(client, 1, "submit", result_id=item.id).status_code == 409
    item.job = 1
    Image.new("RGB", (160, 128), "white").save(store.frame_dir(1, 0) / "sketch.png")
    assert move(client, 1, "submit", result_id=item.id).status_code == 409
    assert store.load(1, 0)["workflow"]["stage"] == "refinement"


def test_legacy_workflow_migration(client):
    import json
    state = defaults()
    state["strokes"] = [[[10, 10], [100, 100]]]
    with store.connect() as conn:
        conn.execute("INSERT INTO frames VALUES (1, 0, 8, ?)", (json.dumps(state),))
    current = client.get(FRAME, headers=HEADERS).json()
    assert current["revision"] == 8
    assert current["workflow"]["stage"] == "refinement"
    assert current["workflow"]["expert"] is None
    assert len(current["state"]["strokes"]) == 1


def test_handoff_preserves_full_import_and_manual_connection(client):
    client.get(FRAME, headers=HEADERS)
    blob = BytesIO()
    Image.new("RGB", (160, 128), "white").save(blob, format="PNG")
    client.post(FRAME + "/sketch?revision=0", headers=HEADERS,
                files={"file": ("sketch.png", blob.getvalue(), "image/png")}).raise_for_status()
    state = defaults()
    state["segments"] = [{"id": "join", "a": [10, 20], "b": [140, 20], "control": [75, 20],
                          "tangent": [130, 0], "aEndpointId": "a", "bEndpointId": "b", "uiRoute": "straight"}]
    client.put(FRAME, headers=HEADERS, json={"revision": 1, "state": state}).raise_for_status()
    response = move(client, 2, "handoff")
    assert response.status_code == 200, response.text
    expert = response.json()["workflow"]["expert"]
    folder = store.frame_dir(1, 0) / "snapshots" / expert["id"]
    assert (folder / "import.png").read_bytes() == blob.getvalue()
    with Image.open(folder / "expert.png") as image:
        assert image.getpixel((75, 20)) == (0, 55, 255)


def test_first_save_remains_expert_draft_without_initial_get(client):
    state = defaults()
    state["strokes"] = [{"id": "first", "points": [[10, 10], [100, 10]]}]
    response = client.put(FRAME, headers=HEADERS, json={"revision": 0, "state": state})
    assert response.status_code == 200
    assert response.json()["workflow"]["stage"] == "sketch"
