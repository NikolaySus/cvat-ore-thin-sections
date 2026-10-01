"""Create a small CVAT task for a reproducible local integration check."""
import argparse
import json
import time
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

LABELS = [("Матрица", "#ffeb00"), ("Сульфидные фазы", "#ff2323"), ("Тальк", "#0046ff"), ("Дефекты", "#80002d")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path)
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--username", default="ore")
    parser.add_argument("--password", default="ore-local-demo")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "data"
    root.mkdir(exist_ok=True)
    if args.image:
        image = Image.open(args.image).convert("RGB")
        image.thumbnail((1600, 1600))
    else:
        image = Image.new("RGB", (768, 512), (55, 62, 60))
        painter = ImageDraw.Draw(image)
        for x in range(30, 740, 80):
            for y in range(30, 490, 80):
                painter.ellipse((x, y, x + 50, y + 35), fill=(205, 192, 110))
        painter.ellipse((100, 100, 500, 400), fill=(22, 25, 24))
    image.save(root / "fixture.jpg")
    sketch = Image.new("RGB", image.size, "white")
    painter = ImageDraw.Draw(sketch)
    w, h = image.size
    # Leave two visible gaps so the connection editor can be tested.
    painter.line([(w * .2, h * .2), (w * .8, h * .2), (w * .8, h * .75)], fill=(0, 55, 255), width=8)
    painter.line([(w * .2, h * .3), (w * .2, h * .8), (w * .7, h * .8)], fill=(0, 55, 255), width=8)
    sketch.save(root / "fixture-sketch.png")
    with httpx.Client(base_url=args.url, timeout=120, trust_env=False) as client:
        response = client.post("/api/auth/login", json={"username": args.username, "password": args.password})
        response.raise_for_status()
        token = response.json()["key"]
        client.headers["Authorization"] = "Token " + token
        response = client.post("/api/tasks", json={"name": "Ore plugin local check", "labels": [
            {"name": name, "color": color, "type": "mask", "attributes": [
                {"name": "ore_run", "mutable": False, "input_type": "text", "default_value": "", "values": []}
            ]} for name, color in LABELS]})
        response.raise_for_status()
        task = response.json()["id"]
        payload = BytesIO()
        image.save(payload, format="JPEG")
        response = client.post(f"/api/tasks/{task}/data", data={"image_quality": "95"}, files=[
            ("client_files[0]", ("thin-section-1.jpg", payload.getvalue(), "image/jpeg")),
            ("client_files[1]", ("thin-section-2.jpg", payload.getvalue(), "image/jpeg")),
        ])
        response.raise_for_status()
        rq = response.json()["rq_id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = client.get(f"/api/requests/{rq}")
            response.raise_for_status()
            status = response.json()
            if status["status"] == "finished":
                break
            if status["status"] == "failed":
                raise RuntimeError(status)
            time.sleep(1)
        else:
            raise TimeoutError("CVAT task creation timed out")
        response = client.get("/api/jobs", params={"task_id": task})
        response.raise_for_status()
        job = response.json()["results"][0]["id"]
        info = {"task": task, "job": job, "url": f"{args.url}/tasks/{task}/jobs/{job}",
                "username": args.username}
        (root / "fixture.json").write_text(json.dumps(info, indent=2))
        print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
