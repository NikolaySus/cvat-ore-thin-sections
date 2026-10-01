"""Stable sketch sources and connected, erasable contour groups."""
import copy
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from . import segmentation as seg


def normalize_state(state):
    state = copy.deepcopy(state)
    state.setdefault("deletedImportedComponents", [])
    if isinstance(state.get("strokes"), list):
        state["strokes"] = [
            {"id": "legacy-" + hashlib.sha256(json.dumps([i, stroke]).encode()).hexdigest()[:24], "points": stroke}
            if isinstance(stroke, list) else stroke
            for i, stroke in enumerate(state["strokes"])
        ]
    return state


def import_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path and Path(path).exists() else ""


def compose(size, state, path, with_sources=False):
    width, height = size
    image = Image.new("RGB", size, "white")
    sources = []
    if path and Path(path).exists():
        with Image.open(path) as original:
            mask = seg._blue_line_mask(np.asarray(original.convert("RGB")))
        if mask.shape != (height, width):
            raise ValueError("Sketch size does not match frame")
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        digest = import_digest(path)
        pixels = np.asarray(image).copy()
        deleted = set(state.get("deletedImportedComponents", []))
        for i in range(1, count):
            x, y, w, h = map(int, stats[i, :4])
            component = labels[y:y+h, x:x+w] == i
            key = "import-" + hashlib.sha256(
                digest.encode() + str((x, y, w, h)).encode() + component.tobytes()).hexdigest()[:32]
            if key in deleted:
                continue
            pixels[y:y+h, x:x+w][component] = (0, 55, 255)
            if with_sources:
                sources.append((key, (x, y), component))
        image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    line_width = max(3, round(max(size) / 300))
    for stroke in state.get("strokes", []):
        points = [tuple(p) for p in stroke["points"]]
        draw.line(points, fill=(0, 55, 255), width=line_width)
        if with_sources:
            xs, ys = zip(*points)
            x, y = max(0, int(min(xs))-line_width), max(0, int(min(ys))-line_width)
            right, bottom = min(width, int(max(xs))+line_width+1), min(height, int(max(ys))+line_width+1)
            if right > x and bottom > y:
                tile = Image.new("1", (right-x, bottom-y))
                ImageDraw.Draw(tile).line([(px-x, py-y) for px, py in points], fill=1, width=line_width)
                sources.append(("stroke-" + stroke["id"], (x, y), np.asarray(tile)))
    return image, sources


def geometry(size, state, path):
    state = normalize_state(state)
    image, sources = compose(size, state, path, with_sources=True)
    # The original algorithm only uses source.size, not the source's pixels.
    result = seg.build_sketch_geometry(image, image, seg.SketchSettings(**state["sketch"]),
                                      state["segments"], state["regionMode"])
    _, labels = cv2.connectedComponents(seg._blue_line_mask(np.asarray(image)).astype(np.uint8), 8)
    owners = {}
    for key, (x, y), mask in sources:
        for label in np.unique(labels[y:y+mask.shape[0], x:x+mask.shape[1]][mask]):
            if label:
                owners.setdefault(f"c{label}", set()).add(key)
    for line in result["lines"]:
        line["sourceRefs"] = sorted(owners.get(line["id"], []))

    # Include both drawn intersections and manually connected paths in topology.
    _, connected = cv2.connectedComponents(seg._closed_geometry_line_mask(result), 8)
    groups = {}
    for kind, objects in (("line", result["lines"]), ("segment", result["segments"])):
        for item in objects:
            points = [p for path in item["paths"] for p in seg._svg_path_points(path)] if kind == "line" else item["points"]
            if not points:
                continue
            x, y = points[0]
            label = int(connected[min(size[1]-1, max(0, round(y))), min(size[0]-1, max(0, round(x)))])
            group = groups.setdefault(label, {"lineIds": [], "segmentIds": [], "sourceRefs": set()})
            group["lineIds" if kind == "line" else "segmentIds"].append(item["id"])
            if kind == "line":
                group["sourceRefs"].update(item["sourceRefs"])
    result["contours"] = []
    for group in groups.values():
        group["sourceRefs"] = sorted(group["sourceRefs"])
        group["id"] = hashlib.sha256(json.dumps([group["sourceRefs"], sorted(group["segmentIds"])]).encode()).hexdigest()[:24]
        for item in [*result["lines"], *result["segments"]]:
            if item["id"] in group["lineIds"] or item["id"] in group["segmentIds"]:
                item["contourId"] = group["id"]
        result["contours"].append(group)
    result["processedStrokeIds"] = [s["id"] for s in state["strokes"]]
    return result
