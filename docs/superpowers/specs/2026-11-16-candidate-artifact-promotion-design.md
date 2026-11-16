# Candidate Artifact Promotion Design

## Intent

Recipe automation should build changed packages once. A successful recipe-PR catalog run already compiles and tests those changed package bytes; post-merge publication should reuse them instead of compiling them again. Unchanged packages continue using the existing checksum-verified previous-release carry-over.

The PR should open ready immediately. A candidate-build status represents the catalog gate, one human approval gates merge, and the merge callback refuses publication without a valid candidate. The candidate status is not a repository-wide required check because that would block unrelated PRs.

## Goals

- Retain the changed package set from a successful exact-head recipe-PR catalog run for 90 days.
- Bind retained bytes to repository, PR head, workflow run and attempt, source tree, package reuse-compatibility keys, build numbers, filenames, sizes and SHA256 hashes.
- Carry that immutable candidate identity through the merge callback and release discovery.
- Reuse candidate package bytes after merge only when the current normal build plan names the same changed packages and every reuse-compatibility key matches.
- Preserve the existing previous-release carry-over for unchanged packages.
- Rerun target-specific archive, ABI, install, kernel-boot and smoke gates before publication.
- Open new automation PRs ready, with candidate-build status instead of draft state.
- Fail closed on an absent, missing, expired, partial or mismatched candidate in the merge-triggered publication path.

## Non-goals

- Reusing target-specific compatibility evidence from the PR run.
- Publishing directly from an unmerged branch.
- Retaining proprietary Unraid images or extracted roots.
- Replacing source bundles, release provenance, human approval or target checks.
- General-purpose artifact promotion for arbitrary PRs.

## Constraints

- `GITHUB_TOKEN` remains the only GitHub credential.
- Model jobs keep read-only GitHub permissions and no publication authority.
- Candidate artifacts come only from the trusted `Build native packages` workflow, same repository, `automation/upstream-recipes`, `publish=false`, successful exact PR head.
- Package compilation uses the pinned baseline; target compatibility remains a separate post-merge concern.
- Package build keys are reuse-compatibility keys, not byte identities: package SHA256 binds bytes, and `REBUILD_GENERATION` remains mandatory for package-affecting build-logic changes.
- Existing 16 MiB file and 64 MiB model-handoff limits do not apply to native CI artifacts; GitHub artifacts support the repository's configured 90-day maximum.

## Approaches considered

### 1. Promote one sealed changed-package bundle — selected

The PR catalog run uploads one package artifact containing only packages in the normal plan's `build` set, plus a separate small manifest artifact. Post-merge target runs validate those bytes, bypass compilation for the matching `build` set, and retain the existing release carry-over for the plan's `reuse` set.

This avoids retaining the roughly 1.72 GB complete catalog on every recipe PR while keeping one cross-run download and one manifest contract.

### 2. Reuse individual package-job artifacts

Each target run could probe and download `native-package-<name>` artifacts from the PR run. This requires per-package artifact lookup and metadata, partial-run coordination, duplicate dependency outputs and many cross-run downloads. It adds more states and races than one sealed changed-package bundle.

### 3. Continue rebuilding after merge

This is the current safe behavior, but wastes runner time and delays release after an already successful package build.

## Candidate bundle contract

A successful `publish=false` catalog run uploads two run/attempt-unique artifacts with 90-day retention:

- candidate packages: the exact `.txz` files named by the plan's `builds` mapping for packages in `build`
- candidate manifest: one `candidate-manifest.json`

The manifest has a fixed schema and records:

- repository and PR number
- candidate head commit and tree SHA
- workflow ID, run ID and run attempt
- package artifact ID/name and manifest artifact ID/name
- baseline version/image SHA, toolchain digest and rebuild generation
- package-build source-date epoch
- sorted changed-package records: name, build number, reuse-compatibility key, file, SHA256 and size

The manifest digest is not self-referential. After the successful run, the trusted controller lists artifacts for that exact run/attempt, resolves both exact names and IDs, downloads only the small manifest artifact, validates it, hashes `candidate-manifest.json`, and stores the digest with the artifact tuple.

No target-specific compatibility claim is reused. The final release retains the validated candidate manifest beside final provenance so package-byte origin remains independently inspectable after the 90-day CI artifacts expire.

## PR lifecycle

Normal and orphan-recovery creation use `draft: false`. Existing code that converts a changed or retried head back to draft is removed. A head change clears stored candidate identity, sets `nerdtools/candidate-build` pending on the new head, and dispatches a new catalog build.

The controller accepts a candidate only after the exact-head catalog workflow succeeds and both expected non-expired artifacts exist. It validates and stores:

