"""Exercise release switching with fake systemd and health endpoints."""

import os
import re
import subprocess
from pathlib import Path

import pytest


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


@pytest.mark.parametrize("mode", ["enabled", "disabled", "maintenance", "failure", "no-key"])
def test_deployment_coordinates_external_worker(tmp_path, mode):
    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "host"
    releases = root / "opt/imu-annotation/releases"
    old, new = "a" * 40, "b" * 40
    for revision in (old, new):
        release = releases / revision
        executable(release / ".venv/bin/imu-annotation", "#!/bin/sh\nexit 0\n")
        executable(release / ".venv/bin/imu-upload-broker", "#!/bin/sh\nexit 0\n")
        (release / ".deployment-ready").write_text(revision + "\n")
        units = release / "configs/systemd"
        units.mkdir(parents=True)
        for unit in (
            "imu-annotation.service", "imu-annotation-gc.service", "imu-annotation-gc.timer",
            "imu-upload-broker.service", "imu-external-devices.service",
        ):
            (units / unit).write_text("[Unit]\n")
    current = root / "opt/imu-annotation/current"
    current.symlink_to(releases / old)
    units = root / "etc/systemd/system"
    units.mkdir(parents=True)
    (units / "imu-external-devices.service").write_text("[Unit]\n")
    config = root / "etc/imu-annotation"
    config.mkdir(parents=True)
    if mode != "no-key":
        (config / "external-devices.env").write_text("fixture-only\n")
    (root / "usr/local/sbin").mkdir(parents=True)
    executable(root / "opt/imu-annotation/python/cpython-3.12.1-linux-x86_64-gnu/bin/python3.12",
               "#!/bin/sh\necho 'Python 3.12.1'\n")
    binary = tmp_path / "bin"
    log = tmp_path / "systemctl.log"
    executable(binary / "systemctl", '#!/bin/sh\necho "$* $(readlink "$CURRENT")" >> "$LOG"\n')
    executable(binary / "runuser", """#!/bin/sh
case "$*" in
  *external_devices*) test "$MODE" != disabled ;;
  *) exit 0 ;;
esac
""")
    executable(binary / "curl", '#!/bin/sh\ntest "$MODE" != failure\n')
    executable(binary / "sleep", "#!/bin/sh\nexit 0\n")
    # The release is already prepared; only the validated activation path runs.
    executable(binary / "sha256sum", '#!/bin/sh\necho "' + "c" * 64 + '  $1"\n')
    executable(binary / "tar", "#!/bin/sh\nexit 0\n")
    bundle = root / f"tmp/imu-annotation-{new}.tar.gz"
    bundle.parent.mkdir(parents=True)
    bundle.touch()
    script = (repo / "scripts/deploy/imu-annotation-deploy").read_text()
    script = re.sub(r"/(opt|etc|var|usr|tmp)/", lambda match: str(root) + match[0], script)
    local_script = tmp_path / "deploy"
    local_script.write_text(script)
    result = subprocess.run(
        ["bash", str(local_script), str(bundle), new, "c" * 64,
         "true" if mode == "maintenance" else "false"],
        env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "MODE": mode,
             "CURRENT": str(current), "LOG": str(log)},
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == (1 if mode in {"failure", "no-key"} else 0), result.stderr
    assert current.resolve().name == (old if mode in {"failure", "no-key"} else new)
    calls = log.read_text().splitlines() if log.exists() else []
    stops = [i for i, line in enumerate(calls) if line.startswith("stop imu-external-devices")]
    starts = [i for i, line in enumerate(calls) if line.startswith("restart imu-external-devices")]
    if mode == "no-key":
        assert not calls
    else:
        assert calls[stops[0]].endswith(old)
        if mode in {"maintenance", "disabled"}:
            assert not starts
        else:
            assert stops[0] < starts[0] and calls[starts[0]].endswith(new)
            if mode == "failure":
                assert starts[0] < stops[1] < starts[1]
                assert calls[starts[1]].endswith(old)
