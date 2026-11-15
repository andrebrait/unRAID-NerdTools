# Upstream Recipe Automation

## Approved scope

Daily discovery produces one batch PR, not per-version issues. One agent processes all candidates, evaluates source provenance and licensing, ports acceptable updates, and repairs failures from the existing native build/test workflows. Confirmed review bots participate; a human approves and merges. Publication remains behind exact-revision authorization and the native compatibility gates.

The user selected the official Codex and Claude Code actions instead of a custom runtime or OMP/Pi harness. The primary model is GPT-6 Luna with xhigh reasoning; Haiku 5.5 at medium effort is the availability fallback. No automatic premium escalation.

## Architecture

- **Vendor actions own agent execution:** pinned Codex and Claude Code releases provide ordinary Bash and file tools with unattended permissions. Codex runs first; Claude runs only after a qualifying provider failure.
- **The model runs under a dedicated non-sudo account:** its writable scratch workspace contains recipe sources, not Git credentials. Trusted setup, collection and publication run separately. Codex's API proxy and a root-owned local Anthropic proxy keep provider keys out of model commands.
- **Network access permits source investigation and downloads:** the worker has no publication authority. Network access is not an exfiltration boundary for workspace contents; credentials are kept outside that workspace and account.
- **The controller owns acceptance and rollback:** an immutable preparation artifact records the base and batch state. The controller validates returned archives and reports before import, preserves existing package policy and smoke assertions, rejects stale state, restores rejected candidates, regenerates pins, and runs trusted validators. Only a fresh trusted job operates GitHub and dispatches native builds.
- **Approval evidence is durable:** after verifying human approval of the exact merged head, the trusted merge callback records approval on the merge commit. Later publication checks require that trusted, revision-bound evidence rather than the former approver retaining write permission forever.

The worker account's processes are terminated before artifact collection. The collector rejects links, special files, traversal and oversized archives. Provider fallback requires a structured top-level Codex availability error, a failed run, and an unchanged safe workspace; tool output or model prose cannot authorize fallback.

## Deliberate limits

- Use `GITHUB_TOKEN`, not a PAT. The live [token probe](https://github.com/andrebrait/unRAID-NerdTools/actions/runs/43559456478) confirmed it cannot push new workflow content. Affected recipe changes are reported for manual handling; compatible updates remain in the batch.
- Builds run separately without provider credentials. Agent assertions are not build or compatibility evidence.
- Provider fallback is permitted only for availability failures before scratch changes. Safety refusal, malformed reports and exhausted limits do not trigger another model.
- A model's licensing or provenance assessment is not a security proof. Human review remains required.
- Closed unmerged PRs and merge conflicts stop for a human instead of discarding work.
- A review request is not a completed review. Bot integrations require actual repository evidence or explicit owner configuration.

## Verification

1. Exercise account and credential isolation: no provider keys, GitHub write credentials or writable trusted collector available to model commands; terminate all worker processes before collection.
2. Test acceptance against forbidden paths, changed smoke checks, forged reports and source-policy changes.
3. Test exact-head CI/review matching, rejected-candidate rollback, and approval surviving later reviewer-permission changes.
4. Run a real inexpensive model-backed recipe update in scratch and feed the accepted result to the existing read-only build gates before merging automation.
5. Run plugin/native checks only where affected; never publish a package as part of the evaluation.

## Alternatives

The first implementation supplied its own provider loops and tools. A subsequent OMP adapter still duplicated capabilities already available in vendor-maintained actions and added context overhead. Pi was considered but not shipped. The selected design uses vendor tools directly while retaining deterministic acceptance, artifact isolation and human approval.

## Applicability to the OMP integration build

Deterministic work comes first. Attempt every independent PR rebase and preserve successful results even when another rebase fails. A stacked child cannot be rebased safely when its parent failed.

Only a completely successful rebase stage proceeds directly to AI-free sequential squashes onto a fresh upstream base. Squashing stops at the first conflict because subsequent squashes depend on the accumulated result. A failed rebase stage or squash stage invokes targeted AI repair, followed by deterministic gates.

The existing OMP pipeline already follows this broad sequence. Reusing the vendor-action execution approach does not justify replacing its deterministic rebase and squash machinery.
