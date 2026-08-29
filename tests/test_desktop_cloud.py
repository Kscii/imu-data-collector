import base64
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

import imu_data_collector.broker_client as broker_client
import imu_data_collector.desktop_auth as desktop_auth
from imu_data_collector import upload_broker
from imu_data_collector.config import (
    DesktopCloudSettings,
    IdentitySettings,
    Settings,
    StorageSettings,
    load_settings,
)
from imu_data_collector.desktop_auth import DesktopOAuthManager
from imu_data_collector.models import ArtifactDescriptor, CaptureManifestV2


def _test_id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=")
    return f"header.{payload.decode()}.signature"


def test_google_oauth_client_id_can_be_isolated_from_shared_yaml(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv(
        "IMU_GOOGLE_OAUTH_CLIENT_ID",
        "desktop.apps.googleusercontent.com",
    )
    monkeypatch.setenv("IMU_GOOGLE_OAUTH_CLIENT_SECRET", "server-private-secret")
    monkeypatch.setenv("IMU_UPLOAD_BROKER_HOST", "0.0.0.0")
    monkeypatch.setenv("IMU_UPLOAD_BROKER_PORT", "9876")

    settings = load_settings(config_path)

    assert (
        settings.cloud.google_oauth_client_id
        == "desktop.apps.googleusercontent.com"
    )
    assert settings.cloud.broker_server_host == "0.0.0.0"
    assert settings.cloud.broker_server_port == 9876
    assert settings.cloud.google_oauth_client_secret == "server-private-secret"


def test_packaged_desktop_config_never_contains_client_secret(tmp_path: Path) -> None:
    output = tmp_path / "desktop-config"

    subprocess.run(
        [
            sys.executable,
            "scripts/prepare_desktop_config.py",
            "--output",
            str(output),
            "--broker-url",
            "https://upload.example.test",
            "--oauth-client-id",
            "desktop.apps.googleusercontent.com",
        ],
        check=True,
    )

    payload = yaml.safe_load((output / "default.yaml").read_text(encoding="utf-8"))
    assert payload["publish"]["mode"] == "broker"
    assert payload["cloud"]["broker_url"] == "https://upload.example.test"
    assert "google_oauth_client_secret" not in payload["cloud"]


def test_desktop_oauth_uses_pkce_and_stores_refresh_token_and_display_email(monkeypatch) -> None:
    secrets_store: dict[tuple[str, str], str] = {}
    posted: list[dict] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "refresh_token": "long-lived-secret",
                "id_token": _test_id_token("member@example.com"),
                "expires_in": 3600,
            }

    def post(url: str, *, json: dict, timeout: int):
        assert timeout == 20
        assert url == "https://upload.example.test/v1/oauth/token"
        posted.append(json)
        return Response()

    monkeypatch.setattr(desktop_auth.requests, "post", post)
    monkeypatch.setattr(
        desktop_auth.keyring,
        "set_password",
        lambda service, name, value: secrets_store.__setitem__((service, name), value),
    )
    monkeypatch.setattr(
        desktop_auth.keyring,
        "get_password",
        lambda service, name: secrets_store.get((service, name)),
    )

    settings = DesktopCloudSettings(
        broker_url="https://upload.example.test",
        google_oauth_client_id="desktop.apps.googleusercontent.com",
    )
    manager = DesktopOAuthManager(settings)
    authorization_url = manager.begin(
        "http://127.0.0.1:8765/api/v1/cloud/oauth/callback"
    )
    query = parse_qs(urlparse(authorization_url).query)

    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid email profile"]
    assert "code_verifier" not in query
    assert "client_secret" not in query
    status = manager.complete(state=query["state"][0], code="one-time-code")

    assert posted[0]["code_verifier"]
    assert posted[0]["grant_type"] == "authorization_code"
    assert "client_id" not in posted[0]
    assert "client_secret" not in posted[0]
    assert "long-lived-secret" in secrets_store.values()
    assert "member@example.com" in secrets_store.values()
    assert "one-time-code" not in secrets_store.values()
    assert status["logged_in"] is True
    assert status["email"] == "member@example.com"
    assert manager.id_token() == _test_id_token("member@example.com")
    assert DesktopOAuthManager(settings).status()["email"] == "member@example.com"


