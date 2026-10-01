from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import segmentation as seg

COLORS = {"ore": (255, 35, 35), "matrix": (255, 235, 0), "talc": (0, 70, 255), "damage": (128, 0, 45)}
Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "1000000000"))


def defaults() -> dict:
    return {"algorithm": "approach2", "corrected": False,
            "approach1": asdict(seg.Approach1Settings()), "approach2": asdict(seg.Approach2Settings()),
            "sketch": asdict(seg.SketchSettings()), "correction": asdict(seg.CorrectionSettings()),
            "regionMode": "inside", "segments": [], "strokes": []}


def sketch_image(size: tuple[int, int], state: dict, path: Path | None) -> Image.Image:
    image = Image.open(path).convert("RGB") if path and path.exists() else Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for stroke in state.get("strokes", []):
        points = [tuple(point) for point in stroke]
        if len(points) > 1:
            draw.line(points, fill=(0, 55, 255), width=max(3, round(max(size) / 300)))
    return image


def encode_mask(mask: np.ndarray) -> list[int]:
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return []
    left, top, right, bottom = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
    flat = mask[top:bottom + 1, left:right + 1].ravel()
    changes = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    runs = np.diff(np.concatenate(([0], changes, [flat.size]))).tolist()
    if flat[0]:
        runs.insert(0, 0)
    return runs + [left, top, right, bottom]


def encode_resized_mask(mask: np.ndarray, size: tuple[int, int]) -> list[int]:
    # Emit original-size RLE one scanline at a time; never allocate a panorama-size bitmap.
    width, height = size
    source_h, source_w = mask.shape
    x_index = np.asarray(Image.fromarray(np.arange(source_w, dtype=np.int32)[None, :]).resize((width, 1), Image.Resampling.NEAREST))[0]
    y_index = np.asarray(Image.fromarray(np.arange(source_h, dtype=np.int32)[:, None]).resize((1, height), Image.Resampling.NEAREST))[:, 0]
    row_has_pixels = mask.any(axis=1)[y_index]
    if not row_has_pixels.any():
        return []
    top, bottom = np.flatnonzero(row_has_pixels)[[0, -1]].tolist()
    columns = mask.any(axis=0)[x_index]
    left, right = np.flatnonzero(columns)[[0, -1]].tolist()
    runs: list[int] = [0]
    previous = False
    cached_rows: dict[int, tuple[list[int], bool]] = {}
    for y in range(top, bottom + 1):
        source_y = int(y_index[y])
        if source_y not in cached_rows:
            row = mask[source_y, x_index[left:right + 1]]
            changes = np.flatnonzero(row[1:] != row[:-1]) + 1
            lengths = np.diff(np.concatenate(([0], changes, [row.size]))).tolist()
            cached_rows[source_y] = (lengths, bool(row[0]))
        lengths, first = cached_rows[source_y]
        value = first
        for count in lengths:
            if value == previous:
                runs[-1] += count
            else:
                runs.append(count)
                previous = value
            value = not value
    return runs + [left, top, right, bottom]


def execute(input_path: str, sketch_path: str, state: dict, output_path: str, kind: str) -> None:
    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=True)
    with Image.open(input_path) as opened:
        image = opened.convert("RGB")
    if kind == "geometry":
        sketch = sketch_image(image.size, state, Path(sketch_path))
        result = seg.build_sketch_geometry(image, sketch, seg.SketchSettings(**state["sketch"]),
                                           state["segments"], state["regionMode"])
        (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return
    settings_type = seg.Approach1Settings if state["algorithm"] == "approach1" else seg.Approach2Settings
    settings = settings_type(**state[state["algorithm"]])
    method = seg.approach1_class_map if state["algorithm"] == "approach1" else seg.approach2_class_map
    rgb, classes = method(image, settings)
    details = {"correction_applied": False}
    if state["corrected"] and state["algorithm"] == "approach2":
        sketch = sketch_image(image.size, state, Path(sketch_path))
        corrected = seg.corrected_approach2_data_from_class_map(
            image, sketch, rgb, classes, settings, seg.SketchSettings(**state["sketch"]),
            state["segments"], state["regionMode"], seg.CorrectionSettings(**state["correction"]))
        classes = corrected["class_map"]
        details = {"correction_applied": corrected["correction_applied"], "details": corrected["details"]}
    color = np.zeros_like(rgb)
    masks = []
    stats = {}
    for name, value in COLORS.items():
        mask = classes == name
        color[mask] = value
        stats[name] = round(float(mask.mean()) * 100, 3)
        points = encode_resized_mask(mask, image.size)
        if points:
            masks.append({"className": name, "points": points})
    Image.fromarray(color).save(output / "mask.png")
    Image.fromarray((rgb * .45 + color * .55).astype(np.uint8)).save(output / "overlay.png")
    result = {"width": image.width, "height": image.height, "workWidth": rgb.shape[1],
              "workHeight": rgb.shape[0], "stats": stats, "masks": masks, **details}
    (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
