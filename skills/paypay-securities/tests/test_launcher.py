"""bin/paypay must run the INSTALLED package, never a `paypay_sec/` in the CWD."""
import shutil
import subprocess
import tempfile
from pathlib import Path

from _runner import run

LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "paypay"


def test_cwd_decoy_package_is_not_imported():
    if not shutil.which("uv"):
        print("  (skipped: uv not on PATH)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "paypay_sec"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text('print("DECOY")\n')
        r = subprocess.run([str(LAUNCHER), "--version"], cwd=tmp, capture_output=True,
                           text=True, timeout=120)
        assert "DECOY" not in r.stdout, "launcher imported paypay_sec from the CWD"
        assert r.stdout.startswith("paypay "), r.stdout + r.stderr


if __name__ == "__main__":
    raise SystemExit(run(globals()))
