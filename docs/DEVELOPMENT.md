# Development

## Runtime

Python 3.11.

Canonical interpreter:

E:/PythonProject/Scripts/Hermes-sentinel/.venv/Scripts/python.exe

## Environment

Create the local virtual environment:

```powershell
py -3.11 -m venv .venv
```

Install the repository-defined development dependencies — the exact
canonical tooling required by the verification gates below:

```powershell
E:/PythonProject/Scripts/Hermes-sentinel/.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` pins the canonical development tooling exactly
(pytest==8.4.2, ruff==0.16.8, mypy==1.20.2). It adds no runtime
dependencies and no unrelated packages. Do not replace it with ad-hoc
local installs.

## Verification

The repository-native canonical verification command is:

```powershell
E:/PythonProject/Scripts/Hermes-sentinel/.venv/Scripts/python.exe tools/verify.py
```

`tools/verify.py` executes EXACTLY, in order, stopping at the first
failure:

- `python -m pytest -q`
- `python -m compileall src`
- `python -m ruff check .`
- `python -m mypy src`
- `python -m pip check`

All five gates are mandatory; none is optional, and none is skipped
when a tool is missing (a missing tool is an environment failure — fix
it with the `requirements-dev.txt` install above). CI
(`.github/workflows/ci.yml`) runs exactly this entrypoint.

Gate scopes, and why:

- Ruff rules are explicitly selected in `pyproject.toml`
  (`[tool.ruff.lint] select = ["E4", "E7", "E9", "F"]`) so CI does not
  inherit release-dependent Ruff defaults; the selected set preserves
  the repository's historical correctness-oriented contract.
- mypy is canonical for production source under `src/` (`python -m
  mypy src`, mirrored by `[tool.mypy] files = ["src"]`).
- Tests are fully runtime-verified by pytest; the accepted test suite
  is not claimed to have a historical mypy-clean baseline, and mypy
  debt in tests is not masked by ignores or exclusions.

## Candidate fingerprint

Compute the deterministic, read-only identity of the effective
candidate tree relative to a base commit:

```powershell
E:/PythonProject/Scripts/Hermes-sentinel/.venv/Scripts/python.exe tools/candidate_fingerprint.py <BASE_HEAD> --repo .
```

Exact output (two machine-readable lines):

```
CANDIDATE_DIFF_HASH:
sha256:<64 lowercase hex chars>
```

Workflow invariant: the same BASE_HEAD + CANDIDATE_DIFF_HASH must be
preserved across

- DEV READY_FOR_QA;
- independent QA (recomputes the fingerprint against the same BASE_HEAD
  and reviews that exact candidate);
- Architect acceptance;
- Operator promotion precheck.

Staging alone must not change the hash: the fingerprint binds the
effective candidate tree (tracked content, non-ignored untracked
additions, deletions, mode/type, path-aware Git-cleaned blob identity),
never the staging state.
