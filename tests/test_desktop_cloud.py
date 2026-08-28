import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
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

    settings = load_settings(config_path)

    assert (
        settings.cloud.google_oauth_client_id
        == "desktop.apps.googleusercontent.com"
    )


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

    def post(_url: str, *, data: dict, timeout: int):
        assert timeout == 20
        posted.append(data)
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
    authorization_url = manager.begin("http://127.0.0.1:8765/callback")
    query = parse_qs(urlparse(authorization_url).query)

    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid email profile"]
    assert "code_verifier" not in query
    status = manager.complete(state=query["state"][0], code="one-time-code")

    assert posted[0]["code_verifier"]
    assert posted[0]["grant_type"] == "authorization_code"
    assert "long-lived-secret" in secrets_store.values()
    assert "member@example.com" in secrets_store.values()
    assert "one-time-code" not in secrets_store.values()
    assert status["logged_in"] is True
    assert status["email"] == "member@example.com"
    assert manager.id_token() == _test_id_token("member@example.com")
    assert DesktopOAuthManager(settings).status()["email"] == "member@example.com"


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


def test_upload_broker_requires_whitelisted_google_identity(monkeypatch, tmp_path) -> None:
    bucket = _FakeBucket()

    class Client:
        def __init__(self, project=None) -> None:
            self.project = project

        def bucket(self, _name: str) -> _FakeBucket:
            return bucket

    monkeypatch.setattr(upload_broker.storage, "Client", Client)
    monkeypatch.setattr(
        upload_broker.google_id_token,
        "verify_oauth2_token",
        lambda token, _request, audience: {
            "email": "member@example.com",
            "email_verified": True,
            "aud": audience,
        },
    )
    settings = Settings(
        data_root=tmp_path / "data",
        catalog_path=tmp_path / "capture.sqlite3",
        storage=StorageSettings(
            backend="gcs",
            bucket="team-bucket",
            project="test-project",
        ),
        cloud=DesktopCloudSettings(
            google_oauth_client_id="desktop.apps.googleusercontent.com",
        ),
        identity=IdentitySettings(
            email_to_unikey={"member@example.com": "xfan0282"}
        ),
    )
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
