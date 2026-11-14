# Upstream Recipe Agent Implementation Plan

> **For agentic workers:** Use executing-plans for native implementation. Steps use checkbox syntax.

**Goal:** Replace upstream-update issues with one agent-maintained, human-approved batch PR and release only after existing build gates.

**Architecture:** A trusted Python orchestrator owns GitHub operations. A bounded tool-calling agent uses OpenAI Responses, falling back to Anthropic Messages only on provider availability failures; it can inspect public sources and change recipe files/package locks, but cannot execute commands or access GitHub credentials. Existing isolated native-build workflows run on the batch branch; completion and bot-review events resume repair work on main. Human-approved merge dispatches publication through the existing release pipeline.

**Tech Stack:** Python standard library, GitHub Actions/REST, OpenAI Responses API, Anthropic Messages API.

**Spec:** Approved conversation: daily discovery; one process iterates all newer recipes, assesses provenance/licensing/security, imports/ports/fixes them in one PR; OpenAI primary, Anthropic availability fallback; CI plus CodeRabbit/Codex/other available bots; human approval before merge; publication after merged-source compatibility gates. `Upstream Updates` already contains provider keys and permits main only. Use GITHUB_TOKEN, not a PAT.

## Global Constraints

- Never merge or approve the agent PR automatically, change repository settings, or weaken build/security/smoke gates.
- Run secret-bearing agent work only from main, using the `Upstream Updates` environment.
- Never expose provider/GitHub secrets to recipe execution or fetch tools.
- One fixed automation branch/PR; preserve rejected recipe state and document reasons.
- Dispatch read-only catalog builds explicitly because token-authored pushes do not trigger CI automatically.
- Request bots only with repository-specific availability evidence or explicit configured owner confirmation; never request Copilot.
- Recheck exact PR head before publishing changes or trusting CI; bounded repair/provider calls.

## Review Focus

- Malicious source URLs, redirects, symlinks and traversal must not access secrets or private network resources.
- Provider refusal, malformed output and exhausted limits must not trigger Anthropic to override a safety decision.
- Old-head CI/reviews must not qualify a changed head or start unbounded repair loops.
- No available bot integration must be reported explicitly, not silently counted as reviewed.
- Prior success on the same Unraid version but older source commit must not suppress publication of merged updates.

### Task 1: Bounded provider-backed recipe agent

**Files:** Create `scripts/upstream-agent.py`, `tests/test_upstream_agent.py`.

**Interfaces:** `run_agent(repo: Path, context: dict) -> dict` returns `summary` and a `recipes` list with one `{name, decision: accept|reject, reason}` per candidate. Context includes candidates, existing PR discussion/CI errors, build guide and previous attempts. Environment provides OPENAI_API_KEY, optional ANTHROPIC_API_KEY, OPENAI_MODEL and ANTHROPIC_MODEL. No shell/GitHub tools.

- [ ] Write tests for provider error fallback, refusal/no fallback, bounded tools, URL/private-IP/redirect rejection, path confinement, and preserving existing smoke/privilege policy.
- [ ] Implement stdlib HTTP provider calls and one bounded session iterating the complete candidate batch. Tools read recipe/package metadata, fetch public HTTPS text or archive hashes, edit recipe files and individual package lock entries. Enforce immutable existing acceptance metadata; no arbitrary execution.
- [ ] Verify the focused tests, including actual filesystem edits and a deterministic local provider protocol fixture.

### Task 2: Trusted batch PR orchestration

**Files:** Create `scripts/upstream-updates.py`, `tests/test_upstream_updates.py`.

**Interfaces:** CLI commands `update`, `feedback`, `release`. `update` discovers versions, resumes the fixed branch, gathers exact-head CI and bot/human findings, calls Task 1, validates recipes with trusted code, commits/pushes recipe-only changes, opens/updates one draft PR, explicitly dispatches read-only native catalog build. Successful exact-head builds mark ready and request confirmed bots once per head. Failed builds resume bounded fixes. `feedback` relays relevant review events to main. `release` requires a merged batch PR and current-head human approval before dispatching Unraid release discovery.

- [ ] Write tests for candidate decisions, rejected recipe rollback, forbidden-file changes, stale-head evidence, bounded repairs, bot availability/deduplication, and approval-gated release dispatch.
- [ ] Implement trusted CLI using gh subprocess arguments (no shell), recipe inventory regeneration, existing native validation and git branch operations. Do not execute model-produced code locally.
- [ ] Verify CLI in disposable local git fixtures with recorded GitHub responses; do not create a live recipe update or release during verification.

### Task 3: Workflow events and release freshness

**Files:** Modify `.github/workflows/upstream-versions.yml`, `.github/workflows/unraid-releases.yml`; create `.github/workflows/upstream-feedback.yml`, `.github/workflows/upstream-release.yml`; update `build/README.md`.

- [ ] Replace issue creation with main-only environment-backed agent update runs on schedule/manual dispatch and catalog workflow completion.
- [ ] Add credential-free review relay and approved-merge release dispatch; both validate fixed branch/base/repository before triggering main.
- [ ] Match release discovery success/in-flight evidence to current source SHA as well as exact target identity.
- [ ] Document provider variables, existing environment secrets, optional confirmed review-bot configuration, hard limits, human approval and actual evidence boundaries.
- [ ] Run focused regression suites, workflow generation drift check, actionlint and one adversarial review round. Fix findings, commit/push and open/watch the implementation PR. No automatic merge or deployment.

## Implementation decisions and review resolution

- User confirmed `GITHUB_TOKEN` only. A disposable Actions probe proved that workflow-file pushes are rejected without `workflows` permission: https://github.com/andrebrait/unRAID-NerdTools/actions/runs/43559456478. Affected updates are explicitly left for manual handling; compatible updates stay in the batch. Main-branch workflow files are never changed by the recipe agent.
- User supplied the pinned OMP action as the model reference: GPT-6 Luna with high reasoning, falling back to Haiku 5.5 with medium effort. No premium-model escalation is automatic.
- The condensed adversarial review drove fixes for state-marker injection, public-comment trust, PR closure races/orphaned pushes, large module feeds, cross-session rollback, version-bearing smoke assertions, shared deadlines, secret-bearing API errors and clipped validation feedback.
- Scheduled publication now checks historical merged automation batches contained in the source revision, not merely the merge callback. Human approvers must have write access. Publishing and read-only runs have distinct identities.
- Rejections are recorded in PRs/workflow summaries and deliberately reassessed on later schedules; no permanent ignore database was added. Merge conflicts and deliberately closed PRs stop for a human rather than discard changes.
- Source assessment remains model-assisted and subject to human review. Hash verification and sandbox/ABI/kernel gates are deterministic; a model's publisher/licensing assessment is not a security proof. No blanket same-host rule was added, because it would reject legitimate upstream moves without protecting against malicious paths on shared hosts.
- Session-local rejection restoration and controller restoration to `main` are separate boundaries: the former makes a completed provider session coherent; the latter handles previous commits and abandoned dependencies. Lock restoration preserves unrelated entry text.
- Verification includes offline provider protocol fixtures, real temporary git/native-validation/push lifecycle tests, 2,100-input feed tests, all 86 actual lock entries, executable release-dispatch JavaScript, actionlint and workflow-generation drift checks. No live provider assessment, native package compilation or package release was performed.
- Repository prerequisite: enable Actions-created PRs. The existing main-only provider environment is ready. Bot availability still needs repository evidence or explicit `UPSTREAM_REVIEW_BOTS` owner confirmation. The current worker has no PR-watcher installation/service; no PR polling fallback is used.
