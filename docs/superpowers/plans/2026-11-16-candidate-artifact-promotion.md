# Candidate Artifact Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse exact package bytes from successful recipe-PR catalog builds after merge, while rerunning every target compatibility gate and keeping publication fail-closed.

**Architecture:** A PR catalog run uploads a small signed-by-contract manifest artifact and one changed-package artifact. The controller records their exact run/attempt/IDs on the PR head. Merge-triggered target runs validate the tuple, current normal build plan, reuse-compatibility keys, package hashes and archives; they skip changed-package compilation, retain normal previous-release carry-over for unchanged packages, and rerun target gates. Recipe PRs open ready with an informational candidate status; one human approval protects `main`, while the merge callback refuses immediate publication without a valid candidate.

**Tech Stack:** Python standard library, GitHub Actions YAML, `actions/upload-artifact` v7, `actions/download-artifact` v8, GitHub REST API, unittest.

**Spec:** `docs/superpowers/specs/2026-11-16-candidate-artifact-promotion-design.md`

## Global Constraints

- `GITHUB_TOKEN` is the only GitHub credential; candidate download jobs receive only `actions:read` and `contents:read` unless an existing write step requires more.
- Candidate artifacts must come from the same repository, exact trusted workflow, `automation/upstream-recipes`, exact head SHA, exact run ID and attempt, successful `publish=false` run.
- Candidate mode accepts a complete tuple or fails; it never silently compiles or publishes mismatched bytes.
- Scheduled release discovery without candidate inputs keeps the existing compile path.
- Candidate packages exactly cover the current plan's `build` set; previous-release carry-over exactly covers `reuse`.
- Target ABI, install, audit, exact-kernel smoke, source bundle, checksums, approval and release gates remain authoritative.
- No proprietary Unraid image/root enters an artifact.
- Package-affecting build-logic changes still require `REBUILD_GENERATION` updates.

## Review Focus

- Candidate run rerun: attempt 2 must never consume attempt 1 artifacts.
- Main drift after candidate build: any changed plan/build key/build number must reject reuse.
- Artifact substitution: wrong ID/name/run, symlink, duplicate, extra, missing, hash or size mismatch must fail before package-job suppression.
- Mixed package sources: candidate `build` files and previous-release `reuse` files must be disjoint and complete.
- Merge without candidate: publication dispatch must fail closed while later scheduled discovery can rebuild normally.

---

### Task 1: Candidate Manifest and Package Verification

**Files:**
- Modify: `build/native.py`
- Modify: `build/test_native.py`
- Modify: `build/README.md`

**Interfaces:**
- Produces: `candidate_manifest(packages, plan, package_dir, identity) -> dict`
- Produces: `write_candidate_manifest(path, manifest) -> None`
- Produces: `verify_candidate_bundle(packages, plan, package_dir, manifest, expected) -> dict`
- CLI: `candidate-create --package-artifacts --plan --output --repository --pr --head --tree --workflow --run --attempt --artifact-name`
- CLI: `candidate-verify --package-artifacts --manifest --expected-json`
- Returned verification data includes the current `builds` mapping and sorted candidate filenames for workflow outputs.

- [ ] **Step 1: Write failing manifest tests**

Cover deterministic schema/order, exact changed `build` set, build numbers, reuse-compatibility keys, SHA256/size, run/attempt/head/tree identity, and rejection of an empty candidate set.

- [ ] **Step 2: Run focused tests and verify expected failures**

Run: `python3 -m unittest build.test_native.CandidateArtifactTests`

Expected: failures because candidate APIs/CLI do not exist.

- [ ] **Step 3: Implement deterministic manifest generation**

Use current `build_keys()`, `artifact_name()`, normal plan `build`/`builds`, regular-file checks and streaming hashes. Keep target evidence out of the manifest.

- [ ] **Step 4: Write failing hostile verification tests**

Cover wrong run/attempt/head/tree, changed build key/number, missing/extra/duplicate package, symlink/special file, filename mismatch, SHA/size mismatch, malformed schema, candidate/reuse overlap, and current-plan drift.

- [ ] **Step 5: Implement fail-closed bundle verification**

Require package names/files to equal current plan `build`/`builds`; run `inspect_package()` for every imported archive. Return only verified data.

- [ ] **Step 6: Verify Task 1**