def test_desktop_oauth_reports_safe_google_error_details(monkeypatch) -> None:
    class Response:
        status_code = 400

        def raise_for_status(self) -> None:
            raise requests.HTTPError(response=self)

        def json(self) -> dict[str, str]:
            return {
                "error": "invalid_request",
                "error_description": "client_secret is missing.",
            }

    monkeypatch.setattr(
        desktop_auth.requests,
        "post",
        lambda *_args, **_kwargs: Response(),
    )
    settings = DesktopCloudSettings(
        broker_url="https://upload.example.test",
        google_oauth_client_id="desktop.apps.googleusercontent.com",
    )
    manager = DesktopOAuthManager(settings)
    authorization_url = manager.begin(
        "http://127.0.0.1:8765/api/v1/cloud/oauth/callback"
    )
    state = parse_qs(urlparse(authorization_url).query)["state"][0]

    with pytest.raises(RuntimeError, match="HTTP 400，invalid_request") as caught:
        manager.complete(state=state, code="one-time-code")

    assert "client_secret is missing" in str(caught.value)
    assert "one-time-code" not in str(caught.value)


def test_desktop_oauth_does_not_require_native_client_secret() -> None:
    manager = DesktopOAuthManager(
        DesktopCloudSettings(
            broker_url="https://upload.example.test",
            google_oauth_client_id="desktop.apps.googleusercontent.com",
        )
    )

    assert manager.configured is True
    assert manager.begin(
        "http://127.0.0.1:8765/api/v1/cloud/oauth/callback"
    ).startswith(
        "https://accounts.google.com/"
    )


def test_desktop_oauth_refresh_uses_broker_without_client_secret(monkeypatch) -> None:
    posted: list[dict] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "id_token": _test_id_token("member@example.com"),
                "expires_in": 3600,
            }

    def post(url: str, *, json: dict, timeout: int):
        assert timeout == 20
        assert url == "https://upload.example.test/v1/oauth/token"
        posted.append(json)
        return Response()

    monkeypatch.setattr(desktop_auth.requests, "post", post)
    monkeypatch.setattr(
        desktop_auth.keyring,
        "get_password",
        lambda _service, name: (
            "stored-refresh-token" if "refresh-token" in name else None
        ),
    )
    settings = DesktopCloudSettings(
        broker_url="https://upload.example.test",
        google_oauth_client_id="desktop.apps.googleusercontent.com",
    )

    token = DesktopOAuthManager(settings).id_token()

    assert token == _test_id_token("member@example.com")
    assert posted == [
        {
            "refresh_token": "stored-refresh-token",
            "grant_type": "refresh_token",
        }
    ]


def test_resumable_upload_uses_bounded_chunks(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"abcdefghij")
    ranges: list[str] = []

    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            return None

    def put(_url: str, *, data: bytes, headers: dict[str, str], timeout: int):
        assert len(data) <= 4
        assert timeout == 180
        ranges.append(headers["Content-Range"])
        return Response(201 if headers["Content-Range"].startswith("bytes 8-") else 308)

    monkeypatch.setattr(broker_client.requests, "put", put)
    broker_client._put_resumable("https://storage.example/session", source, chunk_size=4)

    assert ranges == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]


