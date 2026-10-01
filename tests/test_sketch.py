import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

from ore_service.engine import defaults
from ore_service.sketch import geometry, normalize_state
from ore_service import store
from ore_service.preview import PreviewQueue


def stroke(key, points):
    return {"id": key, "points": points}


def erase(state, result, contour):
    refs = set(contour["sourceRefs"])
    state["strokes"] = [s for s in state["strokes"] if "stroke-"+s["id"] not in refs]
    state["deletedImportedComponents"] += [r for r in refs if r.startswith("import-")]
    state["segments"] = [s for s in state["segments"] if s["id"] not in contour["segmentIds"]]


def test_legacy_ids_and_normalization():
    state = defaults()
    state.pop("deletedImportedComponents")
    state["strokes"] = [[[10, 10], [100, 10]]]
    new = normalize_state(state)
    assert new == normalize_state(state) == normalize_state(new)
    assert isinstance(state["strokes"][0], list)


def test_open_closed_nested_and_intersecting_groups():
    state = defaults()
    state["strokes"] = [stroke("outer", [[20,20],[230,20],[230,230],[20,230],[20,20]]),
                        stroke("inner", [[60,60],[190,60],[190,190],[60,190],[60,60]]),
                        stroke("line", [[80,100],[160,100]])]
    result = geometry((256,256), state, None)
    assert len(result["contours"]) == 3
    selected = next(c for c in result["contours"] if "stroke-inner" in c["sourceRefs"])
    erase(state, result, selected)
    after = geometry((256,256), state, None)
    assert {r for c in after["contours"] for r in c["sourceRefs"]} == {"stroke-outer", "stroke-line"}
    state["strokes"].append(stroke("cross", [[120,80],[120,130]]))
    after = geometry((256,256), state, None)
    assert any(set(c["sourceRefs"]) == {"stroke-line", "stroke-cross"} for c in after["contours"])


def test_import_delete_undo_and_redraw(tmp_path):
    path = tmp_path / "sketch.png"
    image = Image.new("RGB", (256,256), "white")
    draw = ImageDraw.Draw(image)
    draw.line([(30,50),(200,50)], fill=(0,55,255), width=5)
    draw.line([(30,160),(200,160)], fill=(0,55,255), width=5)
    image.save(path)
    original = path.read_bytes()
    state = defaults()
    result = geometry(image.size, state, path)
    assert len(result["contours"]) == 2
    erase(state, result, result["contours"][0])
    assert len(geometry(image.size, state, path)["lines"]) == 1
    state["strokes"].append(stroke("new", [[30,50],[200,50]]))
    assert len(geometry(image.size, state, path)["lines"]) == 2
    state["deletedImportedComponents"] = []
    assert len(geometry(image.size, state, path)["lines"]) == 2
    assert path.read_bytes() == original


def test_manually_connected_group():
    state = defaults()
    state["strokes"] = [stroke("a", [[20,40],[100,40]]), stroke("b", [[130,40],[220,40]]),
                        stroke("other", [[20,150],[220,150]])]
    result = geometry((256,256), state, None)
    endpoints = sorted([e for e in result["allEndpoints"] if e["y"] < 100], key=lambda e:e["x"])
    a,b = endpoints[1:3]
    state["segments"] = [{"id":"link", "routeType":"curve", "aEndpointId":a["id"], "bEndpointId":b["id"],
                          "a":[a["x"],a["y"]], "b":[b["x"],b["y"]],
                          "control":[(a["x"]+b["x"])/2,40], "tangent":[b["x"]-a["x"],0]}]
    result = geometry((256,256), state, None)
    contour = next(c for c in result["contours"] if "link" in c["segmentIds"])
    assert set(contour["sourceRefs"]) == {"stroke-a", "stroke-b"}
    erase(state, result, contour)
    assert not state["segments"]
    assert len(geometry((256,256), state, None)["lines"]) == 1


def test_mixed_imported_drawn_group_and_persistence(tmp_path, monkeypatch):
    path = tmp_path / "sketch.png"
    image = Image.new("RGB", (256,256), "white")
    ImageDraw.Draw(image).line([(20,80),(220,80)], fill=(0,55,255), width=5)
    image.save(path)
    state = defaults()
    state["strokes"] = [stroke("cross", [[120,30],[120,150]]), stroke("separate", [[20,210],[220,210]])]
    result = geometry(image.size, state, path)
    contour = next(c for c in result["contours"] if "stroke-cross" in c["sourceRefs"])
    assert len(contour["sourceRefs"]) == 2
    erase(state, result, contour)
    monkeypatch.setattr(store, "ROOT", tmp_path / "store")
    store.save(1, 0, state, 0)
    loaded = store.load(1, 0)["state"]
    assert len(geometry(image.size, loaded, path)["lines"]) == 1
    assert loaded["strokes"][0]["id"] == "separate"


def test_preview_queue_latest_only_and_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    monkeypatch.setattr("ore_service.preview.threading.Thread.start", lambda _: None)
    queue = PreviewQueue()
    args = ("panel", 1, 0, 0)
    one = queue.submit(*args, 1, tmp_path/"image", tmp_path/"none", defaults())
    two = queue.submit(*args, 2, tmp_path/"image", tmp_path/"none", defaults())
    assert len(queue.pending) == 1
    assert queue.get(one["id"])["status"] == "cancelled"
    with pytest.raises(HTTPException) as error:
        queue.submit(*args, 1, tmp_path/"image", tmp_path/"none", defaults())
    assert error.value.status_code == 409
    queue.cancel(two["id"])
    assert not queue.pending
