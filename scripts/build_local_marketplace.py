#!/usr/bin/env python3
"""Build an allowlisted local marketplace from this source tree."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from profile_harness.packaging import build_local_marketplace  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the Codex Profile Harness local marketplace"
    )
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    print(build_local_marketplace(SOURCE_ROOT, arguments.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
