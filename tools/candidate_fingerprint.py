"""Deterministic, read-only fingerprint of the effective candidate Git tree.

The fingerprint proves that the candidate reviewed by QA is exactly the
candidate later accepted and promoted: identical effective candidate
trees on an identical base always yield the identical hash, while any
repository-significant difference in path, presence, content, or Git
mode/type yields a different hash.

Effective candidate semantics
-----------------------------
The fingerprint identifies the EFFECTIVE candidate Git tree relative to
BASE_HEAD: the tree that would be committed from the current state,
independent of whether an unchanged candidate modification currently
resides in the working tree, the index, or a combination of both.
Staging alone therefore never changes the fingerprint. The effective
entries are derived WITHOUT mutating the index, from:

- the index (`git ls-files -s`) for tracked paths and index-resident
  mode/type facts;
- the working tree (non-ignored untracked files via
  `git ls-files --others --exclude-standard`, and file bytes for all
  present paths);
- the BASE_HEAD tree (`git ls-tree -r`) for deletion identity.

Entries whose effective (mode, content id) equals the BASE_HEAD entry
are omitted (pure diff semantics); a base path without effective
presence is recorded as an explicit deletion. Renames and copies need
no special record parsing: they are represented as their effective
trees (delete-old + add-new, or add-new only), which is inherently
staging-invariant and free of old/new path ambiguity.

Git-cleaned blob identity
-------------------------
For regular blobs the content identity is NOT the raw working-tree
byte hash: it is the PATH-AWARE GIT-CLEANED blob object id that an
exact-path ``git add`` of that file would stage, computed read-only via
``git hash-object --path=<repo-relative-path> --stdin`` (never ``-w``).
This applies the repository's own clean/filter rules — text
normalization, ``core.autocrlf``, and ``.gitattributes`` conversions —
exactly as normal staging would. Symlink blobs are never clean-filtered
by Git and are hashed from the link target string directly.

If a path's attributes require an EXTERNAL clean filter
(``git check-attr filter`` set to anything other than ``unspecified``),
the transformation cannot be safely or deterministically evaluated
read-only, and the tool FAILS CLOSED instead of silently hashing the
wrong bytes.

Git mode/type identity
----------------------
Every present entry records its canonical Git entry mode/type:

- 100644 regular non-executable blob
- 100755 regular executable blob
- 120000 symbolic link
- 160000 gitlink (submodule commit)

Effective mode/type rules:

- a working-tree symlink is mode 120000 and its content identity is
  the link target string (never the dereferenced target file);
- an index-defined 120000 entry defines type 120000 even when the
  working tree holds a regular file or no file at all (Git-native
  symlink construction; identity then comes from the index blob);
- executability is the union of the filesystem x-bit (only when the
  repository trusts filesystem mode, i.e. core.filemode true) and a
  staged 100755 index entry, so staged mode changes remain detectable
  on every platform;
- an INITIALIZED gitlink (nested repository with ``.git``) uses the
  checked-out nested repository HEAD that ``git add <gitlink-path>``
  would stage — a nested HEAD advance changes the fingerprint BEFORE
  outer staging, and dirty nested working-tree contents are never part
  of outer gitlink identity (outer Git commits only the nested HEAD
  OID);
- an UNINITIALIZED gitlink (directory present, no ``.git``) retains the
  outer index/base gitlink identity, matching what Git considers the
  staggable state;
- a registered gitlink whose working-tree directory is entirely MISSING
  fails closed: the index still holds the entry while Git reports a
  worktree deletion, so exact identity is genuinely ambiguous.

Excluded from identity: mtimes, ownership, filesystem inode ids,
absolute workspace paths, command timestamps, ignored artifacts, and
Git internals.

Read-only hard contract
-----------------------
Only Git read commands are invoked, with GIT_OPTIONAL_LOCKS=0 (which
also prevents implicit index refresh). The tool never stages, commits,
updates the index, checks out, restores, writes refs, mutates candidate
files, or touches nested-repository state (the nested repository is
only queried with ``rev-parse``).

Fail-closed conditions: invalid/missing base, base not an ancestor of
HEAD, unmerged index entries, unsupported object format, external clean
filters, unreadable or stat-failing candidate paths, a gitlink entry
under a regular working-tree file, a missing gitlink working-tree
directory, an unresolvable initialized nested HEAD, or malformed
plumbing output. Candidate paths are never silently omitted.

Usage:

    python tools/candidate_fingerprint.py <BASE_HEAD> [--repo PATH]

Output (exact, machine-readable, two lines on stdout):

    CANDIDATE_DIFF_HASH:
    sha256:<64 lowercase hex chars>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

FORMAT_VERSION = b"candidate-fingerprint 3"
REGULAR_MODE = "100644"
EXECUTABLE_MODE = "100755"
SYMLINK_MODE = "120000"
GITLINK_MODE = "160000"
_HEX = frozenset(b"0123456789abcdef")


class FingerprintError(Exception):
    """Raised when a deterministic fingerprint cannot be produced."""


def _git(
    repo: str, *args: str, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run a Git command with optional locks disabled (read-only)."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        input=input_bytes,
        env=env,
        check=False,
    )


def _git_bytes(repo: str, *args: str, input_bytes: bytes | None = None) -> bytes:
    proc = _git(repo, *args, input_bytes=input_bytes)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise FingerprintError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {detail}"
        )
    return proc.stdout


def _display(path: bytes) -> str:
    return path.decode("utf-8", "replace")


def resolve_base(repo: str, expected: str) -> str:
    """Resolve the expected BASE_HEAD to a full commit SHA, fail closed."""
    inside = _git_bytes(repo, "rev-parse", "--is-inside-work-tree").strip()
    if inside != b"true":
        raise FingerprintError(f"{repo!r} is not a Git working tree")
    try:
        sha = (
            _git_bytes(repo, "rev-parse", "--verify", f"{expected}^{{commit}}")
            .decode("ascii")
            .strip()
        )
    except FingerprintError as exc:
        raise FingerprintError(f"invalid or missing BASE_HEAD {expected!r}: {exc}") from exc
    if _git(repo, "merge-base", "--is-ancestor", sha, "HEAD").returncode != 0:
        raise FingerprintError(
            f"BASE_HEAD {sha} is not an ancestor of HEAD; "
            "the candidate/base relationship is ambiguous"
        )
    return sha


def repo_root(repo: str) -> str:
    """Resolve the working-tree root so attribute paths are repo-relative."""
    root = os.fsdecode(_git_bytes(repo, "rev-parse", "--show-toplevel").strip())
    if not root:
        raise FingerprintError("cannot resolve repository working-tree root")
    return root


def object_format(root: str) -> str:
    """Return the repository object format ('sha1' or 'sha256')."""
    fmt = _git_bytes(root, "rev-parse", "--show-object-format").decode("ascii").strip()
    if fmt not in ("sha1", "sha256"):
        raise FingerprintError(f"unsupported repository object format {fmt!r}")
    return fmt


def blob_hex_id(data: bytes, objfmt: str) -> str:
    """Return the Git blob object id of ``data`` in the repository format.

    Used only for content Git never clean-filters (symlink targets).
    """
    return hashlib.new(objfmt, b"blob %d\x00" % len(data) + data).hexdigest()


def read_index(root: str) -> dict[bytes, tuple[str, str]]:
    """Map index paths to (mode, object id); fail closed on unmerged entries."""
    raw = _git_bytes(root, "ls-files", "-s", "-z")
    entries: dict[bytes, tuple[str, str]] = {}
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, tab, path = record.partition(b"\t")
        parts = meta.split(b" ")
        if not tab or len(parts) != 3:
            raise FingerprintError("malformed git ls-files -s record")
        mode, object_id, stage = parts
        if stage != b"0":
            raise FingerprintError(
                "index contains unmerged entries; "
                "candidate enumeration would not be deterministic"
            )
        entries[path] = (mode.decode("ascii"), object_id.decode("ascii"))
    return entries


def read_untracked(root: str) -> list[bytes]:
    """List non-ignored untracked working-tree paths."""
    raw = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    return [token for token in raw.split(b"\x00") if token]


def read_base_tree(root: str, base: str) -> dict[bytes, tuple[str, str]]:
    """Map BASE_HEAD tree paths to (mode, object id) for deletion identity."""
    raw = _git_bytes(root, "ls-tree", "-r", "-z", base)
    entries: dict[bytes, tuple[str, str]] = {}
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, tab, path = record.partition(b"\t")
        parts = meta.split(b" ")
        if not tab or len(parts) != 3:
            raise FingerprintError("malformed git ls-tree record")
        mode, _kind, object_id = parts
        entries[path] = (mode.decode("ascii"), object_id.decode("ascii"))
    return entries


def filemode_trusted(root: str) -> bool:
    """Does this repository honor filesystem mode bits (core.filemode)?"""
    proc = _git(root, "config", "--type=bool", "core.filemode")
    value = proc.stdout.strip()
    if proc.returncode == 0 and value in (b"true", b"false"):
        return value == b"true"
    return sys.platform != "win32"


def _assert_no_external_clean_filters(root: str, paths: list[bytes]) -> None:
    """Fail closed when any path requires an external clean filter."""
    stdin = b"\x00".join(paths) + b"\x00"
    raw = _git_bytes(root, "check-attr", "--stdin", "-z", "filter", input_bytes=stdin)
    tokens = raw.split(b"\x00")
    if len(tokens) < 1 or (len(tokens) - 1) % 3 != 0:
        raise FingerprintError("malformed git check-attr output")
    for index in range(0, len(tokens) - 1, 3):
        path, _attr, value = tokens[index], tokens[index + 1], tokens[index + 2]
        if value != b"unspecified":
            raise FingerprintError(
                f"path {_display(path)!r} requires an external clean filter "
                f"({value.decode('utf-8', 'replace')}); refusing to fingerprint "
                "transformed bytes that cannot be evaluated deterministically"
            )


def _clean_blob_id(root: str, path: bytes) -> str:
    """Git-cleaned, path-aware blob id exactly as exact-path ``git add``
    of this working-tree file would stage it (read-only; never ``-w``)."""
    content = _read_candidate_bytes(root, path)
    try:
        proc = _git(
            root, "hash-object", f"--path={_display(path)}", "--stdin",
            input_bytes=content,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise FingerprintError(f"cannot hash candidate file {_display(path)!r}: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise FingerprintError(f"git hash-object failed for {_display(path)!r}: {detail}")
    value = proc.stdout.decode("ascii", "replace").strip()
    encoded = value.encode("ascii", "replace")
    if len(value) not in (40, 64) or not all(byte in _HEX for byte in encoded):
        raise FingerprintError(f"malformed git hash-object output for {_display(path)!r}")
    return value


def _lstat(root: str, path: bytes) -> os.stat_result | None:
    """lstat a candidate path; absent is None, other errors fail closed."""
    try:
        return os.lstat(Path(root) / os.fsdecode(path))
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise FingerprintError(f"cannot stat candidate path {_display(path)!r}: {exc}") from exc


def _read_candidate_bytes(root: str, path: bytes) -> bytes:
    try:
        return (Path(root) / os.fsdecode(path)).read_bytes()
    except OSError as exc:
        raise FingerprintError(f"cannot read candidate file {_display(path)!r}: {exc}") from exc


def _read_link_target(root: str, path: bytes) -> bytes:
    """Return the symlink target string bytes without dereferencing it."""
    try:
        return os.fsencode(os.readlink(Path(root) / os.fsdecode(path)))
    except OSError as exc:
        raise FingerprintError(f"cannot read symlink target {_display(path)!r}: {exc}") from exc


def _nested_head(root: str, path: bytes) -> str | None:
    """Checked-out HEAD of an initialized nested repository, or None.

    Read-only (``rev-parse`` only): the nested repository is never
    mutated. Fails closed when an initialized nested HEAD cannot be
    resolved (for example an unborn branch).
    """
    nested = Path(root) / os.fsdecode(path)
    if not (nested / ".git").exists():
        return None
    proc = _git(str(nested), "rev-parse", "HEAD")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise FingerprintError(
            f"cannot resolve initialized nested repository HEAD for {_display(path)!r}: {detail}"
        )
    return proc.stdout.decode("ascii").strip()


def effective_entries(
    root: str,
    index: dict[bytes, tuple[str, str]],
    untracked: list[bytes],
    trusted_filemode: bool,
    objfmt: str,
) -> dict[bytes, tuple[str, str]]:
    """Map every effectively-present path to (effective Git mode, content id).

    Deterministic: input paths are processed in sorted raw-byte order.
    Raises FingerprintError on states that would silently omit or
    ambiguize a candidate path.
    """
    entries: dict[bytes, tuple[str, str]] = {}
    regular_paths: dict[bytes, bool] = {}
    for path in sorted(set(index) | set(untracked)):
        st = _lstat(root, path)
        index_mode, index_id = index.get(path, ("", ""))
        if st is not None and stat.S_ISLNK(st.st_mode):
            # Real working-tree symlink: identity is the target string.
            entries[path] = (SYMLINK_MODE, blob_hex_id(_read_link_target(root, path), objfmt))
        elif st is not None and stat.S_ISREG(st.st_mode):
            if index_mode == SYMLINK_MODE:
                # Index-defined symlink (Git-native construction; on
                # core.symlinks=false hosts the working-tree regular
                # file holds the link-target text). Symlink blobs are
                # never clean-filtered.
                entries[path] = (SYMLINK_MODE, blob_hex_id(_read_candidate_bytes(root, path), objfmt))
            elif index_mode == GITLINK_MODE:
                raise FingerprintError(
                    f"gitlink entry {_display(path)!r} has a regular working-tree file"
                )
            else:
                executable = (trusted_filemode and bool(st.st_mode & 0o111)) or (
                    index_mode == EXECUTABLE_MODE
                )
                regular_paths[path] = executable
        elif st is not None and stat.S_ISDIR(st.st_mode):
            if index_mode == GITLINK_MODE:
                # Initialized: the nested checked-out HEAD is what an
                # exact-path git add would stage. Uninitialized (no
                # .git): the outer index identity is the staggable
                # state.
                nested_id = _nested_head(root, path)
                entries[path] = (GITLINK_MODE, nested_id if nested_id is not None else index_id)
            # Any other tracked path replaced by a directory is an
            # effective deletion; directory contents surface through
            # the untracked enumeration.
        elif index_mode == SYMLINK_MODE:
            # Index-resident symlink with no working-tree file: the
            # index is its only representation.
            entries[path] = (SYMLINK_MODE, index_id)
        elif index_mode == GITLINK_MODE:
            # A registered gitlink with an entirely missing working-tree
            # directory: the index still holds the entry while Git
            # reports a worktree deletion — genuinely ambiguous.
            raise FingerprintError(
                f"gitlink {_display(path)!r} has no working-tree directory; "
                "exact identity is ambiguous (index entry vs worktree deletion)"
            )
        # Any other path without working-tree presence is effectively
        # deleted (or was a staged-then-removed addition: contributes
        # nothing to the diff).
    if regular_paths:
        _assert_no_external_clean_filters(root, sorted(regular_paths))
        for path, executable in regular_paths.items():
            mode = EXECUTABLE_MODE if executable else REGULAR_MODE
            entries[path] = (mode, _clean_blob_id(root, path))
    return entries


def _field(data: bytes) -> bytes:
    """Length-prefixed binary field (unambiguous concatenation)."""
    return len(data).to_bytes(8, "big") + data


def build_representation(
    base: str,
    objfmt: str,
    deleted: list[bytes],
    files: dict[bytes, tuple[str, str]],
) -> bytes:
    """Build the stable byte representation that is hashed.

    Entries are sorted by raw path bytes. Deletions carry an explicit
    marker; present entries carry their effective Git mode/type and
    Git-cleaned content identity. The BASE_HEAD and object format are
    part of the identity.
    """
    chunks = [
        FORMAT_VERSION,
        b"\x00",
        b"base ",
        base.encode("ascii"),
        b"\x00",
        b"objfmt ",
        objfmt.encode("ascii"),
        b"\x00",
    ]
    for path in sorted(set(deleted) | set(files)):
        entry = files.get(path)
        if entry is None:
            chunks.append(b"D" + _field(path))
        else:
            mode, content_id = entry
            chunks.append(b"F" + _field(path) + mode.encode("ascii") + _field(content_id.encode("ascii")))
    return b"".join(chunks)


def fingerprint(repo: str, expected_base: str) -> str:
    """Compute the deterministic candidate diff fingerprint hex digest."""
    base = resolve_base(repo, expected_base)
    root = repo_root(repo)
    objfmt = object_format(root)
    index = read_index(root)
    untracked = read_untracked(root)
    base_tree = read_base_tree(root, base)
    present = effective_entries(root, index, untracked, filemode_trusted(root), objfmt)
    files = {path: entry for path, entry in present.items() if base_tree.get(path) != entry}
    deleted = [path for path in base_tree if path not in present]
    representation = build_representation(base, objfmt, deleted, files)
    return hashlib.sha256(representation).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute the deterministic, read-only candidate diff fingerprint."
    )
    parser.add_argument(
        "base",
        help="expected BASE_HEAD (any committish; resolved to its full SHA)",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="path inside the candidate Git working tree (default: cwd)",
    )
    args = parser.parse_args(argv)
    try:
        digest = fingerprint(args.repo, args.base)
    except FingerprintError as exc:
        print(f"candidate_fingerprint: error: {exc}", file=sys.stderr)
        return 2
    print("CANDIDATE_DIFF_HASH:")
    print(f"sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