def _manifest(*, h5_key: str = "captures/recording-1/capture.h5") -> CaptureManifestV2:
    zero_sha = "0" * 64
    return CaptureManifestV2(
        recording_id="recording-1",
        collection_id="collection-1",
        participant_id="xfan0282",
        data_tier="test",
        captured_at_utc="2026-08-28T00:00:00Z",
        duration_ns=1_000_000_000,
        source_h5_schema_version="1.6.0",
        software_revision="test",
        artifacts=[
            ArtifactDescriptor(
                role="capture_h5",
                object_key=h5_key,
                filename="capture.h5",
                size_bytes=1,
                sha256=zero_sha,
                content_type="application/x-hdf5",
            ),
            ArtifactDescriptor(
                role="video_mkv",
                object_key="captures/recording-1/video.mkv",
                filename="video.mkv",
                size_bytes=1,
                sha256=zero_sha,
                content_type="video/x-matroska",
            ),
            ArtifactDescriptor(
                role="preview_mp4",
                object_key="captures/recording-1/preview.mp4",
                filename="preview.mp4",
                size_bytes=1,
                sha256=zero_sha,
                content_type="video/mp4",
            ),
        ],
    )


def test_upload_broker_accepts_only_exact_recording_artifact_keys() -> None:
    upload_broker._verify_manifest_keys(_manifest())

    with pytest.raises(HTTPException, match="稳定键") as error:
        upload_broker._verify_manifest_keys(
            _manifest(h5_key="captures/recording-1/replacement.h5")
        )
    assert error.value.status_code == 422


class _FakeBlob:
    def __init__(self, name: str) -> None:
        self.name = name
        self.content: bytes | None = None
        self.content_type: str | None = None
        self.metadata: dict[str, str] = {}
        self.size: int | None = None
        self.generation: int | None = None

    def exists(self, _client) -> bool:
        return self.content is not None

    def reload(self, _client) -> None:
        return None

    def download_as_bytes(self, client=None) -> bytes:
        assert self.content is not None
        return self.content

    def open(self, mode: str):
        assert mode == "rb"
        assert self.content is not None
        return io.BytesIO(self.content)

    def create_resumable_upload_session(self, **_kwargs) -> str:
        return f"https://storage.example/{self.name}"

    def upload_from_string(self, value, **_kwargs) -> None:
        self.content = value.encode() if isinstance(value, str) else value
        self.size = len(self.content)
        self.generation = 1


class _FakeBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, _FakeBlob] = {}

    def blob(self, name: str) -> _FakeBlob:
        return self.blobs.setdefault(name, _FakeBlob(name))


def _patch_broker_storage(monkeypatch, bucket: _FakeBucket) -> None:
    class Client:
        def __init__(self, project=None) -> None:
            self.project = project

        def bucket(self, _name: str) -> _FakeBucket:
            return bucket

    monkeypatch.setattr(upload_broker.storage, "Client", Client)


def _broker_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        catalog_path=tmp_path / "capture.sqlite3",
        storage=StorageSettings(
            backend="gcs",
            bucket="team-bucket",
            project="test-project",
        ),
        cloud=DesktopCloudSettings(
            google_oauth_client_id="desktop.apps.googleusercontent.com",
            google_oauth_client_secret="server-private-secret",
        ),
        identity=IdentitySettings(
            email_to_unikey={"member@example.com": "xfan0282"}
        ),
    )


def test_upload_broker_refuses_to_start_without_private_client_secret(
    monkeypatch, tmp_path
) -> None:
    _patch_broker_storage(monkeypatch, _FakeBucket())
    settings = _broker_settings(tmp_path)
    settings.cloud.google_oauth_client_secret = None

    with pytest.raises(RuntimeError, match="服务器私有"):
        upload_broker.create_upload_broker_app(settings)


