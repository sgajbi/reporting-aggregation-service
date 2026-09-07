# Lotus Agent Operating Contract

This is the governed operating contract for Lotus agent work.

Repo-root `AGENTS.md` files across Lotus repositories and the deployed local `AGENTS.md` should
remain synchronized copies of this file.

Use `lotus-platform/automation/Sync-AgentOperatingContract.ps1` to synchronize or verify those
copies. That path identifies the script; it is not a command that runs from where you are
standing. Run it from the `lotus-platform` checkout, because the qualified form resolves from the
directory holding the checkouts rather than from the repository you are working in:

```powershell
Set-Location "$env:LOTUS_WORKSPACE_ROOT/lotus-platform"
powershell -ExecutionPolicy Bypass -File automation/Sync-AgentOperatingContract.ps1 -CheckOnly
```

```bash
cd "$LOTUS_WORKSPACE_ROOT/lotus-platform"
pwsh -File automation/Sync-AgentOperatingContract.ps1 -CheckOnly
```

Bare `-CheckOnly` verifies the repository-root copy, which is the one CI can check: the deployed
file exists only on a developer machine. Add `-IncludeDeployedTarget` to check that copy as well,
and `-AllRepoRoots` to check every sibling checkout.

Set `LOTUS_WORKSPACE_ROOT` once to the directory holding the Lotus checkouts. Without a
`lotus-platform` checkout the script cannot be run at all: the GitHub fallback below can display a
document, and it cannot execute one.

## Progressive Context Discovery

Before substantial work, load this small starting set:

1. the target repository's `AGENTS.md` for mandatory operating rules,
2. `lotus-platform/context/LOTUS-QUICKSTART-CONTEXT.md` for ecosystem identity and ownership,
3. the target repository's `REPOSITORY-ENGINEERING-CONTEXT.md` for local architecture, boundaries,
   commands, and constraints,
4. `lotus-platform/context/LOTUS-SKILL-ROUTING-MAP.md` to select an applicable skill before acting.

Then load only task-relevant depth:

1. `lotus-platform/context/LOTUS-ENGINEERING-CONTEXT.md` for cross-repository architecture or
   shared engineering policy,
2. `lotus-platform/context/CONTEXT-REFERENCE-MAP.md` to locate a specific standard, RFC,
   contract, or runbook,
3. `lotus-platform/context/TASK-ROUTING-GUIDE.md` when ownership or the correct context set is
   unclear,
4. `lotus-platform/context/PROCEDURAL-MEMORY-INDEX.md` when execution method, recovery, or
   delivery evidence is central.

Do not load the complete context estate by default. Essential controls in `AGENTS.md` remain
mandatory even when the rest of the task needs only repository-local context.

Skills live in `lotus-platform/codex/skills/<skill-name>/SKILL.md` and the routing map is keyed by
task rather than by repository, so an agent in any repository reaches the right skill through the
task row. Check it even when the task looks narrow: a skill found afterwards means the work was
done to a convention that already existed and was not followed. Documentation and wiki work is
covered by `lotus-readme-wiki-governance`; skills, agent context, routing and manifests by
`lotus-skill-context-governance`.

A path beginning `lotus-platform/` is relative to the directory that holds the Lotus checkouts,
not to the repository you are working in. Qualifying a path says who owns it; it does not by
itself say where to stand to read it, and resolving one of these from a repository root reaches
`<your-repo>/lotus-platform/...`, which does not exist.

Without a sibling checkout, replace the `lotus-platform/` prefix with
`https://github.com/sgajbi/lotus-platform/blob/main/` and read the document there. One rule
covers every path under it, including `context/`, `docs/`, `automation/` and
`codex/skills/<skill-name>/SKILL.md`. The repository is the authoritative source, so a sibling
checkout is a local convenience rather than a prerequisite.

