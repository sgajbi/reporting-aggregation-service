"""Assert that live branch protection matches the documented policy, field by field.

An undocumented protection exception is indistinguishable from a misconfiguration,
and a policy document that outlives the configuration it describes is worse than
none. This gate fails in both drift directions: when live protection weakens
relative to `quality/branch_protection_policy.v1.json`, and when the documented
zero-approval exception is removed without the configuration strengthening.

The policy document is the only repository-specific input, so a sibling
repository can lift this script verbatim and edit the table. Absent settings are
compared as absent, never coerced to false (a missing
`required_pull_request_reviews` block and `required_approving_review_count: 0`
are different postures and must be distinguishable).

Usage:
  python scripts/check_branch_protection_policy.py --offline   # document shape only
  python scripts/check_branch_protection_policy.py             # live comparison (needs gh auth)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

POLICY_PATH = Path(__file__).resolve().parents[1] / "quality" / "branch_protection_policy.v1.json"

_REQUIRED_EXCEPTION_KEYS = {"field", "value", "reason", "compensating_controls", "retires_when"}

# Every field the live comparison reads. Without these lists an edit could drop
# a field, pass --offline in the blocking lane, and only fail hours later in the
# scheduled live run.
_BYPASS_CATEGORIES = ("users", "teams", "apps")

_REQUIRED_REVIEW_KEYS = (
    "required_approving_review_count",
    "dismiss_stale_reviews",
    "require_code_owner_reviews",
    "require_last_push_approval",
    "bypass_pull_request_allowances",
)

_BOOLEAN_REVIEW_KEYS = (
    "present",
    "dismiss_stale_reviews",
    "require_code_owner_reviews",
    "require_last_push_approval",
)

# Controls the branch-protection API returns that decide whether main can be
# merged to at all. `lock_branch` makes the branch read-only; `required_signatures`
# fails every unsigned merge; `block_creations` changes what may be created.
# Absent from this list they were never compared, so an administrator could
# enable any of them and the scheduled audit still reported a clean match.
_MERGEABILITY_EXPECTED_KEYS = (
    "lock_branch",
    "required_signatures",
    "block_creations",
    "allow_fork_syncing",
)

_BOOLEAN_EXPECTED_KEYS = (
    "enforce_admins",
    "required_linear_history",
    "allow_force_pushes",
    "allow_deletions",
    "required_conversation_resolution",
    "restrictions_present",
    "codeowners_present",
    *_MERGEABILITY_EXPECTED_KEYS,
)

_REQUIRED_EXPECTED_KEYS = (
    "enforce_admins",
    "required_linear_history",
    "allow_force_pushes",
    "allow_deletions",
    "required_conversation_resolution",
    "required_status_checks",
    "required_pull_request_reviews",
    "restrictions_present",
    "codeowners_present",
    # REQUIRED, not optional. An undeclared control is an unmeasured one, and
    # silence is precisely the defect this list closes: an adopter cannot fix it
    # by declaring the field unless the checker reads it, and the checker must
    # not pass a table that omits it.
    *_MERGEABILITY_EXPECTED_KEYS,
)


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    policy: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return policy


def detect_repository(repo_root: Path) -> str | None:
    """Return the repository this checkout actually is, or None if unknowable.

    Identity must be corroborated from OUTSIDE the policy document. The document
    is the thing being validated, so trusting its own `repository` field lets a
    lifted table point at the repository it was copied from: the checker then
    reads someone else's protection, finds it matches, and passes. A sibling
    that lifts the table and forgets to edit one field gets a green gate that
    measured nothing about itself.

    `GITHUB_REPOSITORY` is authoritative in Actions. Locally the origin remote
    is the equivalent fact, and it is read rather than the directory name
    because worktrees and clones are routinely named something else.
    """
    from_env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if from_env:
        return from_env

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
    except (OSError, FileNotFoundError):
        # No git binary is the same situation as no remote: identity cannot be
        # corroborated from outside the document, so it is unknowable rather
        # than an error to crash on. The caller already refuses on None.
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    if not url:
        return None
    url = url.removesuffix(".git")
    if url.startswith("git@"):
        url = url.partition(":")[2]
    parts = [part for part in url.replace("\\", "/").split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def validate_policy_document(policy: dict[str, Any]) -> list[str]:
    """Offline shape check: the document must be complete enough to gate against."""
    issues: list[str] = []

    # Present-but-blank is not a declaration. An empty identity field would pass
    # the mismatch comparison by having nothing to mismatch -- the same gap as
    # omitting it, wearing the shape of a filled-in field.
    if not str(policy.get("repository", "")).strip():
        issues.append(
            "policy declares no repository: the identity field is present but "
            "empty, so nothing can be compared against this checkout"
        )
    for key in ("repository", "protected_branch", "expected", "documented_exceptions"):
        if key not in policy:
            issues.append(f"policy is missing required key: {key}")
    authority = policy.get("review_authority", {})
    for key in ("review_lead", "mergeable_meaning", "escalation"):
        if not str(authority.get(key, "")).strip():
            issues.append(f"review_authority.{key} must be documented")
    expected = policy.get("expected", {})
    for key in _REQUIRED_EXPECTED_KEYS:
        if key not in expected:
            issues.append(f"expected.{key} must be declared")
    checks = expected.get("required_status_checks", {})
    for key in ("strict", "contexts"):
        if key not in checks:
            issues.append(f"expected.required_status_checks.{key} must be declared")
    contexts = checks.get("contexts")
    if "contexts" in checks:
        if not isinstance(contexts, list) or not all(isinstance(c, str) for c in contexts):
            issues.append(
                "expected.required_status_checks.contexts must be a list of strings: "
                "a bare string would be compared character by character"
            )
        elif not contexts:
            issues.append(
                "expected.required_status_checks.contexts is empty: nothing would be required"
            )
    if "strict" in checks and not isinstance(checks["strict"], bool):
        issues.append("expected.required_status_checks.strict must be a boolean")
    for key in _BOOLEAN_EXPECTED_KEYS:
        if key in expected and not isinstance(expected[key], bool):
            issues.append(f"expected.{key} must be a boolean")
    declared_reviews = expected.get("required_pull_request_reviews", {})
    if "present" not in declared_reviews:
        issues.append("expected.required_pull_request_reviews.present must be declared")
    elif declared_reviews.get("present"):
        for key in _REQUIRED_REVIEW_KEYS:
            if key not in declared_reviews:
                issues.append(f"expected.required_pull_request_reviews.{key} must be declared")
        for key in _BOOLEAN_REVIEW_KEYS:
            if key in declared_reviews and not isinstance(declared_reviews[key], bool):
                issues.append(f"expected.required_pull_request_reviews.{key} must be a boolean")
        count = declared_reviews.get("required_approving_review_count")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
            issues.append(
                "expected.required_pull_request_reviews."
                "required_approving_review_count must be an integer"
            )
        bypass = declared_reviews.get("bypass_pull_request_allowances", {})
        for category in _BYPASS_CATEGORIES:
            if category not in bypass:
                issues.append(
                    "expected.required_pull_request_reviews."
                    f"bypass_pull_request_allowances.{category} must be declared"
                )
            elif not isinstance(bypass[category], list):
                issues.append(
                    "expected.required_pull_request_reviews."
                    f"bypass_pull_request_allowances.{category} must be a list"
                )
    for exception in policy.get("documented_exceptions", []):
        missing = _REQUIRED_EXCEPTION_KEYS - set(exception)
        if missing:
            issues.append(f"documented exception is missing keys: {sorted(missing)}")
    if declared_reviews.get("required_approving_review_count") == 0 and not any(
        e.get("field") == "required_pull_request_reviews.required_approving_review_count"
        for e in policy.get("documented_exceptions", [])
    ):
        issues.append(
            "required_approving_review_count is 0 without a documented exception: "
            "either strengthen protection or document the deliberate deviation"
        )
    return issues


def fetch_live_protection(repository: str, branch: str) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repository}/branches/{branch}/protection"],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def resolve_effective_codeowners(repo_root: Path) -> Path | None:
    """Return the CODEOWNERS file GitHub would apply, or None if there is none.

    GitHub does not merge the recognized locations: it uses the first file it
    finds, in this order. A stale or empty file in an earlier location shadows a
    valid one later, so presence-anywhere is not the posture GitHub enforces.
    """
    for location in (".github", "", "docs"):
        candidate = repo_root / location / "CODEOWNERS" if location else repo_root / "CODEOWNERS"
        if candidate.is_file():
            return candidate
    return None


def _enabled(node: Any) -> Any:
    return node.get("enabled") if isinstance(node, dict) else node


def compare_live_to_policy(policy: dict[str, Any], live: dict[str, Any]) -> list[str]:
    expected = policy["expected"]
    issues: list[str] = []

    scalar_fields = {
        "enforce_admins": _enabled(live.get("enforce_admins")),
        "required_linear_history": _enabled(live.get("required_linear_history")),
        "allow_force_pushes": _enabled(live.get("allow_force_pushes")),
        "allow_deletions": _enabled(live.get("allow_deletions")),
        "required_conversation_resolution": _enabled(live.get("required_conversation_resolution")),
        "restrictions_present": live.get("restrictions") is not None,
        **{key: _enabled(live.get(key)) for key in _MERGEABILITY_EXPECTED_KEYS},
    }
    for name, actual in scalar_fields.items():
        if actual != expected[name]:
            issues.append(f"{name}: live={actual!r} policy={expected[name]!r}")

    checks = live.get("required_status_checks") or {}
    if checks.get("strict") != expected["required_status_checks"]["strict"]:
        issues.append(f"required_status_checks.strict: live={checks.get('strict')!r}")
    live_contexts = sorted(checks.get("contexts") or [])
    policy_contexts = sorted(expected["required_status_checks"]["contexts"])
    if live_contexts != policy_contexts:
        issues.append(
            f"required_status_checks.contexts differ: live={live_contexts} policy={policy_contexts}"
        )

    reviews = live.get("required_pull_request_reviews")
    expected_reviews = expected["required_pull_request_reviews"]
    if (reviews is not None) != expected_reviews["present"]:
        issues.append(
            "required_pull_request_reviews block presence: "
            f"live={'present' if reviews is not None else 'ABSENT'} "
            f"policy={'present' if expected_reviews['present'] else 'ABSENT'}"
        )
    elif reviews is not None:
        for key in (
            "required_approving_review_count",
            "dismiss_stale_reviews",
            "require_code_owner_reviews",
            "require_last_push_approval",
        ):
            if reviews.get(key) != expected_reviews[key]:
                issues.append(
                    f"required_pull_request_reviews.{key}: "
                    f"live={reviews.get(key)!r} policy={expected_reviews[key]!r}"
                )
        live_bypass = reviews.get("bypass_pull_request_allowances") or {}
        expected_bypass = expected_reviews["bypass_pull_request_allowances"]
        actual_bypass = {
            "users": sorted(u.get("login", "") for u in live_bypass.get("users", [])),
            "teams": sorted(t.get("slug", "") for t in live_bypass.get("teams", [])),
            "apps": sorted(a.get("slug", "") for a in live_bypass.get("apps", [])),
        }
        if actual_bypass != {k: sorted(v) for k, v in expected_bypass.items()}:
            issues.append(
                "required_pull_request_reviews.bypass_pull_request_allowances: "
                f"live={actual_bypass!r} policy={expected_bypass!r}"
            )

    effective_codeowners = resolve_effective_codeowners(Path(__file__).resolve().parents[1])
    codeowners = effective_codeowners is not None
    if codeowners != expected["codeowners_present"]:
        issues.append(
            f"CODEOWNERS presence: live={codeowners} policy={expected['codeowners_present']}"
        )

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="validate the policy document only")
    args = parser.parse_args()

    policy = load_policy()
    issues = validate_policy_document(policy)

    declared = str(policy.get("repository", "")).strip()
    actual = detect_repository(POLICY_PATH.resolve().parents[1])
    if actual is None:
        issues.append(
            "cannot determine which repository this checkout is (no "
            "GITHUB_REPOSITORY and no origin remote); refusing rather than "
            "trusting the policy document's own repository field"
        )
    elif declared and declared.lower() != actual.lower():
        issues.append(
            f"policy declares repository {declared!r} but this checkout is "
            f"{actual!r}: a lifted policy table that keeps the source "
            "repository would validate the wrong repository and pass"
        )
    if not args.offline and not issues:
        live = fetch_live_protection(policy["repository"], policy["protected_branch"])
        issues.extend(compare_live_to_policy(policy, live))

    if issues:
        print("Branch-protection policy gate failed:")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    mode = "document shape" if args.offline else "live configuration"
    print(f"Branch-protection policy gate passed ({mode}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
