import runpy
import sys
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient
from test_motion_labels import seed


def test_development_store_allows_reviews_but_blocks_admin_mutations():
    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/serve-motion-preview.py"))
    allowed = module["review_write_path"]
    root = "/api/v1/synthetic/motions/"
    mid = "f" * 64
    for path in (
        root + mid + "/claim",
        root + mid + "/release",
        root + mid + "/variants/example/" + "a" * 64 + "/draft",
        root + mid + "/variants/example/" + "a" * 64 + "/review",
    ):
        assert allowed(path)
    for path in (
        root + "models/retry-failed",
        root + "models/candidate/activate",
        root + "batch",
        root + "snapshots",
        root + mid + "/activate",
        "/api/v1/synthetic/temporal/runs/test/reference",
    ):
        assert not allowed(path)


def test_fresh_workbench_populates_its_actual_api_catalog(tmp_path, monkeypatch):
    store, _, prepared, mid, variant = seed(tmp_path)
    script = Path(__file__).parents[1] / "scripts/serve-motion-preview.py"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--local-store",
            str(store.root),
            "--state-dir",
            str(tmp_path / "fresh-api"),
            "--reviews-only",
        ],
    )

    def serve(app, **kwargs):
        assert app.state.motion_label_service.path != prepared.path
        c = variant["candidate"]
        client = TestClient(app)
        response = client.get(
            f"/api/v1/synthetic/motions/lookup/{c['candidate_id']}/{c['version_id']}"
        )
        assert response.status_code == 200
        assert response.json()["motion_sha256"] == mid
        assert (
            client.post("/api/v1/synthetic/motions/models/test/activate", json={}).status_code
            == 403
        )
        assert client.post(f"/api/v1/synthetic/motions/{mid}/claim", json={}).status_code == 200

    monkeypatch.setattr(uvicorn, "run", serve)
    runpy.run_path(str(script), run_name="__main__")