A path naming a document owned by `lotus-platform` carries the `lotus-platform/` prefix, because
this file is deployed unchanged into every repository and a bare path resolves inside whichever
repository is reading it. A path naming a document each repository owns for itself, such as
`AGENTS.md` or `REPOSITORY-ENGINEERING-CONTEXT.md`, is deliberately bare.

Paths in Lotus documentation are otherwise repository-relative. Where a machine-specific location is
unavoidable, `<workspace-root>` means the directory holding the Lotus checkouts and `<temp-dir>`
the local temporary directory; neither is a real path and neither assumes an operating system,
drive or folder name.

## Target Repository Root Rule

Do not assume the inherited shell working directory is the task repository. VS Code multi-root
workspaces can start Codex in the first workspace folder, even when the user asks for another Lotus
repository.

Before substantial work:

1. infer the target repository from the user request, active goal, issue, PR, branch, file path, or
   explicit repo name,
2. inspect the active branch, worktrees, and existing changes before editing; do not overwrite,
   relocate, or delete work unless ownership is verified, and allow the designated repository owner
   to continue scoped work,
3. if the target is a Lotus repository and the current working directory is a different Lotus
   repository, switch command `workdir` to that target repo before reading repo-local context,
   running tests, inspecting git state, editing files, or creating issues,
4. use `lotus-platform` only for central context, automation, platform contracts, skill source, and
   cross-repo governance unless the task explicitly targets `lotus-platform`,
5. for multi-repo work, state the active repo for each command group and never let one repo's
   `AGENTS.md` or `REPOSITORY-ENGINEERING-CONTEXT.md` stand in for another repo's local truth,
6. when delegating or launching background work, pass the exact repository name, absolute repo root,
   branch, read/write scope, and expected evidence so child agents do not inherit the wrong cwd.

If the inherited cwd conflicts with the named target repo, announce the correction briefly and keep
all subsequent repo-local commands anchored to the target repo.

In a shared checkout, verify ownership and the active branch, stage explicit paths, and never stash
another session's work. Record provenance and revert rationale through the
`Agent Context And Task Ledger Playbook` linked from
`lotus-platform/context/PROCEDURAL-MEMORY-INDEX.md`.

## Mandatory Operating Rules

Always:

1. reduce complexity where possible,
2. improve readability, maintainability, and modularity as part of the slice,
3. make code and test improvements that materially improve reliability and maintainability,
4. update documentation when platform or repository truth changes,
5. leave the codebase cleaner than you found it,
6. write meaningful, high-value tests and avoid superficial coverage,
7. keep commits small, meaningful, and truthful,
8. remove dead code, duplicate logic, and stale non-standard handling when encountered,
9. ensure every UI feature is genuinely backed by supported backend functionality,
10. treat "merged to `main` and validated" as the definition of done; ensure
    RFC/docs/wiki/context/contract closure truth is present on `main`, not stranded on an
    unmerged side branch.

For RFC, documentation, wiki, context, contract, supported-features, API-governance, migration, or
CI-workflow changes, run stranded-truth reconciliation before starting implementation, before final
closure, and before moving to the next RFC:

1. `git fetch origin --prune`,
2. `git branch -r --no-merged origin/main`,
3. inspect unmerged branches that touch durable governance paths,
4. classify each as `must-merge`, `cherry-pick`, `superseded`, `delete`, or `active`,
5. merge, cherry-pick, explicitly supersede, or delete unique durable truth before claiming closure.

## Evidence And Guard Integrity

Always:

1. write file content with the editing/patch mechanism, never a shell heredoc that may interpret
   escapes,
2. run a gate bare or explicitly preserve its exit status; do not hide it behind `tee` or `tail`,
3. prove every new or changed guard fails on representative bad inputs and accepts valid inputs,
4. compare synchronized files by committed blob SHA, not working-tree bytes,
5. post issue evidence before closing and verify the comment exists,
6. keep workflow-changing PRs single-commit unless the repository's per-revision dispatcher is
   proven to evaluate every intermediate workflow tree,
7. accept reported output as evidence only when the producing command exits successfully,
8. classify pattern matches and validate a guard's premise before rewriting or hardening it.

