import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "desktop_ci_scope.py"


def _desktop_build_required(paths: list[str]) -> bool:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--null"],
        input="\0".join(paths).encode(),
        check=True,
        capture_output=True,
    )
    return result.stdout.strip() == b"true"


def test_annotation_only_changes_skip_desktop_packages() -> None:
    paths = [
        "src/imu_data_collector/annotation_service.py",
        "frontend/src/annotationTimeline.ts",
        "tests/test_annotation_consistency.py",
        "configs/server.annotation.example.yaml",
        "scripts/deploy/imu-annotation-deploy",
        ".github/workflows/deploy-production.yml",
        "docs/annotation-and-sync.md",
    ]

    assert _desktop_build_required(paths) is False


def test_shared_or_desktop_change_requires_packages() -> None:
    for path in (
        "frontend/src/App.tsx",
        "src/imu_data_collector/models.py",
        "src/imu_data_collector/identity_migration.py",
        "packaging/imu_collector.spec",
        "uv.lock",
    ):
        assert _desktop_build_required([path]) is True


def test_mixed_change_requires_packages() -> None:
    assert _desktop_build_required(
        [
            "src/imu_data_collector/annotation_service.py",
            "src/imu_data_collector/capture_api.py",
        ]
    ) is True


def test_empty_or_unsafe_input_requires_packages() -> None:
    assert _desktop_build_required([]) is True
    assert _desktop_build_required(["../docs/annotation-and-sync.md"]) is True
