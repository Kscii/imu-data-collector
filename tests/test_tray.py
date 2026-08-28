import sys
from pathlib import Path
from types import SimpleNamespace

import imu_data_collector.tray as tray
from imu_data_collector.config import Settings


class _Icon:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        catalog_path=tmp_path / "catalog.sqlite3",
        activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
    )


def test_tray_icon_is_self_contained() -> None:
    image = tray._create_icon_image()
    assert image.size == (64, 64)
    assert image.mode == "RGBA"


def test_tray_reuses_existing_capture_backend(monkeypatch, tmp_path: Path) -> None:
    application = tray.TrayApplication(_settings(tmp_path))
    monkeypatch.setattr(
        tray,
        "_health",
        lambda _url: {
            "application": "capture",
            "build_id": tray.CAPTURE_API_BUILD_ID,
        },
    )

    application._start_server()

    assert application.owns_server is False
    assert application.server is None


def test_tray_server_config_does_not_require_console_streams(
    monkeypatch, tmp_path: Path
) -> None:
    application = tray.TrayApplication(_settings(tmp_path))
    captured: dict[str, object] = {}

    class _Server:
        def __init__(self, config) -> None:
            captured["config"] = config

        def run(self) -> None:
            return None

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(tray.uvicorn, "Server", _Server)
    config = tray.uvicorn.Config(
        tray.create_capture_app(application.settings),
        host=application.settings.server_host,
        port=application.settings.server_port,
        log_config=None,
        access_log=False,
    )
    tray.uvicorn.Server(config)

    assert captured["config"].log_config is None  # type: ignore[union-attr]


def test_tray_refuses_to_exit_during_recording(monkeypatch, tmp_path: Path) -> None:
    application = tray.TrayApplication(_settings(tmp_path))
    icon = _Icon()
    messages: list[str] = []
    monkeypatch.setattr(tray, "_uses_chinese", lambda: True)
    monkeypatch.setattr(tray, "_health", lambda _url: {"state": "recording"})
    monkeypatch.setattr(
        tray,
        "_show_message",
        lambda _title, message, **_kwargs: messages.append(message),
    )

    application._exit(icon)

    assert icon.stopped is False
    assert "结束录制" in messages[0]


def test_tray_idle_exit_requests_graceful_server_shutdown(
    monkeypatch, tmp_path: Path
) -> None:
    application = tray.TrayApplication(_settings(tmp_path))
    application.owns_server = True
    application.server = SimpleNamespace(should_exit=False)  # type: ignore[assignment]
    icon = _Icon()
    monkeypatch.setattr(tray, "_health", lambda _url: {"state": "idle"})
    monkeypatch.setattr(tray, "_confirm_exit", lambda: True)

    application._exit(icon)

    assert application.server.should_exit is True
    assert icon.stopped is True
