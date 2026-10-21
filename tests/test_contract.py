"""The module contract check over this repo, as `moxi check` runs it at each commit.

A contributor's clone and a pull request run the same check as the commit hook.
"""

from pathlib import Path

from moxi.contract import check

ROOT = Path(__file__).resolve().parents[1]


def test_the_repo_keeps_the_module_contract():
    assert [str(finding) for finding in check(ROOT)] == []
