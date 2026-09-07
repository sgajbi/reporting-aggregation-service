"""The documented branch-protection policy must stay complete and self-consistent.

The live comparison runs in CI where a token exists; these offline checks keep
the policy document itself honest so the live gate always has a valid table to
compare against, and so the zero-approval exception cannot be silently deleted
while the configuration stays weak.
"""

import copy

from scripts.check_branch_protection_policy import (
    _MERGEABILITY_EXPECTED_KEYS,
    compare_live_to_policy,
    load_policy,
    resolve_effective_codeowners,
    validate_policy_document,
)


def _live_matching_policy(policy: dict) -> dict:
    expected = policy["expected"]
    return {
        # Derived from the checker's own list rather than restated here. These
        # four all carry the plain `{"enabled": bool}` shape, and writing them
        # out again would make this fake drift the next time a control is added
        # -- which is exactly how the four arrived undeclared in the first place.
        **{key: {"enabled": expected[key]} for key in _MERGEABILITY_EXPECTED_KEYS},
        "enforce_admins": {"enabled": expected["enforce_admins"]},
        "required_linear_history": {"enabled": expected["required_linear_history"]},
        "allow_force_pushes": {"enabled": expected["allow_force_pushes"]},
        "allow_deletions": {"enabled": expected["allow_deletions"]},
        "required_conversation_resolution": {
            "enabled": expected["required_conversation_resolution"]
        },
        "restrictions": {"users": []} if expected["restrictions_present"] else None,
        "required_status_checks": {
            "strict": expected["required_status_checks"]["strict"],
            "contexts": list(expected["required_status_checks"]["contexts"]),
        },
        "required_pull_request_reviews": (
            {
                key: expected["required_pull_request_reviews"][key]
                for key in (
                    "required_approving_review_count",
                    "dismiss_stale_reviews",
                    "require_code_owner_reviews",
                    "require_last_push_approval",
                )
            }
            if expected["required_pull_request_reviews"]["present"]
            else None
        ),
    }


def test_policy_document_is_complete() -> None:
    assert validate_policy_document(load_policy()) == []


def test_zero_approval_count_requires_a_documented_exception() -> None:
    policy = copy.deepcopy(load_policy())
    policy["documented_exceptions"] = []

    issues = validate_policy_document(policy)

    assert any("documented exception" in issue for issue in issues)


def test_matching_live_configuration_passes() -> None:
    policy = load_policy()

    assert compare_live_to_policy(policy, _live_matching_policy(policy)) == []


def test_weakened_live_protection_fails() -> None:
    policy = load_policy()
    live = _live_matching_policy(policy)
    live["enforce_admins"] = {"enabled": False}
    live["required_status_checks"]["contexts"] = live["required_status_checks"]["contexts"][:-1]

    issues = compare_live_to_policy(policy, live)

    assert any(issue.startswith("enforce_admins") for issue in issues)
    assert any("contexts differ" in issue for issue in issues)


def test_absent_reviews_block_is_distinguished_from_zero_count() -> None:
    # The render#66 drift class: a missing required_pull_request_reviews block
    # must be reported as ABSENT, never conflated with a present zero-count.
    policy = load_policy()
    live = _live_matching_policy(policy)
    live["required_pull_request_reviews"] = None

    issues = compare_live_to_policy(policy, live)

    assert any("ABSENT" in issue for issue in issues)


def test_codeowners_resolution_follows_github_precedence(tmp_path):
    """A .github/ file wins over root and docs/, as GitHub resolves it."""
    for location in (".github", "docs"):
        (tmp_path / location).mkdir()
    (tmp_path / "CODEOWNERS").write_text("* @root\n", encoding="utf-8")
    (tmp_path / "docs" / "CODEOWNERS").write_text("* @docs\n", encoding="utf-8")

    assert resolve_effective_codeowners(tmp_path) == tmp_path / "CODEOWNERS"

    (tmp_path / ".github" / "CODEOWNERS").write_text("", encoding="utf-8")
    effective = resolve_effective_codeowners(tmp_path)
    assert effective == tmp_path / ".github" / "CODEOWNERS"
    assert effective.read_text(encoding="utf-8") == "", (
        "an empty higher-precedence file must shadow the valid lower ones, "
        "because that is the posture GitHub applies"
    )


