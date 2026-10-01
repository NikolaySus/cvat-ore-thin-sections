"""Exercise the real CVAT adapter, worker queue and cancellation on fixture frame 1."""
import json
import time
from pathlib import Path

import httpx


def wait(client, key):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = client.get(f"/ore/api/jobs/{key}")
        response.raise_for_status()
        status = response.json()
        if status["status"] in ("completed", "cancelled", "failed"):
            return status
        time.sleep(.3)
    raise TimeoutError(key)


def main():
    info = json.loads((Path(__file__).resolve().parents[1] / "data/fixture.json").read_text())
    base = f"/ore/api/frames/{info['job']}/1"
    with httpx.Client(base_url="http://localhost:8080", timeout=120, trust_env=False) as client:
        assert client.get(base).status_code in (401, 403)
        login = client.post("/api/auth/login", json={"username": "ore", "password": "ore-local-demo"})
        login.raise_for_status()
        client.headers["Authorization"] = "Token " + login.json()["key"]
        response = client.get(base)
        response.raise_for_status()
        record = response.json()
        record["state"]["approach2"].update(max_work_side=512, sample_pixels=10000)
        record["state"]["corrected"] = False
        response = client.put(base, json={"revision": record["revision"], "state": record["state"]})
        response.raise_for_status()
        saved = response.json()
        assert client.put(base, json={"revision": record["revision"], "state": record["state"]}).status_code == 409
        params = {"revision": saved["revision"]}
        first = client.post(base + "/jobs", params=params)
        first.raise_for_status()
        key = first.json()["id"]
        second = client.post(base + "/jobs", params=params)
        second.raise_for_status()
        cancelled_key = second.json()["id"]
        assert client.delete(f"/ore/api/jobs/{cancelled_key}").status_code == 200
        status = wait(client, key)
        assert status["status"] == "completed", status
        assert wait(client, cancelled_key)["status"] == "cancelled"
        response = client.get(f"/ore/api/jobs/{key}/result")
        response.raise_for_status()
        result = response.json()["result"]
        assert result["width"] == record["width"]
        assert abs(sum(result["stats"].values()) - 100) < .01
        assert 1 <= len(result["masks"]) <= 4
        assert client.get(f"/ore/api/jobs/{key}/mask.png").headers["content-type"] == "image/png"
        assert client.get(f"/ore/api/jobs/{key}/../../state.sqlite").status_code == 404
        print(json.dumps({"status": "passed", "job": info["job"], "frame": 1,
                          "elapsed": status["elapsed"], "stats": result["stats"],
                          "checks": ["CVAT authentication", "optimistic revision", "worker result", "queue cancellation", "mask asset"]}, indent=2))


if __name__ == "__main__":
    main()
