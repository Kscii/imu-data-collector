from pathlib import Path

import imu_data_collector.host as host_module


def test_windows_default_directories_use_local_app_data(monkeypatch) -> None:
    monkeypatch.setattr(host_module, "platform_id", lambda: "windows")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Tester\AppData\Local")

    assert host_module.user_data_dir() == Path(
        r"C:\Users\Tester\AppData\Local"
    ) / "imu-data-collector"
    assert host_module.user_cache_dir() == Path(
        r"C:\Users\Tester\AppData\Local"
    ) / "imu-data-collector" / "cache"


def test_find_executable_does_not_claim_missing_tool_exists(monkeypatch) -> None:
    monkeypatch.setattr(host_module, "platform_id", lambda: "windows")
    monkeypatch.setattr(host_module, "resource_path", lambda relative: Path("/missing") / relative)
    monkeypatch.setattr(host_module.shutil, "which", lambda _name: None)

    assert host_module.find_executable("ffmpeg") is None
    assert host_module.resolve_executable("ffmpeg") == "ffmpeg.exe"


def test_windows_background_processes_do_not_create_console_window(monkeypatch) -> None:
    monkeypatch.setattr(host_module, "platform_id", lambda: "windows")

    options = host_module.background_subprocess_kwargs()

    assert options == {
        "creationflags": getattr(
            host_module.subprocess, "CREATE_NO_WINDOW", 0x08000000
        )
    }


def test_non_windows_background_processes_keep_default_flags(monkeypatch) -> None:
    monkeypatch.setattr(host_module, "platform_id", lambda: "linux")

    assert host_module.background_subprocess_kwargs() == {}
