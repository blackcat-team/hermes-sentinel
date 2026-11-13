"""Check that H2 cleanup evidence requires a successful firewall query."""

from pathlib import Path
import re
import subprocess
import textwrap

import pytest


RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "DEADMAN_DEPLOYMENT.md"


@pytest.mark.parametrize(
    ("scenario", "outcome", "deletion_attempted"),
    [
        ("absent", 0, False),
        ("present", 0, True),
        ("list_failed", 1, False),
        ("delete_failed", 1, True),
        ("verify_failed", 1, True),
        ("still_present", 1, True),
    ],
)
def test_cleanup_requires_verified_absence(
    tmp_path: Path, scenario: str, outcome: int, deletion_attempted: bool,
) -> None:
    block = re.search(
        r"<!-- h2-isolation-cleanup -->\n   ```bash\n(.*?)\n   ```",
        RUNBOOK.read_text(),
        re.DOTALL,
    )
    assert block is not None
    fake_sudo = """
sudo() {
    case "$*" in
        'nft list tables')
            if [[ "$H2_SCENARIO" = list_failed ]]; then return 1; fi
            if [[ -f attempted ]]; then
                if [[ "$H2_SCENARIO" = verify_failed ]]; then return 1; fi
                if [[ "$H2_SCENARIO" != still_present ]]; then return 0; fi
            fi
            if [[ "$H2_SCENARIO" != absent ]]; then echo 'table inet hermes_sentinel_h2'; fi
            ;;
        'nft delete table inet hermes_sentinel_h2')
            touch attempted
            if [[ "$H2_SCENARIO" = delete_failed ]]; then return 1; fi
            ;;
        *) return 1 ;;
    esac
}
"""
    result = subprocess.run(
        ["bash", "-c", 'H2_SCENARIO="$1"\n' + fake_sudo + textwrap.dedent(block[1]), "cleanup", scenario],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == outcome
    assert ("H2 isolation removed" in result.stdout) == (outcome == 0)
    assert (tmp_path / "attempted").exists() == deletion_attempted
