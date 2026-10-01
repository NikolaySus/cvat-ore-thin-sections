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
