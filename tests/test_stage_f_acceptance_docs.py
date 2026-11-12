"""Exercise the Stage F runbook checks on disposable synthetic data."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import test_http_server as b5

ROOT = Path(__file__).resolve().parents[1]
HARDENING = ROOT / "docs/PRODUCTION_HARDENING.md"
CENTRAL = ROOT / "docs/SENTINEL_DEPLOYMENT.md"
REPORTER = ROOT / "docs/REPORTER_DEPLOYMENT.md"


def _runbook_block(path: Path, label: str, language: str) -> str:
    match = re.search(
        rf"<!-- {label} -->\n```{language}\n(.*?)\n```",
        path.read_text(), re.DOTALL,
    )
    assert match is not None, f"missing {label} in {path.name}"
    return match[1]


def test_sha_preflight_rejects_drift_and_allows_verified_rollback(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=tmp_path, text=True, timeout=10,
        ).strip()

    def preflight(sha: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", _runbook_block(HARDENING, "stage-f-sha-preflight", "bash")],
            env={**os.environ, "APPROVED_SHA": sha}, cwd=tmp_path,
            capture_output=True, text=True, timeout=10,
        )

    git("init", "-q", "--template=")
    git("config", "user.name", "Synthetic operator")
    git("config", "user.email", "operator@example.invalid")
    git("commit", "-qm", "Previous approved revision", "--allow-empty")
    previous = git("rev-parse", "HEAD")
    git("commit", "-qm", "Current approved revision", "--allow-empty")
    current = git("rev-parse", "HEAD")

    assert preflight(current).returncode == 0
    assert preflight("").returncode != 0
    assert preflight(previous).returncode != 0
    dirty = tmp_path / "unreviewed"
    dirty.touch()
    assert preflight(current).returncode != 0
    dirty.unlink()
    git("checkout", "-q", "--detach", previous)
    assert preflight(previous).returncode == 0


def test_sqlite_observation_is_per_node_redacted_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = b5.HttpServerTestCase()
    try:
        backend.setUp()
        assert b5._parse_response(backend._exchange(backend._request_bytes()))[0] == 204
        for name, value in (
            ("PYTHONPATH", str(ROOT / "src")),
            ("STAGE_F_TEST_PYTHON", sys.executable),
            ("STAGE_F_TEST_DB", str(backend.db_path)),
        ):
            monkeypatch.setenv(name, value)

        script = _runbook_block(CENTRAL, "stage-f-sqlite-observation", "bash")
        # Substitute only sudo and its two installation paths in this lab.
        sudo = 'sudo() { "$STAGE_F_TEST_PYTHON" "$2" "$3" "$STAGE_F_TEST_DB"; }\n'

        def observe(node: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", "-c", sudo + script],
                input=node + "\n", capture_output=True, text=True, timeout=10,
            )

        before = backend.db_path.read_bytes()
        accepted = observe("vds-01")
        assert (accepted.returncode, accepted.stdout.strip(), accepted.stderr) == (
            0, f"rows=1 latest_received_at={b5._RECEIVED_AT.isoformat()}", "",
        )
        absent = observe("vds-02")
        assert (absent.returncode, absent.stdout.strip(), absent.stderr) == (
            0, "rows=0 latest_received_at=none", "",
        )
        assert backend.db_path.read_bytes() == before
        assert observe("").returncode != 0
        missing = tmp_path / "missing.sqlite3"
        monkeypatch.setenv("STAGE_F_TEST_DB", str(missing))
        assert observe("vds-01").returncode != 0
        assert not missing.exists()
    finally:
        backend.doCleanups()


def test_evidence_template_opens_each_gate_and_links_operator_procedures() -> None:
    evidence = json.loads(_runbook_block(HARDENING, "stage-f-evidence-template", "json"))
    assert evidence["ci"] == {
        "workflow": "CI", "job": "verify", "run_id": None, "url": None,
        "head_sha": None, "result": "open",
    }
    assert evidence["mvp_runtime"] == evidence["stage_f"] == {
        "status": "open", "reviewer_id": None,
    }
    assert evidence["h2"] == {"status": "open", "evidence_id": None}
    assert evidence["observations"] == []
    assert set(evidence["observation_fields"]) == {
        "check", "utc", "source_id", "expected", "actual", "artifact_id", "sha256",
    }
    assert evidence["rollback"]["outcome"] == "open"

    hardening = HARDENING.read_text()
    assert all(f"]({link})" in hardening for link in (
        CENTRAL.name, REPORTER.name, "DEADMAN_DEPLOYMENT.md",
    ))
    assert f"]({HARDENING.name})" in CENTRAL.read_text()
    assert f"]({HARDENING.name})" in REPORTER.read_text()