Run: `python3 -m unittest build.test_native.CandidateArtifactTests`

Expected: all candidate tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add build/native.py build/test_native.py build/README.md
git commit -m "feat: validate candidate package bundles"
```

### Task 2: Retain Exact PR Candidate Artifacts

**Files:**
- Modify: `.github/workflows/build-packages.yml`
- Modify: `tests/test_upstream_release_dispatch.py` or add focused workflow-contract test under `tests/`

**Interfaces:**
- Consumes: Task 1 `candidate-create`
- Produces artifacts:
  - `candidate-packages-<run_id>-<run_attempt>` containing only plan `build` `.txz` files
  - `candidate-manifest-<run_id>-<run_attempt>` containing only `candidate-manifest.json`
- Both use 90-day retention and are uploaded only after successful `publish=false` full-catalog gates on `automation/upstream-recipes`.

- [ ] **Step 1: Write failing workflow contract tests**

Assert exact branch/event/publish guard, deterministic artifact names, 90-day retention, no full catalog/reused package upload, and manifest creation after native gates.

- [ ] **Step 2: Run focused test and verify failure**

Run the focused workflow contract unittest.

- [ ] **Step 3: Implement candidate extraction and uploads**

After the trusted full build passes, call `candidate-create` using `needs.package-list.outputs.plan`, copy only expected changed packages to a staging directory, and upload package/manifest artifacts separately.

- [ ] **Step 4: Verify Task 2**

Run focused workflow tests plus `actionlint .github/workflows/build-packages.yml`.

- [ ] **Step 5: Commit Task 2**

```bash
git add .github/workflows/build-packages.yml tests
git commit -m "feat: retain recipe candidate packages"
```

### Task 3: Ready PRs and Exact Candidate State

**Files:**
- Modify: `scripts/upstream-updates.py`
- Modify: `.github/workflows/upstream-versions.yml`
- Modify: `tests/test_upstream_updates.py`
- Modify: `tests/test_upstream_controller_security.py`

**Interfaces:**
- Candidate state keys: `head_sha`, `run_id`, `run_attempt`, `package_artifact_id`, `package_artifact_name`, `manifest_artifact_id`, `manifest_artifact_name`, `manifest_sha256`, `build_url`.
- Status context: `nerdtools/candidate-build`, pending/success/failure, exact PR URL.
- Controller downloads only the manifest artifact by exact artifact ID to validate/hash it.

- [ ] **Step 1: Write failing ready/status tests**

Normal and orphan PR creation must use `draft: false`; retries/head changes never convert to draft, clear candidate state, and set pending status on the new head.

- [ ] **Step 2: Write failing artifact-discovery tests**

Fixture exact run/attempt artifact API responses. Reject expired, missing, duplicate-name, wrong-run and partial package/manifest artifact sets. Verify manifest download/extraction bounds and stored SHA256.

- [ ] **Step 3: Implement ready lifecycle and candidate discovery**

Keep visible building/awaiting states. On exact-head successful workflow callback, validate run identity and artifacts, persist the complete tuple, and set success. Build/artifact failure sets failure without approving/merging.

- [ ] **Step 4: Verify Task 3**

Run: `python3 -m unittest discover -s tests -p 'test_upstream*.py'`

- [ ] **Step 5: Commit Task 3**

```bash
git add scripts/upstream-updates.py .github/workflows/upstream-versions.yml tests
git commit -m "feat: record exact recipe candidate artifacts"
```

### Task 4: Carry Candidate Identity Through Release Discovery

**Files:**
- Modify: `scripts/upstream-updates.py`
- Modify: `.github/workflows/upstream-release.yml`
- Modify: `.github/workflows/unraid-releases.yml`
- Modify: `.github/workflows/build-packages.yml`
- Modify: `tests/test_upstream_release_dispatch.py`

**Interfaces:**
- `release()` dispatches candidate PR/head/run/attempt/artifact IDs/names/manifest digest/merge SHA.
- `unraid-releases.yml` defines optional string inputs for the complete tuple and rejects partial tuples.
- Target build run identity includes candidate run/attempt when present; deduplication matches it.

- [ ] **Step 1: Write failing dispatch/deduplication tests**

Cover exact tuple propagation, partial tuple rejection, merged-head mismatch, absent candidate failure, candidate-aware title/deduplication, and scheduled no-candidate behavior.

- [ ] **Step 2: Implement complete input propagation**

Validate candidate state again against merged PR head before dispatch. Keep durable approval attestation unchanged.

- [ ] **Step 3: Verify Task 4**

Run focused release-dispatch tests and actionlint for all changed workflows.

- [ ] **Step 4: Commit Task 4**

```bash
git add scripts/upstream-updates.py .github/workflows tests/test_upstream_release_dispatch.py
git commit -m "feat: propagate candidate identity to releases"
```

### Task 5: Verify and Reuse Candidate Bytes in Target Runs

**Files:**
- Modify: `.github/workflows/build-packages.yml`
- Modify: `build/native.py`
- Modify: `build/test_native.py`

**Interfaces:**
- Trusted `candidate` job downloads exact artifact IDs using `actions:read`, validates through Task 1, and reuploads one same-run verified package artifact.
- Outputs: `active`, `builds`, `candidate_identity`.
- `package-list` computes the normal plan. Candidate mode requires exact `build` coverage, outputs `names=[]`, preserves `reuse`, and passes current `builds`.
- Final build downloads verified candidate packages, then checksum-verified previous-release `reuse` packages; sets must be disjoint.

- [ ] **Step 1: Write failing candidate-mode plan tests**

Cover exact candidate `build` coverage, unchanged `reuse`, compile-job suppression, wrong build number/key, candidate/reuse overlap and incomplete package set.

- [ ] **Step 2: Implement candidate verification job and plan wiring**

Do not let invalid candidate mode fall through to compilation. Normal no-candidate runs remain unchanged.

- [ ] **Step 3: Write failing final-consumption tests**

Prove only verified same-run candidate files reach `--package-artifacts`, previous-release carry-over cannot overwrite them, and package inspection occurs before target gates.

- [ ] **Step 4: Implement final consumption and provenance**

Regenerate final catalog/checksums/source bundle/target evidence. Include candidate manifest in `dist` and add candidate origin to final provenance.

- [ ] **Step 5: Verify Task 5**

Run candidate/native tests, full build unittest discovery in the builder environment, workflow generation checks and actionlint.

- [ ] **Step 6: Commit Task 5**

```bash
git add .github/workflows/build-packages.yml build/native.py build/test_native.py
git commit -m "feat: reuse verified candidate packages"
```

### Task 6: Approval Protection and Documentation

**Files:**
- Modify: `build/README.md`
- Modify: `docs/superpowers/specs/2026-11-16-candidate-artifact-promotion-design.md`
- Repository setting: protect `main` with one required approving review, stale-approval dismissal, no automation bypass; do not require recipe-specific status globally.

- [ ] **Step 1: Update maintainer documentation**

Document ready PR behavior, candidate status meaning, artifact expiry, fail-closed merge callback, scheduled rebuild recovery and retained final manifest.

- [ ] **Step 2: Apply and verify branch protection**

Preserve existing settings; require one approval and dismiss stale approvals. Verify automation cannot bypass.

- [ ] **Step 3: Run final local gates**

Run upstream automation tests, native tests in builder environment, package workflow generation, actionlint, shell syntax and `git diff --check`.

- [ ] **Step 4: Commit Task 6**

```bash
git add build/README.md docs
git commit -m "docs: explain candidate package promotion"
```

### Task 7: Live Nonpublishing Promotion Proof

**Files:** none expected; temporary exact evaluation branch and artifacts only.

- [ ] **Step 1: Produce a controlled candidate**

Dispatch a `publish=false` catalog build for an exact branch/head and record both artifact IDs plus manifest digest.

- [ ] **Step 2: Dispatch post-merge-equivalent candidate mode without publication**

Use an exact trusted ref and candidate tuple. Confirm package compile jobs are skipped, candidate packages and previous-release reuse are disjoint/complete, and target ABI/kernel/smoke gates pass.

- [ ] **Step 3: Verify no publication and clean temporary access**

Confirm no release/assets were created; restore main-only environment access and remove temporary refs after preserving evidence.

- [ ] **Step 4: Final review and PR**

Request one condensed adversarial review, fix findings, push, open the PR, use ompweb `watch_github`, handle CI/current-head review, merge when green, and sync primary.
