"""Enforce baseline OpenAPI quality for reporting aggregation service."""

from __future__ import annotations

import pathlib
import sys

repo_root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "src"))

from src.app.main import app


def main() -> int:
    schema = app.openapi()
    paths = schema.get("paths", {})
    if not paths:
        print("OpenAPI quality gate: no paths found")
        return 1

    missing_docs: list[str] = []
    operation_ids: list[str] = []

    for path, operations in paths.items():
        for method, operation in operations.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            summary = operation.get("summary")
            description = operation.get("description")
            if not summary and not description:
                missing_docs.append(f"{method.upper()} {path}")
            op_id = operation.get("operationId")
            if op_id:
                operation_ids.append(op_id)

    if missing_docs:
        print("OpenAPI quality gate: missing endpoint documentation")
        for endpoint in missing_docs:
            print(f"- {endpoint}")
        return 1

    duplicate_ids = sorted({op_id for op_id in operation_ids if operation_ids.count(op_id) > 1})
    if duplicate_ids:
        print("OpenAPI quality gate: duplicate operationId values")
        for op_id in duplicate_ids:
            print(f"- {op_id}")
        return 1

    print("OpenAPI quality gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