def test_upload_broker_exchanges_code_with_server_secret_and_filters_response(
    monkeypatch, tmp_path
) -> None:
    _patch_broker_storage(monkeypatch, _FakeBucket())
    posted: list[dict] = []

    class Response:
        ok = True

        def json(self) -> dict:
            return {
                "access_token": "desktop-does-not-need-this",
                "refresh_token": "stored-on-desktop-keyring",
                "id_token": _test_id_token("member@example.com"),
                "expires_in": 3600,
            }

    def post(url: str, *, data: dict, timeout: int):
        assert url == "https://oauth2.googleapis.com/token"
        assert timeout == 20
        posted.append(data)
        return Response()

    monkeypatch.setattr(upload_broker.requests, "post", post)
    app = upload_broker.create_upload_broker_app(_broker_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": "one-time-code",
                "code_verifier": "v" * 64,
                "redirect_uri": (
                    "http://127.0.0.1:8765/api/v1/cloud/oauth/callback"
                ),
            },
        )

    assert response.status_code == 200, response.json()
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "id_token": _test_id_token("member@example.com"),
        "refresh_token": "stored-on-desktop-keyring",
        "expires_in": 3600,
    }
    assert posted[0]["client_id"] == "desktop.apps.googleusercontent.com"
    assert posted[0]["client_secret"] == "server-private-secret"
    assert posted[0]["code_verifier"] == "v" * 64


def test_upload_broker_rejects_non_loopback_oauth_redirect(
    monkeypatch, tmp_path
) -> None:
    _patch_broker_storage(monkeypatch, _FakeBucket())
    monkeypatch.setattr(
        upload_broker.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("禁止向 Google 转发非本机回调"),
    )
    app = upload_broker.create_upload_broker_app(_broker_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": "one-time-code",
                "code_verifier": "v" * 64,
                "redirect_uri": "https://attacker.example/callback",
            },
        )

    assert response.status_code == 422
    assert "本机回调" in response.json()["detail"]


def test_upload_broker_does_not_echo_oauth_credentials_in_error(
    monkeypatch, tmp_path
) -> None:
    _patch_broker_storage(monkeypatch, _FakeBucket())

    class Response:
        ok = False
        status_code = 400

        def json(self) -> dict:
            return {
                "error": "invalid_grant\n",
                "error_description": "bad one-time-code server-private-secret",
            }

    monkeypatch.setattr(
        upload_broker.requests,
        "post",
        lambda *_args, **_kwargs: Response(),
    )
    app = upload_broker.create_upload_broker_app(_broker_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": "one-time-code",
                "code_verifier": "v" * 64,
                "redirect_uri": (
                    "http://127.0.0.1:8765/api/v1/cloud/oauth/callback"
                ),
            },
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert "one-time-code" not in response.text
    assert "server-private-secret" not in response.text


