"""Enable `python -m paypay_sec` so the launcher can invoke the CLI unambiguously
(`uv run python -m paypay_sec`), avoiding any `paypay` name clash on PATH."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
