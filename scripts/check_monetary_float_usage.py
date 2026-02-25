import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

KEYWORDS = (
    "amount",
    "price",
    "rate",
    "value",
    "market_value",
    "cost",
    "pnl",
    "return",
    "risk",
    "notional",
    "weight",
)
IGNORE_DIRS = {"tests", ".venv", "venv", "docs", "rfcs", "output", "build", "dist", "__pycache__"}

FLOAT_ANNOTATION = re.compile(r"\bfloat\b")


def is_candidate(path: Path) -> bool:
    parts = set(path.parts)
    if any(p in parts for p in IGNORE_DIRS):
        return False
    return path.suffix == ".py"


def scan_repo(repo_root: Path) -> list[str]:
    findings: list[str] = []
    for file_path in repo_root.rglob("*.py"):
        if not is_candidate(file_path.relative_to(repo_root)):
            continue
        rel = file_path.relative_to(repo_root).as_posix()
        for line_no, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
            lowered = line.lower()
            if not any(k in lowered for k in KEYWORDS):
                continue
            if not FLOAT_ANNOTATION.search(lowered):
                continue
            if "# monetary-float-allow" in lowered:
                continue
            finding = f"{rel}:{line_no}:{line.strip()}"
            findings.append(finding)
    return sorted(set(findings))


def _parse_review_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"Invalid review_by date format: {value!r}, expected YYYY-MM-DD") from exc


def load_allowlist(path: Path) -> tuple[dict[str, dict], list[str], list[str]]:
    if not path.exists():
        return {}, [], []
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_entries = data.get("allowlist", [])
    entries: dict[str, dict] = {}
    errors: list[str] = []
    stale: list[str] = []
    today = datetime.now(tz=UTC).date()
    for item in raw_entries:
        if isinstance(item, str):
            errors.append(f"Legacy allowlist string entry must be migrated: {item}")
            continue
        if not isinstance(item, dict):
            errors.append(f"Allowlist entry must be object, found: {type(item).__name__}")
            continue
        finding = item.get("finding")
        justification = item.get("justification")
        owner = item.get("owner")
        review_by = item.get("review_by")
        if not all([finding, justification, owner, review_by]):
            errors.append(
                "Allowlist entry missing required fields (finding/justification/owner/review_by): "
                + json.dumps(item, sort_keys=True)
            )
            continue
        try:
            review_dt = _parse_review_date(str(review_by))
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if review_dt.date() < today:
            stale.append(str(finding))
        entries[str(finding)] = {
            "finding": str(finding),
            "justification": str(justification),
            "owner": str(owner),
            "review_by": review_dt.strftime("%Y-%m-%d"),
        }
    return entries, errors, stale


def write_allowlist(
    path: Path, findings: list[str], existing_entries: dict[str, dict], review_by: str
) -> None:
    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    allowlist_entries: list[dict] = []
    for finding in sorted(set(findings)):
        if finding in existing_entries:
            allowlist_entries.append(existing_entries[finding])
            continue
        allowlist_entries.append(
            {
                "finding": finding,
                "justification": "Temporary approved monetary float usage; migrate to Decimal.",
                "owner": "platform-governance",
                "review_by": review_by,
            }
        )
    payload = {
        "description": "Approved baseline monetary-float findings. New findings fail CI.",
        "policy_version": "1.1.0",
        "generated_at": generated_at,
        "allowlist": allowlist_entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Guard against unauthorized monetary float usage")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--allowlist",
        default="docs/standards/monetary-float-allowlist.json",
    )
    parser.add_argument("--update-allowlist", action="store_true")
    parser.add_argument(
        "--default-review-days",
        type=int,
        default=180,
        help="Days until review_by for newly generated allowlist entries.",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    allowlist_path = (repo_root / args.allowlist).resolve()
    findings = scan_repo(repo_root)
    allowlist_entries, allowlist_errors, stale_entries = load_allowlist(allowlist_path)

    if args.update_allowlist:
        default_review_by = (datetime.now(tz=UTC) + timedelta(days=args.default_review_days)).strftime(
            "%Y-%m-%d"
        )
        write_allowlist(allowlist_path, findings, allowlist_entries, default_review_by)
        print(f"Updated allowlist with {len(findings)} finding(s): {allowlist_path}")
        return 0

    if allowlist_errors:
        print("Allowlist schema validation failed:")
        for item in allowlist_errors:
            print(f" - {item}")
        return 1

    if stale_entries:
        print("Allowlist contains stale entries (review_by in the past):")
        for item in stale_entries:
            print(f" - {item}")
        print(f"\nUpdate {allowlist_path} with refreshed review dates and remediation status.")
        return 1

    unexpected = sorted(set(findings) - set(allowlist_entries))

    if unexpected:
        print("Unauthorized monetary float usage detected:")
        for item in unexpected:
            print(f" - {item}")
        print(f"\nBaseline allowlist file: {allowlist_path}")
        print("If intentional and approved, run with --update-allowlist in dedicated PR.")
        return 1

    print(
        "Monetary float guard passed. "
        f"Findings={len(findings)}, allowlisted={len(allowlist_entries)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
