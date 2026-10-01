import json
import importlib.util
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from ore_service.engine import encode_mask, encode_resized_mask, defaults, execute
from ore_service import segmentation as seg


def decode(points, size):
    out = np.zeros((size[1], size[0]), dtype=bool)
    if not points:
        return out
    left, top, right, bottom = points[-4:]
    flat = np.zeros((right - left + 1) * (bottom - top + 1), dtype=bool)
    offset = 0
    for index, length in enumerate(points[:-4]):
        flat[offset:offset + length] = bool(index % 2)
        offset += length
    assert offset == flat.size
    out[top:bottom + 1, left:right + 1] = flat.reshape(bottom - top + 1, right - left + 1)
    return out


@pytest.mark.parametrize("shape,size", [((4, 5), (5, 4)), ((4, 5), (37, 23)), ((17, 13), (4, 3))])
def test_rle_original_coordinates(shape, size):
    rng = np.random.default_rng(42)
    for mask in (np.zeros(shape, bool), np.ones(shape, bool), rng.random(shape) > .5):
        expected = np.asarray(Image.fromarray(mask).resize(size, Image.Resampling.NEAREST))
        assert np.array_equal(decode(encode_resized_mask(mask, size), size), expected)
        assert np.array_equal(decode(encode_mask(mask), (shape[1], shape[0])), mask)


def test_rle_matches_cvat_sdk():
    source = Path(__file__).resolve().parents[1] / "vendor/cvat/cvat-sdk/cvat_sdk/masks.py"
    if not source.exists():
        pytest.skip("Run scripts/prepare.sh for official CVAT SDK compatibility check")
    spec = importlib.util.spec_from_file_location("cvat_masks", source)
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    bitmap = np.random.default_rng(8).random((31, 47)) > .6
    assert encode_mask(bitmap) == sdk.encode_mask(bitmap)
    encoded = encode_resized_mask(bitmap, (149, 121))
    decoded = sdk.decode_mask(encoded, image_width=149, image_height=121)
    assert np.array_equal(decoded, np.asarray(Image.fromarray(bitmap).resize((149, 121), Image.Resampling.NEAREST)))


def test_engine_masks_partition_frame(tmp_path):
    rng = np.random.default_rng(1)
    image = Image.fromarray(rng.integers(0, 255, (128, 192, 3), dtype=np.uint8))
    source = tmp_path / "input.png"
    image.save(source)
    state = defaults()
    state["approach2"].update(max_work_side=128, sample_pixels=2000, local_shadow_correction_strength=0,
                                illumination_correction_strength=0, min_component_area=0)
    output = tmp_path / "output"
    execute(str(source), str(tmp_path / "missing.png"), state, str(output), "segmentation")
    result = json.loads((output / "result.json").read_text())
    total = sum(decode(mask["points"], image.size).astype(int) for mask in result["masks"])
    assert np.all(total == 1)
    assert result["workWidth"] == 128
    assert abs(sum(result["stats"].values()) - 100) < .01


def test_first_approach_preserves_existing_output():
    image = Image.fromarray(np.random.default_rng(4).integers(0, 255, (80, 80, 3), dtype=np.uint8))
    settings = replace(seg.Approach1Settings(), min_component_area=0, sample_pixels=1000)
    rgb, classes = seg.approach1_class_map(image, settings)
    expected = seg._make_result(rgb, classes, {})
    actual = seg.run_approach1(image, settings)
    assert actual.stats == expected.stats
    assert actual.overlay_png == expected.overlay_png


def test_drawn_contour_produces_region(tmp_path):
    image = Image.new("RGB", (256, 256), "gray")
    source = tmp_path / "image.png"
    image.save(source)
    state = defaults()
    state["strokes"] = [[[40, 40], [216, 40], [216, 216], [40, 216], [40, 40]]]
    execute(str(source), str(tmp_path / "none"), state, str(tmp_path / "output"), "geometry")
    geometry = json.loads((tmp_path / "output/result.json").read_text())
    assert geometry["lines"]
    assert geometry["regionChoices"]


def test_freehand_geometry_does_not_add_rectangle(tmp_path):
    source = tmp_path / "image.png"
    Image.new("RGB", (256, 256), "gray").save(source)
    state = defaults()
    state["strokes"] = [[[30, 50], [80, 55], [130, 70], [180, 60]]]
    output = tmp_path / "output"
    execute(str(source), str(tmp_path / "missing.png"), state, str(output), "geometry")
    geometry = json.loads((output / "result.json").read_text())
    assert len(geometry["lines"]) == 1
    assert not geometry["segments"]
    assert not geometry["regionChoices"]
    for path in geometry["lines"][0]["paths"]:
        for x, y in seg._svg_path_points(path):
            assert 25 <= x <= 185
            assert 45 <= y <= 75
