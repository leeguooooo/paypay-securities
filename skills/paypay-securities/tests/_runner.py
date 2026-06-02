"""Minimal test runner (no pytest dep — matches the project convention).
Usage in a test module:  from _runner import run; ... ; if __name__ == "__main__": raise SystemExit(run(globals()))
"""
import sys
from pathlib import Path

# make the package importable when run as a plain script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run(ns) -> int:
    fns = [v for k, v in sorted(ns.items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0