def test_codeowners_resolution_reports_absence(tmp_path):
    assert resolve_effective_codeowners(tmp_path) is None


def test_offline_validation_rejects_a_policy_missing_expected_fields():
    """--offline is the only PR-time gate; it must not accept a gutted policy."""
    policy = load_policy()
    for field in ("enforce_admins", "required_status_checks", "required_pull_request_reviews"):
        incomplete = copy.deepcopy(policy)
        del incomplete["expected"][field]
        issues = validate_policy_document(incomplete)
        assert any(f"expected.{field} must be declared" in issue for issue in issues), (
            f"removing expected.{field} passed the offline gate"
        )


def test_offline_validation_rejects_an_empty_required_context_list():
    policy = copy.deepcopy(load_policy())
    policy["expected"]["required_status_checks"]["contexts"] = []
    issues = validate_policy_document(policy)
    assert any("contexts is empty" in issue for issue in issues)


def test_offline_validation_rejects_missing_nested_fields():
    """Deleting a nested field must not pass offline and crash the live run."""
    policy = load_policy()
    cases = [
        (("required_status_checks", "strict"), "expected.required_status_checks.strict"),
        (("required_status_checks", "contexts"), "expected.required_status_checks.contexts"),
        (
            ("required_pull_request_reviews", "present"),
            "expected.required_pull_request_reviews.present",
        ),
        (
            ("required_pull_request_reviews", "dismiss_stale_reviews"),
            "expected.required_pull_request_reviews.dismiss_stale_reviews",
        ),
        (
            ("required_pull_request_reviews", "bypass_pull_request_allowances"),
            "expected.required_pull_request_reviews.bypass_pull_request_allowances",
        ),
    ]
    for (parent, field), expected_message in cases:
        incomplete = copy.deepcopy(policy)
        del incomplete["expected"][parent][field]
        issues = validate_policy_document(incomplete)
        assert any(expected_message in issue for issue in issues), (
            f"removing expected.{parent}.{field} passed the offline gate"
        )


def test_offline_validation_requires_each_bypass_category():
    """The live comparison builds all three categories; offline must demand them."""
    policy = load_policy()
    for category in ("users", "teams", "apps"):
        incomplete = copy.deepcopy(policy)
        del incomplete["expected"]["required_pull_request_reviews"][
            "bypass_pull_request_allowances"
        ][category]
        issues = validate_policy_document(incomplete)
        assert any(
            f"bypass_pull_request_allowances.{category} must be declared" in issue
            for issue in issues
        ), f"removing bypass_pull_request_allowances.{category} passed the offline gate"


def test_offline_validation_rejects_wrong_value_types():
    """A bare string would be compared character by character after merge."""
    policy = copy.deepcopy(load_policy())
    policy["expected"]["required_status_checks"]["contexts"] = "PR Merge Gate / Coverage"
    assert any("must be a list of strings" in issue for issue in validate_policy_document(policy))

    policy = copy.deepcopy(load_policy())
    policy["expected"]["required_status_checks"]["strict"] = "true"
    assert any(
        "required_status_checks.strict must be a boolean" in issue
        for issue in validate_policy_document(policy)
    )

    policy = copy.deepcopy(load_policy())
    policy["expected"]["enforce_admins"] = "true"
    assert any(
        "expected.enforce_admins must be a boolean" in issue
        for issue in validate_policy_document(policy)
    )


def test_offline_validation_rejects_wrong_review_value_types():
    """A string "true" or "0" would merge and mismatch only in the live run."""
    base = load_policy()

    policy = copy.deepcopy(base)
    policy["expected"]["required_pull_request_reviews"]["dismiss_stale_reviews"] = "true"
    assert any(
        "dismiss_stale_reviews must be a boolean" in issue
        for issue in validate_policy_document(policy)
    )

    policy = copy.deepcopy(base)
    policy["expected"]["required_pull_request_reviews"]["required_approving_review_count"] = "0"
    assert any(
        "required_approving_review_count must be an integer" in issue
        for issue in validate_policy_document(policy)
    )

    policy = copy.deepcopy(base)
    policy["expected"]["required_pull_request_reviews"]["bypass_pull_request_allowances"][
        "users"
    ] = "nobody"
    assert any(
        "bypass_pull_request_allowances.users must be a list" in issue
        for issue in validate_policy_document(policy)
    )
