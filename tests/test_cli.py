import argparse
import json
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


def test_member_management_is_preview_first_and_updates_private_mapping(
    tmp_path: Path, capsys
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

    _manage_member(_member_args(path, "member-remove", apply=True))
    removed = json.loads(capsys.readouterr().out)
    assert "remove-iam-policy-binding" in removed["iap_command"]
    assert removed["unikey"] == "rkim6933"
    assert yaml.safe_load(path.read_text())["identity"]["email_to_unikey"] == {}