The evidence and failure cases behind these rules live in the
`Agentic Coding Quality Evaluation Loop` linked from
`lotus-platform/context/PROCEDURAL-MEMORY-INDEX.md`; they do not
belong in this mandatory entry contract.

## Where Repository-Scoped Practice Lives

`AGENTS.md` is deployed identically to every repository from this contract and checked by
`lotus-platform/automation/Sync-AgentOperatingContract.ps1 -CheckOnly`. Nothing repository-specific belongs in it:
editing one repository's copy forks a governed file.

Repository-scoped working practice — the hazards, conventions and command shapes that are true of
one repository and not the estate — belongs in that repository's
`REPOSITORY-ENGINEERING-CONTEXT.md`, under a section that names it as practice rather than
architecture. Every Lotus repository has that file and each owns its own copy, so it needs no new
convention and no synchronisation. `CLAUDE.md` is not the home for it: only one repository has one,
and it is read by a single agent runtime rather than all of them.

## Delivery Posture

Operate as a banking-grade engineer, not a generic coding assistant.

That means:

1. prefer truthful implementation over cosmetic output,
2. prefer reusable patterns over local hacks,
3. treat naming, contracts, tests, docs, and validation as part of the implementation,
4. use domain-correct private banking, portfolio, advisory, performance, and risk language.

## Skills, Automation, And Async Execution

When the task matches an available Lotus skill, use it.

Before choosing between overlapping Lotus skills, consult
`lotus-platform/context/LOTUS-SKILL-ROUTING-MAP.md`.

Prefer:

1. standards, validators, and runbooks before inventing a new pattern,
2. repo-native commands before ad hoc command sequences,
3. targeted local checks for quick proof,
4. GitHub-backed heavy execution for expensive full validation,
5. async monitoring and fix-forward work rather than blocking on long reruns.

For long-running, delegated, async, or context-compacted work, use
`lotus-platform/context/playbooks/AGENT-CONTEXT-AND-TASK-LEDGER.md`. Preserve operational
identifiers exactly, including repository, branch, PR number, commit SHA, check name, RFC id, file
path, endpoint, contract name, portfolio id, `engineering_task_id`, and task status. Treat
`output/background-runs.json` as local automation evidence and GitHub Actions as GitHub check truth.

For multi-agent delegation, use the governed profiles and envelopes in
`lotus-platform/platform-contracts/agent-engineering/delegation-policy-contract.v1.json`.
Delegate only bounded non-blocking work with explicit read scope, explicit write scope or `none`,
required evidence, and a required return envelope. Keep the main agent accountable for diff review,
integration, tests, PR posture, wiki publication, and final communication. Do not delegate broad
repo cleanup, immediate critical-path blockers, overlapping write scopes, PR merge, or wiki
publication unless the main agent explicitly owns and reviews the final action.

## Wiki Publication Rule

When documentation, RFC, context, runbook, or operator-facing truth changes:

1. update the repo-local `wiki/` source in the same PR when wiki truth changed,
2. record an explicit no-wiki-change decision when no wiki update is needed,
3. before merge, run
   `lotus-platform/automation/Sync-RepoWikis.ps1 -CheckOnly -Repository <repo-name> -AllowUnpublishedSourceChanges`
   when the branch intentionally changes repo-local `wiki/` source,
4. after merge to `main`, publish with
   `lotus-platform/automation/Sync-RepoWikis.ps1 -Publish -Repository <repo-name>`,
5. after publishing, run strict parity verification with
   `lotus-platform/automation/Sync-RepoWikis.ps1 -CheckOnly -Repository <repo-name>`,
6. use `-AllRepositories` only for platform-wide audits or coordinated publication sweeps.

Repo-local `wiki/` is the authored source of truth. The separate GitHub `*.wiki.git` repository is
only the publication target and must not receive hand-edited truth that is absent from repo source.