def test_upload_broker_requires_whitelisted_google_identity(monkeypatch, tmp_path) -> None:
    bucket = _FakeBucket()
    _patch_broker_storage(monkeypatch, bucket)
    monkeypatch.setattr(
        upload_broker.google_id_token,
        "verify_oauth2_token",
        lambda token, _request, audience: {
            "email": "member@example.com",
            "email_verified": True,
            "aud": audience,
        },
    )
    settings = _broker_settings(tmp_path)
    app = upload_broker.create_upload_broker_app(settings)
    with TestClient(app) as client:
        unauthorized = client.post(
            "/v1/uploads",
            json={"manifest": _manifest().model_dump(mode="json")},
        )
        assert unauthorized.status_code == 401, unauthorized.json()
        response = client.post(
            "/v1/uploads",
            json={"manifest": _manifest().model_dump(mode="json")},
            headers={"Authorization": "Bearer signed-id-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["sessions"]) == 3
    assert all(
        item["session_url"].startswith("https://storage.example/")
        for item in payload["sessions"]
    )
    assert bucket.blob(f"_upload_sessions/{payload['upload_id']}.json").exists(None)


def _model_package_request() -> tuple[dict, dict[str, bytes]]:
    prefix = "benchmark-models/packages/threshold-impact-deadbeef0000"
    payloads = {"bundle": b"bundle", "model": b"onnx"}
    descriptors = [
        {
            "file_id": "bundle",
            "object_key": f"{prefix}/package.tar.gz",
            "size_bytes": len(payloads["bundle"]),
            "sha256": hashlib.sha256(payloads["bundle"]).hexdigest(),
            "content_type": "application/gzip",
        },
        {
            "file_id": "model",
            "object_key": f"{prefix}/files/model.onnx",
            "size_bytes": len(payloads["model"]),
            "sha256": hashlib.sha256(payloads["model"]).hexdigest(),
            "content_type": "application/octet-stream",
        },
    ]
    marker = {
        "schema_version": "imu_model_package_publication_v1",
        "package_id": "threshold-impact-deadbeef0000",
        "created_at_utc": "2026-08-29T00:00:00+00:00",
        "logical_digest": "d" * 64,
        "manifest": {"model_code": "threshold-impact"},
        "bundle": {
            "filename": "package.tar.gz",
            "size_bytes": descriptors[0]["size_bytes"],
            "sha256": descriptors[0]["sha256"],
        },
        "files": [
            {
                "file_id": "model",
                "filename": "model.onnx",
                "object_key": descriptors[1]["object_key"],
                "size_bytes": descriptors[1]["size_bytes"],
                "sha256": descriptors[1]["sha256"],
                "content_type": "application/octet-stream",
            }
        ],
    }
    return {
        "publication_kind": "package",
        "publication_id": "threshold-impact-deadbeef0000",
        "marker": marker,
        "artifacts": descriptors,
    }, payloads


def test_model_broker_constrains_keys_verifies_payload_and_writes_marker_last(
    monkeypatch, tmp_path
) -> None:
    bucket = _FakeBucket()
    _patch_broker_storage(monkeypatch, bucket)
    monkeypatch.setattr(
        upload_broker.google_id_token,
        "verify_oauth2_token",
        lambda token, _request, audience: {
            "email": "member@example.com",
            "email_verified": True,
            "aud": audience,
        },
    )
    app = upload_broker.create_upload_broker_app(_broker_settings(tmp_path))
    request, payloads = _model_package_request()
    headers = {"Authorization": "Bearer gcloud-signed-id-token"}
    with TestClient(app) as client:
        started = client.post("/v1/model-uploads", json=request, headers=headers)
        assert started.status_code == 200, started.json()
        plan = started.json()
        assert not bucket.blob(
            "benchmark-models/packages/threshold-impact-deadbeef0000/publication.json"
        ).exists(None)
        for session in plan["sessions"]:
            blob = bucket.blob(session["object_key"])
            blob.content = payloads[session["file_id"]]
            blob.size = len(blob.content)
            blob.generation = 1
        completed = client.post(
            "/v1/model-uploads/complete",
            json={"upload_id": plan["upload_id"]},
            headers=headers,
        )
        state = bucket.blob(
            "benchmark-models/packages/threshold-impact-deadbeef0000/state.json"
        )
        marker = bucket.blob(
            "benchmark-models/packages/threshold-impact-deadbeef0000/publication.json"
        )
        restored = client.post(
            "/v1/model-publications/package/threshold-impact-deadbeef0000/restore",
            json={"expected_generation": 1},
            headers=headers,
        )

    assert completed.status_code == 200, completed.json()
    assert state.exists(None)
    assert marker.exists(None)
    assert restored.status_code == 200, restored.json()
    assert restored.json()["history"][-1]["action"] == "restore"


def test_model_broker_rejects_caller_selected_object_key(monkeypatch, tmp_path) -> None:
    bucket = _FakeBucket()
    _patch_broker_storage(monkeypatch, bucket)
    monkeypatch.setattr(
        upload_broker.google_id_token,
        "verify_oauth2_token",
        lambda token, _request, audience: {
            "email": "member@example.com",
            "email_verified": True,
            "aud": audience,
        },
    )
    request, _payloads = _model_package_request()
    request["artifacts"][1]["object_key"] = "captures/other/manifest.json"
    app = upload_broker.create_upload_broker_app(_broker_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/model-uploads",
            json=request,
            headers={"Authorization": "Bearer gcloud-signed-id-token"},
        )

    assert response.status_code == 422
    assert "推导结果" in response.json()["detail"]
