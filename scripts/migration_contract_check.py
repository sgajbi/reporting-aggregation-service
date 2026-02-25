from __future__ import annotations

import argparse
from pathlib import Path

REQUIRED_DOC = Path("docs/standards/migration-contract.md")
REQUIRED_PHRASES = (
    "no persistent schema",
    "forward-fix",
    "versioned migration",
)


def run_no_schema_checks() -> int:
    if not REQUIRED_DOC.exists():
        print(f"Missing required migration contract document: {REQUIRED_DOC}")
        return 1

    content = REQUIRED_DOC.read_text(encoding="utf-8").lower()
    missing = [phrase for phrase in REQUIRED_PHRASES if phrase not in content]
    if missing:
        print("Migration contract document is missing required phrases:")
        for phrase in missing:
            print(f"- {phrase}")
        return 1

    print("Migration contract check passed (no-schema mode).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate migration contract requirements.")
    parser.add_argument("--mode", choices=["no-schema"], default="no-schema")
    args = parser.parse_args()

    if args.mode == "no-schema":
        return run_no_schema_checks()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
