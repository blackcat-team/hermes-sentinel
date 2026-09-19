"""Deterministic tests for tools/candidate_fingerprint.py.

Each test builds a throwaway Git repository under a per-test temporary
directory, applies a controlled candidate change, and asserts on the
tool's fingerprint contract: effective-candidate (staging-invariant)
identity, Git mode/type identity (100644/100755/120000/160000), symlink
non-dereference, determinism, sensitivity to material changes,
insensitivity to ignored artifacts and staging, exact output format,
read-only behavior, and fail-closed error handling.

Mode/type and symlink constructions use Git-native index operations
(update-index --chmod / --cacheinfo, hash-object) so proofs do not
depend on Windows chmod or symlink permissions.

Adapted from the accepted Funding-cut-farmer implementation to the
Hermes Sentinel repository: these focused infrastructure tests remain
runnable with the stdlib unittest runner (``python -m unittest
discover -s tests``) even though pytest is a canonical verification
gate, so the per-test temporary directory comes from
tempfile.TemporaryDirectory instead of the pytest tmp_path fixture,
and platform-restricted symlinks skip through unittest's skipTest.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "candidate_fingerprint.py"
HASH_LINE = re.compile(r"^sha256:([0-9a-f]{64})$", re.MULTILINE)


def _git_env(home: Path) -> dict[str, str]:
    """Hermetic Git environment for deterministic commits.

    Redirects HOME/USERPROFILE to an empty directory (no global config),
    disables the system config, and pins author/committer timestamps so
    that identical content produces identical commit SHAs across
    independently created test repositories.
    """
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_AUTHOR_DATE"] = "@0 +0000"
    env["GIT_COMMITTER_DATE"] = "@0 +0000"
    return env


def git(
    repo: Path, *args: str, home: Path, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        input=input_bytes,
        env=_git_env(home),
        check=False,
    )


def git_ok(repo: Path, *args: str, home: Path, input_bytes: bytes | None = None) -> None:
    proc = git(repo, *args, home=home, input_bytes=input_bytes)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def write(repo: Path, rel: str, data: bytes) -> Path:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def commit_index(repo: Path, message: str, home: Path) -> None:
    """Commit exactly the current index (no implicit staging)."""
    git_ok(
        repo,
        "-c",
        "user.email=dev@example.invalid",
        "-c",
        "user.name=Dev",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        message,
        home=home,
    )


def commit(repo: Path, message: str, home: Path) -> str:
    git_ok(repo, "add", "-A", home=home)
    commit_index(repo, message, home=home)
    proc = git(repo, "rev-parse", "HEAD", home=home)
    assert proc.returncode == 0
    return proc.stdout.decode("ascii").strip()


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a repository whose base commit has tracked files and ignores."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    init = subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        capture_output=True,
        env=_git_env(home),
        check=False,
    )
    assert init.returncode == 0, init.stderr.decode("utf-8", "replace")
    git_ok(repo, "config", "core.autocrlf", "false", home=home)
    write(repo, "tracked.txt", b"one\n")
    write(repo, "keep.txt", b"keep\n")
    write(repo, "sub/nested.txt", b"nested\n")
    write(repo, ".gitignore", b"ignored.log\n*.pyc\n")
    base = commit(repo, "base", home=home)
    return repo, base


def apply_candidate(repo: Path) -> None:
    """Apply a representative candidate: modify, add, delete, rename."""
    write(repo, "tracked.txt", b"one-modified\n")
    write(repo, "untracked.txt", b"fresh\n")
    (repo / "keep.txt").unlink()
    (repo / "sub" / "nested.txt").rename(repo / "sub" / "moved.txt")


def stage(repo: Path, home: Path) -> None:
    git_ok(repo, "add", "-A", home=home)


def hash_object(repo: Path, data: bytes, home: Path) -> str:
    proc = git(repo, "hash-object", "-w", "--stdin", home=home, input_bytes=data)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout.decode("ascii").strip()


def set_index_entry(repo: Path, mode: str, object_id: str, path: str, home: Path) -> None:
    git_ok(repo, "update-index", "--add", "--cacheinfo", f"{mode},{object_id},{path}", home=home)


def set_index_exec(repo: Path, path: str, executable: bool, home: Path) -> None:
    flag = "+x" if executable else "-x"
    git_ok(repo, "update-index", f"--chmod={flag}", path, home=home)


def remove_index_entry(repo: Path, path: str, home: Path) -> None:
    git_ok(repo, "update-index", "--force-remove", path, home=home)


def index_bytes(repo: Path) -> bytes:
    return (repo / ".git" / "index").read_bytes()


def run_fingerprint(repo: Path, base: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), base, "--repo", str(repo)],
        capture_output=True,
        text=True,
        env=_git_env(tmp_path / "home"),
        check=False,
    )


def hash_of(proc: subprocess.CompletedProcess[str]) -> str:
    match = HASH_LINE.search(proc.stdout)
    assert match is not None, proc.stdout + proc.stderr
    return match.group(1)


def fp(repo: Path, base: str, tmp_path: Path) -> str:
    proc = run_fingerprint(repo, base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    return hash_of(proc)


def home_of(tmp_path: Path) -> Path:
    return tmp_path / "home"


class CandidateFingerprintTests(unittest.TestCase):
    """Fingerprint contract tests; each gets a throwaway directory."""

    def in_temp_dir(self, body: Callable[[Path], None]) -> None:
        with tempfile.TemporaryDirectory() as name:
            body(Path(name))

    # -------------------------------------------------------------------
    # Regression: determinism, diff sensitivity, ignored exclusion,
    # read-only, fail-closed base and index handling.
    # -------------------------------------------------------------------

    def test_identical_content_and_base_yield_identical_hash(self) -> None:
        """Identical candidate content on the same base is hash-identical."""
        def body(tmp_path: Path) -> None:
            repo_a, base_a = make_repo(tmp_path / "a")
            repo_b, base_b = make_repo(tmp_path / "b")
            apply_candidate(repo_a)
            apply_candidate(repo_b)
            proc_a = run_fingerprint(repo_a, base_a, tmp_path / "a")
            proc_b = run_fingerprint(repo_b, base_b, tmp_path / "b")
            assert proc_a.returncode == 0, proc_a.stderr
            assert proc_b.returncode == 0, proc_b.stderr
            assert hash_of(proc_a) == hash_of(proc_b)
            # Repeated execution without content change is also identical.
            assert hash_of(run_fingerprint(repo_a, base_a, tmp_path / "a")) == hash_of(proc_a)
        self.in_temp_dir(body)

    def test_modified_tracked_content_changes_hash(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            before = fp(repo, base, tmp_path)
            write(repo, "tracked.txt", b"one-modified\n")
            after = fp(repo, base, tmp_path)
            assert before != after
        self.in_temp_dir(body)

    def test_added_untracked_file_changes_hash(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            before = fp(repo, base, tmp_path)
            write(repo, "untracked.txt", b"fresh\n")
            after = fp(repo, base, tmp_path)
            assert before != after
        self.in_temp_dir(body)

    def test_modified_untracked_file_changes_hash(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            write(repo, "untracked.txt", b"fresh\n")
            before = fp(repo, base, tmp_path)
            write(repo, "untracked.txt", b"fresh-modified\n")
            after = fp(repo, base, tmp_path)
            assert before != after
        self.in_temp_dir(body)

    def test_deleted_tracked_file_changes_hash(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            before = fp(repo, base, tmp_path)
            (repo / "tracked.txt").unlink()
            after = fp(repo, base, tmp_path)
            assert before != after
        self.in_temp_dir(body)

    def test_renamed_tracked_file_changes_hash(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            before = fp(repo, base, tmp_path)
            (repo / "sub" / "nested.txt").rename(repo / "sub" / "moved.txt")
            after = fp(repo, base, tmp_path)
            assert before != after
        self.in_temp_dir(body)

    def test_ignored_file_does_not_change_hash(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            before = fp(repo, base, tmp_path)
            write(repo, "ignored.log", b"runtime noise\n")
            write(repo, "artifact.pyc", b"\x00\x01binary noise\n")
            after = fp(repo, base, tmp_path)
            assert before == after
        self.in_temp_dir(body)

    def test_different_base_changes_identity(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base_a = make_repo(tmp_path)
            write(repo, "tracked.txt", b"one-evolved\n")
            base_b = commit(repo, "second", home=home_of(tmp_path))
            apply_candidate(repo)
            vs_a = fp(repo, base_a, tmp_path)
            vs_b = fp(repo, base_b, tmp_path)
            assert vs_a != vs_b
        self.in_temp_dir(body)

    def test_fingerprint_leaves_worktree_and_index_unchanged(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            apply_candidate(repo)
            home = home_of(tmp_path)
            status_before = git(repo, "status", "--porcelain", "-z", home=home).stdout
            index_bytes_before = index_bytes(repo)
            proc = run_fingerprint(repo, base, tmp_path)
            assert proc.returncode == 0, proc.stderr
            status_after = git(repo, "status", "--porcelain", "-z", home=home).stdout
            assert status_after == status_before
            assert index_bytes(repo) == index_bytes_before
        self.in_temp_dir(body)

    def test_output_format_is_exact_and_machine_readable(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            apply_candidate(repo)
            proc = run_fingerprint(repo, base, tmp_path)
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout == f"CANDIDATE_DIFF_HASH:\nsha256:{hash_of(proc)}\n"
        self.in_temp_dir(body)

    def test_missing_base_fails_closed(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, _base = make_repo(tmp_path)
            proc = run_fingerprint(
                repo, "1234567890123456789012345678901234567890", tmp_path
            )
            assert proc.returncode != 0
            assert "CANDIDATE_DIFF_HASH" not in proc.stdout
            assert proc.stderr.strip()
        self.in_temp_dir(body)

    def test_non_ancestor_base_fails_closed(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            git_ok(repo, "checkout", "--orphan", "unrelated", home=home)
            write(repo, "other.txt", b"unrelated lineage\n")
            commit(repo, "orphan", home=home)
            proc = run_fingerprint(repo, base, tmp_path)
            assert proc.returncode != 0
            assert "CANDIDATE_DIFF_HASH" not in proc.stdout
            assert "ancestor" in proc.stderr
        self.in_temp_dir(body)

    def test_unmerged_index_fails_closed(self) -> None:
        """A conflicted (unmerged) index fails closed, never guesses."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            git_ok(repo, "checkout", "-b", "side", home=home)
            write(repo, "tracked.txt", b"side-version\n")
            commit(repo, "side", home=home)
            git_ok(repo, "checkout", "main", home=home)
            write(repo, "tracked.txt", b"main-version\n")
            commit(repo, "main-version", home=home)
            merge = git(
                repo,
                "-c",
                "user.email=dev@example.invalid",
                "-c",
                "user.name=Dev",
                "-c",
                "commit.gpgsign=false",
                "merge",
                "side",
                home=home,
            )
            assert merge.returncode != 0
            proc = run_fingerprint(repo, base, tmp_path)
            assert proc.returncode != 0
            assert "CANDIDATE_DIFF_HASH" not in proc.stdout
            assert "unmerged" in proc.stderr
        self.in_temp_dir(body)

    # -------------------------------------------------------------------
    # Mode/type identity (Git-native constructions; Windows-permission
    # free).
    # -------------------------------------------------------------------

    def test_regular_mode_candidate_is_stable(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            write(repo, "tracked.txt", b"one-modified\n")
            first = fp(repo, base, tmp_path)
            second = fp(repo, base, tmp_path)
            assert first == second
        self.in_temp_dir(body)

    def test_executable_mode_round_trip(self) -> None:
        """100644 -> 100755 changes identity; reverting restores it."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            regular = fp(repo, base, tmp_path)
            set_index_exec(repo, "tracked.txt", executable=True, home=home)
            executable = fp(repo, base, tmp_path)
            assert executable != regular
            set_index_exec(repo, "tracked.txt", executable=False, home=home)
            restored = fp(repo, base, tmp_path)
            assert restored == regular
        self.in_temp_dir(body)

    def test_mode_change_detectable_after_staging_content(self) -> None:
        """A staged 100755 entry stays detectable once content is staged too."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            write(repo, "tracked.txt", b"one-modified\n")
            regular = fp(repo, base, tmp_path)
            stage(repo, home)
            set_index_exec(repo, "tracked.txt", executable=True, home=home)
            executable = fp(repo, base, tmp_path)
            assert executable != regular
        self.in_temp_dir(body)

    def test_symlink_mode_round_trip(self) -> None:
        """100644 -> 120000 changes identity; reverting restores it."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            regular = fp(repo, base, tmp_path)
            blob = hash_object(repo, b"one\n", home=home)
            set_index_entry(repo, "120000", blob, "tracked.txt", home=home)
            symlinked = fp(repo, base, tmp_path)
            assert symlinked != regular
            set_index_entry(repo, "100644", blob, "tracked.txt", home=home)
            restored = fp(repo, base, tmp_path)
            assert restored == regular
        self.in_temp_dir(body)

    def test_symlink_identity_is_link_text_not_target_file(self) -> None:
        """Symlink content identity is the link-target text, with mode 120000."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            write(repo, "target.txt", b"TARGET FILE CONTENTS\n")
            blob_a = hash_object(repo, b"target.txt", home=home)
            set_index_entry(repo, "120000", blob_a, "link.txt", home=home)
            as_symlink = fp(repo, base, tmp_path)
            # Same bytes as a plain 100644 file differ only by mode/type.
            remove_index_entry(repo, "link.txt", home=home)
            write(repo, "link.txt", b"target.txt")
            as_regular = fp(repo, base, tmp_path)
            assert as_symlink != as_regular
            # Different link-target text differs by content.
            blob_b = hash_object(repo, b"other-target.txt", home=home)
            write(repo, "link.txt", b"other-target.txt")
            set_index_entry(repo, "120000", blob_b, "link.txt", home=home)
            other_symlink = fp(repo, base, tmp_path)
            assert other_symlink != as_symlink
        self.in_temp_dir(body)

    def test_real_symlink_is_not_dereferenced(self) -> None:
        """A real symlink contributes its target string, not target contents."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            clean = fp(repo, base, tmp_path)
            outside = tmp_path / "outside.bin"
            outside.write_bytes(b"OUTSIDE-V1\n" * 64)
            link = repo / "link.txt"
            try:
                os.symlink(str(outside), link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is restricted on this platform")
            with_link = fp(repo, base, tmp_path)
            assert with_link != clean
            outside.write_bytes(b"OUTSIDE-V2\n" * 64)
            assert fp(repo, base, tmp_path) == with_link
            link.unlink()
            assert fp(repo, base, tmp_path) == clean
        self.in_temp_dir(body)

    # -------------------------------------------------------------------
    # Staging invariance: the same effective tree hashes identically
    # whether or not it is staged.
    # -------------------------------------------------------------------

    def test_staging_invariance_modified_tracked(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            write(repo, "tracked.txt", b"one-modified\n")
            unstaged = fp(repo, base, tmp_path)
            stage(repo, home)
            staged = fp(repo, base, tmp_path)
            assert staged == unstaged
            # Fingerprinting a populated index leaves it byte-identical.
            index_snapshot = index_bytes(repo)
            fp(repo, base, tmp_path)
            assert index_bytes(repo) == index_snapshot
        self.in_temp_dir(body)

    def test_staging_invariance_added_file(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            write(repo, "untracked.txt", b"fresh\n")
            unstaged = fp(repo, base, tmp_path)
            stage(repo, home)
            staged = fp(repo, base, tmp_path)
            assert staged == unstaged
        self.in_temp_dir(body)

    def test_staging_invariance_renamed_path(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            (repo / "tracked.txt").rename(repo / "renamed.txt")
            unstaged = fp(repo, base, tmp_path)
            stage(repo, home)
            staged = fp(repo, base, tmp_path)
            assert staged == unstaged
        self.in_temp_dir(body)

    # -------------------------------------------------------------------
    # Rename / copy semantics through the effective tree.
    # -------------------------------------------------------------------

    def test_staged_git_mv_rename(self) -> None:
        """A staged rename fingerprints cleanly and round-trips."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            original = fp(repo, base, tmp_path)
            git_ok(repo, "mv", "tracked.txt", "moved.txt", home=home)
            renamed = fp(repo, base, tmp_path)
            assert renamed != original
            git_ok(repo, "mv", "moved.txt", "tracked.txt", home=home)
            assert fp(repo, base, tmp_path) == original
        self.in_temp_dir(body)

    def test_staged_and_unstaged_rename_share_identity(self) -> None:
        """git mv and a filesystem rename staged later are the same candidate."""
        def body(tmp_path: Path) -> None:
            repo_a, base_a = make_repo(tmp_path / "a")
            repo_b, base_b = make_repo(tmp_path / "b")
            assert base_a == base_b
            git_ok(repo_a, "mv", "tracked.txt", "renamed.txt", home=tmp_path / "a" / "home")
            (repo_b / "tracked.txt").rename(repo_b / "renamed.txt")
            stage(repo_b, tmp_path / "b" / "home")
            assert fp(repo_a, base_a, tmp_path / "a") == fp(repo_b, base_b, tmp_path / "b")
        self.in_temp_dir(body)

    def test_copy_adds_new_path_identity(self) -> None:
        """A copy is an added path; the unchanged source path is untouched."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            before = fp(repo, base, tmp_path)
            (repo / "copy.txt").write_bytes((repo / "tracked.txt").read_bytes())
            after = fp(repo, base, tmp_path)
            assert after != before
        self.in_temp_dir(body)

    # -------------------------------------------------------------------
    # Git-clean (filter/normalization) identity and CRLF staging
    # invariance.
    # -------------------------------------------------------------------

    def staged_entry(self, repo: Path, path: str, home: Path) -> tuple[str, str]:
        """Staged (mode, object id) of one index path."""
        out = git(repo, "ls-files", "-s", "--", path, home=home).stdout.decode("ascii").strip()
        meta = out.split("\t")[0].split()
        assert len(meta) == 3, out
        return meta[0], meta[1]

    def blob_content(self, repo: Path, sha: str, home: Path) -> bytes:
        proc = git(repo, "cat-file", "blob", sha, home=home)
        assert proc.returncode == 0
        return proc.stdout

    def clean_blob(self, repo: Path, path: str, data: bytes, home: Path) -> str:
        """Oracle: path-aware Git-cleaned blob id of ``data`` at ``path``."""
        proc = git(repo, "hash-object", f"--path={path}", "--stdin", home=home, input_bytes=data)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        return proc.stdout.decode("ascii").strip()

    def committed_tree_entry(self, repo: Path, path: str, home: Path) -> tuple[str, str, str] | None:
        """Committed (mode, type, object id) of one path, or None if absent."""
        out = git(repo, "ls-tree", "HEAD", "--", path, home=home).stdout.decode("utf-8", "replace").strip()
        if not out:
            return None
        meta, _, _path = out.partition("\t")
        parts = meta.split()
        assert len(parts) == 3, out
        return parts[0], parts[1], parts[2]

    def test_crlf_clean_identity_and_staging_invariance(self) -> None:
        """CRLF content fingerprints as its Git-cleaned blob; exact-path
        staging does not change identity; conversion-untouched paths hash
        their raw bytes."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            git_ok(repo, "config", "core.autocrlf", "true", home=home)
            write(repo, "crlf.txt", b"one\r\ntwo\r\n")
            write(repo, "plain.txt", b"plain\nlines\n")
            pre_stage = fp(repo, base, tmp_path)
            git_ok(repo, "add", "crlf.txt", "plain.txt", home=home)
            post_stage = fp(repo, base, tmp_path)
            assert post_stage == pre_stage
            # The staged identities are the Git-cleaned blobs, not raw CRLF bytes.
            mode, sha = self.staged_entry(repo, "crlf.txt", home)
            assert mode == "100644"
            assert self.blob_content(repo, sha, home) == b"one\ntwo\n"
            _, sha_plain = self.staged_entry(repo, "plain.txt", home)
            assert self.blob_content(repo, sha_plain, home) == b"plain\nlines\n"
            # Materially altering the content changes the hash.
            write(repo, "crlf.txt", b"one\r\nTWO\r\n")
            assert fp(repo, base, tmp_path) != pre_stage
        self.in_temp_dir(body)

    def test_external_clean_filter_fails_closed(self) -> None:
        """A path attributed to an external clean filter fails closed
        instead of silently hashing bytes that cannot be evaluated
        deterministically read-only."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            write(repo, ".gitattributes", b"*.filtered filter=denoise\n")
            write(repo, "signal.filtered", b"payload\n")
            proc = run_fingerprint(repo, base, tmp_path)
            assert proc.returncode != 0
            assert "CANDIDATE_DIFF_HASH" not in proc.stdout
            assert "clean filter" in proc.stderr
        self.in_temp_dir(body)

    # -------------------------------------------------------------------
    # Gitlink effective identity (nested repository HEAD) and promotion.
    # -------------------------------------------------------------------

    def make_nested_gitlink(self, repo: Path, home: Path) -> tuple[Path, str]:
        """Create an initialized nested repository at submod and stage the
        outer gitlink; return (nested path, nested HEAD)."""
        nested = repo / "submod"
        nested.mkdir()
        init = subprocess.run(
            ["git", "init", "-b", "main", str(nested)],
            capture_output=True,
            env=_git_env(home),
            check=False,
        )
        assert init.returncode == 0, init.stderr.decode("utf-8", "replace")
        git_ok(nested, "config", "core.autocrlf", "false", home=home)
        write(nested, "n.txt", b"v1\n")
        nested_head = commit(nested, "v1", home=home)
        git_ok(repo, "add", "submod", home=home)
        return nested, nested_head

    def test_gitlink_effective_identity_and_promotion(self) -> None:
        """Nested HEAD v1 is stable; advancing to v2 changes the fingerprint
        BEFORE outer staging; staging preserves it; the committed gitlink OID
        matches; dirty nested contents are not part of outer identity."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            nested, v1 = self.make_nested_gitlink(repo, home)
            mode, staged_id = self.staged_entry(repo, "submod", home)
            assert (mode, staged_id) == ("160000", v1)
            stable_v1 = fp(repo, base, tmp_path)
            assert fp(repo, base, tmp_path) == stable_v1

            # Nested advances WITHOUT outer staging.
            write(nested, "n.txt", b"v2\n")
            v2 = commit(nested, "v2", home=home)
            advanced = fp(repo, base, tmp_path)
            assert advanced != stable_v1

            # Exact-path git add of the gitlink preserves the candidate identity.
            git_ok(repo, "add", "submod", home=home)
            assert fp(repo, base, tmp_path) == advanced

            # Dirty nested working tree with the same HEAD changes nothing.
            write(nested, "dirty.txt", b"dirty\n")
            assert fp(repo, base, tmp_path) == advanced
            (nested / "dirty.txt").unlink()

            # Committing the outer repository commits exactly the represented OID.
            commit_index(repo, "candidate", home=home)
            assert fp(repo, base, tmp_path) == advanced
            entry = self.committed_tree_entry(repo, "submod", home)
            assert entry == ("160000", "commit", v2)
        self.in_temp_dir(body)

    def test_gitlink_uninitialized_follows_index_truth(self) -> None:
        """Uninitialized (no .git): index identity stands. Missing worktree
        directory entirely: fail closed as genuinely ambiguous."""
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            nested, _v1 = self.make_nested_gitlink(repo, home)
            initialized = fp(repo, base, tmp_path)
            (nested / ".git").rename(tmp_path / "stashed-dotgit")
            assert fp(repo, base, tmp_path) == initialized
            shutil.rmtree(nested)
            proc = run_fingerprint(repo, base, tmp_path)
            assert proc.returncode != 0
            assert "CANDIDATE_DIFF_HASH" not in proc.stdout
            assert "gitlink" in proc.stderr
        self.in_temp_dir(body)

    # -------------------------------------------------------------------
    # Promotion simulation: fingerprint H -> exact-path git add ->
    # fingerprint -> commit -> committed tree corresponds exactly to the
    # H identity.
    # -------------------------------------------------------------------

    def test_promotion_simulation_regular_executable_rename(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            write(repo, "tracked.txt", b"one-modified\n")
            git_ok(repo, "mv", "sub/nested.txt", "sub/moved.txt", home=home)
            git_ok(repo, "add", "keep.txt", home=home)
            set_index_exec(repo, "keep.txt", executable=True, home=home)
            before_add = fp(repo, base, tmp_path)
            git_ok(repo, "add", "tracked.txt", home=home)
            assert fp(repo, base, tmp_path) == before_add
            commit_index(repo, "candidate", home=home)
            assert fp(repo, base, tmp_path) == before_add
            assert self.committed_tree_entry(repo, "tracked.txt", home) == (
                "100644",
                "blob",
                self.clean_blob(repo, "tracked.txt", b"one-modified\n", home),
            )
            keep_entry = self.committed_tree_entry(repo, "keep.txt", home)
            assert keep_entry is not None
            assert keep_entry[0:2] == ("100755", "blob")
            assert self.committed_tree_entry(repo, "sub/moved.txt", home) == (
                "100644",
                "blob",
                self.clean_blob(repo, "sub/moved.txt", b"nested\n", home),
            )
            assert self.committed_tree_entry(repo, "sub/nested.txt", home) is None
        self.in_temp_dir(body)

    def test_promotion_simulation_crlf(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            git_ok(repo, "config", "core.autocrlf", "true", home=home)
            write(repo, "crlf.txt", b"one\r\ntwo\r\n")
            write(repo, "plain.txt", b"plain\nlines\n")
            before_add = fp(repo, base, tmp_path)
            git_ok(repo, "add", "crlf.txt", "plain.txt", home=home)
            assert fp(repo, base, tmp_path) == before_add
            commit_index(repo, "candidate", home=home)
            assert fp(repo, base, tmp_path) == before_add
            crlf_entry = self.committed_tree_entry(repo, "crlf.txt", home)
            assert crlf_entry is not None and crlf_entry[0:2] == ("100644", "blob")
            assert self.blob_content(repo, crlf_entry[2], home) == b"one\ntwo\n"
            plain_entry = self.committed_tree_entry(repo, "plain.txt", home)
            assert plain_entry is not None
            assert self.blob_content(repo, plain_entry[2], home) == b"plain\nlines\n"
        self.in_temp_dir(body)

    def test_promotion_simulation_symlink(self) -> None:
        def body(tmp_path: Path) -> None:
            repo, base = make_repo(tmp_path)
            home = home_of(tmp_path)
            blob = hash_object(repo, b"target.txt", home=home)
            set_index_entry(repo, "120000", blob, "link.txt", home=home)
            before_commit = fp(repo, base, tmp_path)
            commit_index(repo, "candidate", home=home)
            assert fp(repo, base, tmp_path) == before_commit
            assert self.committed_tree_entry(repo, "link.txt", home) == ("120000", "blob", blob)
            # Where the platform permits real symlinks, the same promotion holds
            # through an exact-path git add.
            try:
                os.symlink("target.txt", repo / "real_link.txt")
            except (OSError, NotImplementedError):
                return
            with_real = fp(repo, base, tmp_path)
            git_ok(repo, "add", "real_link.txt", home=home)
            assert fp(repo, base, tmp_path) == with_real
            commit_index(repo, "candidate-2", home=home)
            real_blob = hash_object(repo, b"target.txt", home=home)
            assert self.committed_tree_entry(repo, "real_link.txt", home) == ("120000", "blob", real_blob)
        self.in_temp_dir(body)


if __name__ == "__main__":
    unittest.main()