- candidate head SHA
- run ID and attempt
- package artifact ID/name
- manifest artifact ID/name
- candidate-manifest SHA256
- successful build URL

The `main` branch protection requires one approving human review, dismisses stale approvals when the head changes, and grants automation no bypass. It intentionally does not require this recipe-specific status, which would block unrelated PRs. A premature merge is therefore possible at GitHub level, but the trusted merge callback refuses immediate publication without a valid exact-head candidate; scheduled discovery may rebuild later.

## Merge and release dispatch

The merge callback verifies human approval and the stored candidate tuple against the merged PR head. It passes the tuple plus PR number and merge SHA to `unraid-releases.yml`.

`unraid-releases.yml` defines optional candidate inputs, includes candidate identity in target-run naming/deduplication, and forwards a complete tuple to every `build-packages.yml` target dispatch. Candidate mode requires all fields; partial tuples fail.

Scheduled release discovery has no candidate tuple and retains the existing normal-build path. If merge-triggered candidate reuse fails closed, a later scheduled run may rebuild normally without candidate inputs.

## Candidate verification and reuse

A trusted candidate job in `build-packages.yml`:

1. Resolves the exact run attempt and both artifacts through GitHub Actions APIs.
2. Requires same repository, expected workflow, successful conclusion, `publish=false`, expected branch and candidate head.
3. Downloads only the stored artifact IDs with `actions:read`; the action's digest verification remains enabled.
4. Verifies the stored manifest SHA256 and strict manifest schema.
5. Rejects links, special files, duplicate names, unexpected paths and partial package sets.
6. Runs the normal current plan against the previous release.
7. Requires manifest package names/files/build numbers to equal the plan's complete `build`/`builds` set.
8. Recomputes current reuse-compatibility keys and requires exact equality for every candidate package.
9. Verifies each package SHA256/size and runs the existing package archive inspector.

When valid, `package-list` returns no packages to compile, preserves the normal plan's unchanged `reuse` set, and the final build receives candidate `.txz` files as `--package-artifacts`. The existing previous-release download then adds only unchanged packages. When invalid in candidate mode, the workflow fails; it never silently rebuilds or publishes stale bytes.

The final build regenerates catalog, checksums, provenance, source bundle and target evidence from the merged checkout. Provenance and the final release include the candidate manifest and record candidate PR, head, run, attempt and artifact identity as the origin of changed package bytes.

## Security and races

- Candidate state and its status check are cleared/reset on every automation head change and retry.
- The workflow run ID and attempt must both match; a later rerun cannot inherit an earlier artifact.
- Artifact lookup is by exact stored IDs/names/run, never newest-by-name alone.
- Merge-tree drift is allowed only when the normal changed-package plan and every candidate reuse-compatibility key remain identical.
- Candidate verification happens before package-job suppression and again before final artifact consumption, using the same manifest digest.
- Candidate-mode prior-release carry-over cannot overwrite candidate filenames and is restricted to the normal plan's `reuse` set.
- Human approval, durable merge attestation and target publication checks remain mandatory.
- No candidate artifact contains proprietary Unraid content.

## Maintainer experience

1. Discovery opens one ready batch PR with `nerdtools/candidate-build` pending.
2. Checks show the catalog build as pending/running/failed/successful.
3. The successful run leaves a downloadable 90-day changed-package bundle and marks the candidate status successful.
4. Bot and human reviews happen without draft conversion; branch protection requires one human approval.
5. Merge closes accepted recipe issues.
6. The merge callback blocks immediate publication unless the exact-head candidate is valid.
7. Post-merge target workflows reuse changed packages, carry unchanged packages from the previous release, rerun compatibility gates and publish only after success.
8. Missing or stale candidate data blocks the merge-triggered release; scheduled discovery can later rebuild normally.

## Verification

- Unit tests for candidate manifest generation, strict schema, transactional hash/package-set validation and build-key mismatch.
- Workflow tests for exact run/attempt/artifact IDs, candidate-aware run deduplication and complete/partial candidate inputs.
- Controller tests for ready PR creation, pending/success/failure candidate status, tuple persistence/clearing and merge dispatch.
- Native-plan tests proving candidate packages exactly cover `build`, previous-release packages exactly cover `reuse`, no candidate filename is overwritten, and package inspection runs.
- A nonpublishing live test: create a controlled recipe change, retain its candidate artifacts, run a post-merge-equivalent target workflow with candidate inputs, and prove package compile jobs are skipped while target gates pass.
- Existing upstream automation, native build, workflow generation and security suites remain green.
