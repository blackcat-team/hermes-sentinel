"""Check the Stage F2 drop-in separately from the frozen E9 service."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import test_central_systemd_packaging as e9

UNIT_NAME = e9.SERVICE_UNIT_NAME
UNIT = e9.SERVICE_FILE
DROP_IN = e9.PACKAGING / f"{UNIT_NAME}.d" / "10-hardening.conf"


def test_drop_in_adds_only_confinement() -> None:
    base = e9._parse_unit(UNIT.read_text())["Service"]
    hardening = e9._parse_unit(DROP_IN.read_text())
    assert hardening == {
        "Service": {
            "ProtectSystem": ["strict"],
            "ProtectHome": ["yes"],
            "PrivateDevices": ["yes"],
            "ProtectKernelTunables": ["yes"],
            "ProtectControlGroups": ["yes"],
            "ReadOnlyPaths": ["/tmp /var/tmp -/dev/shm"],
            "CapabilityBoundingSet": [""],
            "AmbientCapabilities": [""],
            "RestrictNamespaces": ["yes"],
            "SystemCallArchitectures": ["native"],
            "RestrictAddressFamilies": ["AF_UNIX AF_INET AF_INET6"],
        },
    }
    assert base.keys().isdisjoint(hardening["Service"])
    assert base["StateDirectory"] == ["hermes-sentinel"]
    assert base["PrivateTmp"] == ["yes"]
    assert base["NoNewPrivileges"] == ["yes"]


@pytest.mark.skipif(
    os.environ.get("HERMES_STAGE_F_LIVE") != "1",
    reason="requires systemd-analyze and the installed entrypoint in the Stage F Linux lab",
)
def test_systemd_analyzes_merged_unit_and_rejects_wrong_execstart(tmp_path: Path) -> None:
    units = tmp_path / "etc/systemd/system"
    drop_ins = units / f"{UNIT_NAME}.d"
    drop_ins.mkdir(parents=True)
    unit = shutil.copyfile(UNIT, units / UNIT_NAME)
    shutil.copyfile(DROP_IN, drop_ins / DROP_IN.name)

    rendered = subprocess.check_output(
        [
            "systemd-analyze", f"--root={tmp_path}", "cat-config",
            f"systemd/system/{UNIT_NAME}",
        ],
        stderr=subprocess.PIPE, text=True, timeout=10,
    )
    assert UNIT.read_text().rstrip() in rendered
    assert DROP_IN.read_text().rstrip() in rendered
    print("systemd cat-config: exact E9 service and F2 drop-in loaded")

    command = ["systemd-analyze", "--recursive-errors=yes", "verify", str(unit)]
    verified = subprocess.run(command, text=True, capture_output=True, timeout=10)
    assert (verified.returncode, verified.stderr) == (0, "")
    print("systemd-analyze verify: effective unit accepted with installed entrypoint")

    (drop_ins / "90-invalid.conf").write_text(
        "[Service]\nExecStart=\nExecStart=/nonexistent/stage-f-negative-control\n"
    )
    rejected = subprocess.run(command, text=True, capture_output=True, timeout=10)
    assert rejected.returncode != 0
    assert "stage-f-negative-control" in rejected.stderr
    print("systemd-analyze verify: negative control with wrong ExecStart rejected")
