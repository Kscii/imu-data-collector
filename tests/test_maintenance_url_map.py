from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "deploy"
    / "patch-maintenance-url-map.py"
)
SPEC = importlib.util.spec_from_file_location("patch_maintenance_url_map", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_patch_scopes_maintenance_policy_to_annotation_host() -> None:
    document = {
        "name": "imu-annotation-map",
        "defaultService": "annotation-service",
        "hostRules": [
            {"hosts": ["upload.imu.kscii.tech"], "pathMatcher": "upload"},
        ],
        "pathMatchers": [
            {"name": "upload", "defaultService": "upload-service"},
        ],
    }

    patched = MODULE.patch_url_map(
        document,
        host="imu.kscii.tech",
        matcher_name="annotation-maintenance",
        application_service="annotation-service",
        error_service="maintenance-bucket",
        error_path="/maintenance.html",
        fingerprint="current-fingerprint",
    )

    assert {tuple(item["hosts"]): item["pathMatcher"] for item in patched["hostRules"]} == {
        ("upload.imu.kscii.tech",): "upload",
        ("imu.kscii.tech",): "annotation-maintenance",
    }
    upload = next(item for item in patched["pathMatchers"] if item["name"] == "upload")
    assert upload == {"name": "upload", "defaultService": "upload-service"}
    annotation = next(
        item for item in patched["pathMatchers"]
        if item["name"] == "annotation-maintenance"
    )
    assert annotation["defaultService"] == "annotation-service"
    assert patched["fingerprint"] == "current-fingerprint"
    assert annotation["defaultCustomErrorResponsePolicy"] == {
        "errorResponseRules": [
            {"matchResponseCodes": ["5xx"], "path": "/maintenance.html"}
        ],
        "errorService": "maintenance-bucket",
    }


def test_patch_is_idempotent_and_splits_a_shared_host_rule() -> None:
    document = {
        "hostRules": [
            {
                "hosts": ["imu.kscii.tech", "upload.imu.kscii.tech"],
                "pathMatcher": "old",
            }
        ],
        "pathMatchers": [{"name": "old", "defaultService": "old-service"}],
    }
    kwargs = {
        "host": "imu.kscii.tech",
        "matcher_name": "annotation-maintenance",
        "application_service": "annotation-service",
        "error_service": "maintenance-bucket",
        "error_path": "/maintenance.html",
    }

    once = MODULE.patch_url_map(document, **kwargs)
    twice = MODULE.patch_url_map(once, **kwargs)

    assert twice == once
    assert twice["hostRules"] == [
        {"hosts": ["upload.imu.kscii.tech"], "pathMatcher": "old"},
        {"hosts": ["imu.kscii.tech"], "pathMatcher": "annotation-maintenance"},
    ]
