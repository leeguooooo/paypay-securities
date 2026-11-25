"""The CLI's __version__ must match pyproject.toml (v0.2.1 shipped with them out of sync)."""
import re
from pathlib import Path

from _runner import run
from paypay_sec import __version__


def test_version_matches_pyproject():
    py = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', py, re.M)
    assert m, "no version in pyproject.toml"
    assert __version__ == m.group(1), f"__version__ {__version__} != pyproject {m.group(1)}"


if __name__ == "__main__":
    raise SystemExit(run(globals()))
