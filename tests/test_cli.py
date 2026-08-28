import argparse
import json
import os
from pathlib import Path

import yaml

from imu_data_collector.annotation_cli import _manage_member
from imu_data_collector.cli import _estimate_batched_sample_rate


def test_batched_sample_rate_includes_one_packet_interval_of_coverage() -> None:
    packet_times = [1_000_000_000, 2_000_000_000, 3_000_000_000]

    coverage_seconds, rate_hz = _estimate_batched_sample_rate(75, packet_times)

    assert coverage_seconds == 3.0
    assert rate_hz == 25.0


def test_batched_sample_rate_requires_two_packets() -> None:
    assert _estimate_batched_sample_rate(25, [1_000_000_000]) == (None, None)


def _member_args(path: Path, command: str, *, apply: bool) -> argparse.Namespace:
    return argparse.Namespace(
        command=command,
        config=path,
        email="member@example.com",
        unikey="rkim6933",
        project="test-project",
        backend_service="annotation-backend",
        apply=apply,
    )


def test_member_management_is_preview_first_and_preserves_config_metadata(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "identity": {
                    "allowed_unikeys": ["rkim6933", "xfan0282"],
                    "email_to_unikey": {},
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o640)
    original = path.stat()
    chown_calls: list[tuple[Path, int, int]] = []
    real_chown = getattr(os, "chown", None)

    def record_chown(target, uid: int, gid: int) -> None:
        chown_calls.append((Path(target), uid, gid))
        if real_chown is not None:
            real_chown(target, uid, gid)

    monkeypatch.setattr(
        "imu_data_collector.annotation_cli.os.chown",
        record_chown,
        raising=False,
    )

    _manage_member(_member_args(path, "member-add", apply=False))
    preview = json.loads(capsys.readouterr().out)
    assert preview["apply"] is False
    assert "member@example.com" not in yaml.safe_load(path.read_text())["identity"][
        "email_to_unikey"
    ]
    assert "add-iam-policy-binding" in preview["iap_command"]

    _manage_member(_member_args(path, "member-add", apply=True))
    capsys.readouterr()
    assert yaml.safe_load(path.read_text())["identity"]["email_to_unikey"] == {
        "member@example.com": "rkim6933"
    }
    if real_chown is not None:
        assert chown_calls[-1][1:] == (original.st_uid, original.st_gid)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o640
    if real_chown is not None:
        assert (path.stat().st_uid, path.stat().st_gid) == (
            original.st_uid,
            original.st_gid,
        )

    _manage_member(_member_args(path, "member-remove", apply=True))
    removed = json.loads(capsys.readouterr().out)
    assert "remove-iam-policy-binding" in removed["iap_command"]
    assert removed["unikey"] == "rkim6933"
    assert yaml.safe_load(path.read_text())["identity"]["email_to_unikey"] == {}
