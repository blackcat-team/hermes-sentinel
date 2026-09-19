"""Canonical Hermes Sentinel verification entrypoint.

Runs exactly the repository verification gates documented in
docs/DEVELOPMENT.md, in their documented order:

    python -m pytest -q
    python -m compileall src
    python -m ruff check .
    python -m mypy src
    python -m pip check

Contract:

- every gate executes with the active interpreter (sys.executable)
  from the repository root;
- deterministic check order, first failure stops and exits non-zero;
- successful completion of every gate exits zero;
- no repository mutation beyond the gates' own ignored artifacts
  (__pycache__, tool caches); no tracked files or Git state are touched;
- no network, no deployment, no Telegram, no monitored-host access, and
  no secret requirements;
- gates are never skipped because a tool happens to be missing: absent
  canonical tooling is an environment failure (install
  requirements-dev.txt), surfaced as a failed gate;
- gate output is surfaced, never swallowed;
- usable locally and in GitHub Actions.

Usage:

    python tools/verify.py
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

Check = tuple[str, ...]
Executor = Callable[[list[str]], "tuple[int, str]"]

CHECKS: tuple[Check, ...] = (
    ("-m", "pytest", "-q"),
    ("-m", "compileall", "src"),
    ("-m", "ruff", "check", "."),
    ("-m", "mypy", "src"),
    ("-m", "pip", "check"),
)


def gate_argv(check: Check) -> list[str]:
    """Full command line of one canonical gate."""
    return [sys.executable, *check]


def real_executor(argv: list[str]) -> tuple[int, str]:
    """Run one gate from the repository root with the active interpreter."""
    try:
        proc = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return 127, f"failed to start {argv[0]}: {exc}\n"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def verify(checks: Sequence[Check] = CHECKS, executor: Executor = real_executor) -> int:
    """Run the canonical gates in order; stop at the first failure."""
    for check in checks:
        argv = gate_argv(check)
        label = " ".join(("python", *check))
        print(f"[verify] run: {label}")
        code, output = executor(argv)
        if output.strip():
            print(output.rstrip())
        if code != 0:
            print(f"[verify] FAILED (exit {code}): {label}")
            return 1
        print(f"[verify] ok: {label}")
    print("[verify] all canonical checks passed")
    return 0


def main() -> int:
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