When a task is explicitly about canonical populated Workbench surfaces, demo screenshots, or
`PB_SG_GLOBAL_BAL_001`, choose `lotus-front-office-runtime` first and use broader QA or delivery
skills only as supporting guidance.

## Front-Office Runtime Routing Rule

When the task is about:

1. local front-office runtime bring-up,
2. populated Workbench screens,
3. panel validation,
4. demo screenshots,
5. canonical UI proof,

use the governed `lotus-workbench` runtime and validation flow first:

1. `lotus-workbench/docs/operations/canonical-front-office-local-runtime.md`
2. `npm run live:stack:up`
3. `npm run live:validate`
4. `npm run live:stack:down`
5. `lotus-platform/automation/Invoke-Canonical-FrontOffice-QA.ps1 -ScreenshotDirectory <path>` when the task needs platform-owned validation evidence and a caller-directed demo screenshot pack
6. `lotus-platform/context/contracts/canonical-front-office-demo-data-contract.json`
7. `lotus-platform/context/contracts/canonical-front-office-demo-data-invariants.json`

Use `PB_SG_GLOBAL_BAL_001` as the governed seeded front-office portfolio unless the task explicitly requires another dataset.

Treat the RFC-0076 contract files as the source of truth for canonical portfolio identity, benchmark
identity, governed as-of date, and minimum supportability thresholds. Runtime evidence should carry
contract provenance instead of relying on implicit repo convention.

Canonical platform QA includes `lotus-idea` by default. Do not reintroduce an opt-in flag or skip
`lotus-idea` readiness and teardown evidence unless the task explicitly asks for a diagnostic
partial run.

Do not treat `lotus-platform/platform-stack` as the canonical front-office product bring-up path. It owns shared ingress and infrastructure support, not the full governed product-surface flow.

Do not capture or share demo-ready screenshots before canonical API, calculation, and panel validation pass. If a pre-validation capture is necessary for diagnosis, label it with a `diagnostic-` prefix and keep it separate from demo evidence.

## Context Maintenance Rule

Keep the context system up to date as Lotus changes.

Update the relevant context artifacts when:

1. platform architecture changes,
2. repository responsibilities change,
3. canonical commands or validation flows change,
4. CI or governance expectations change,
5. a repeatable pattern should become durable guidance,
6. domain vocabulary or operating assumptions materially change.

If the change is platform-wide:

1. update the central context system in `lotus-platform/context/`.
2. update `lotus-platform/context/LOTUS-SKILL-ROUTING-MAP.md` if task routing expectations
   changed.

If the change is repository-local:

1. update that repository's `REPOSITORY-ENGINEERING-CONTEXT.md`.

If both changed:

1. update both in the same slice.

Documented commands must state their working directory, provide runnable OS-specific variants, use
portable paths, and be verified from a fresh checkout. Detailed authoring rules live in
`lotus-platform/docs/documentation/LOTUS-DOCUMENTATION-LAYERING.md`.

## Cross-Links

Central context system:

1. `lotus-platform/context/LOTUS-QUICKSTART-CONTEXT.md`
2. `lotus-platform/context/LOTUS-ENGINEERING-CONTEXT.md`
3. `lotus-platform/context/CONTEXT-REFERENCE-MAP.md`
4. `lotus-platform/context/PROCEDURAL-MEMORY-INDEX.md`
5. `lotus-platform/context/LOTUS-SKILL-ROUTING-MAP.md`
6. `lotus-platform/context/lotus-context-manifest.json`
7. `lotus-platform/context/platform-engineering-ledger.md`
8. `lotus-platform/context/recent-architectural-decisions-digest.md`
9. `lotus-platform/docs/onboarding/LOTUS-DEVELOPER-ONBOARDING.md`
10. `lotus-platform/docs/onboarding/LOTUS-AGENT-RAMP-UP.md`

Repository-local context:

1. `REPOSITORY-ENGINEERING-CONTEXT.md` in the repository you are changing

When the central contract changes, keep both this source file and the deployed `AGENTS.md` synchronized.
