# Upstream Recipe Automation

## Approved scope

Daily discovery produces one batch PR, not per-version issues. One agent processes all candidates, evaluates source provenance and licensing, ports acceptable updates, and repairs failures from the existing native build/test workflows. Confirmed review bots participate; a human approves and merges. Publication remains behind exact-revision authorization and the native compatibility gates.

The user selected pinned OMP instead of maintaining a custom provider runtime. Models match the existing OMP maintenance setup: GPT-6 Luna with high reasoning, with Haiku 5.5 at medium effort as the availability fallback. No automatic premium escalation.

## Architecture

- **OMP owns agent execution:** provider protocols, conversation, token handling, tool calls, and run limits. A versioned Linux binary is verified against a repository-pinned SHA256.
- **The model owns only a scratch checkout:** explicit trusted tools run commands in a disposable Docker container with no network, no capabilities, a read-only root filesystem, and only the scratch workspace mounted writable. Host Git state, provider credentials, GitHub tokens, and the Docker socket are not mounted.
- **Network access is a separate trusted operation:** public HTTPS downloads reuse the bootstrap's pinned-address transport. Archives can be inspected offline in the sandbox. Output and download limits apply.
- **The controller owns acceptance and rollback:** it validates scratch changes before import, preserves existing package policy and smoke assertions, restores rejected candidates against `main`, regenerates pins, and runs trusted validators. It alone operates GitHub and dispatches native builds.
- **Approval evidence is durable:** after verifying human approval of the exact merged head, the trusted merge callback records approval on the merge commit. Later publication checks require that trusted, revision-bound evidence rather than the former approver retaining write permission forever.

Native OMP tools, ambient extensions, rules, skills and context-file discovery are disabled. Only the explicit workspace/fetch/finish adapter is available. The model cannot replace its own host-side tools or forge a finish report through workspace commands.

## Deliberate limits

- Use `GITHUB_TOKEN`, not a PAT. The live [token probe](https://github.com/andrebrait/unRAID-NerdTools/actions/runs/43559456478) confirmed it cannot push new workflow content. Affected recipe changes are reported for manual handling; compatible updates remain in the batch.
- Builds run separately without provider credentials. Agent assertions are not build or compatibility evidence.
- Provider fallback is permitted only for availability failures before scratch changes. Safety refusal, malformed reports and exhausted limits do not trigger another model.
- A model's licensing or provenance assessment is not a security proof. Human review remains required.
- Closed unmerged PRs and merge conflicts stop for a human instead of discarding work.
- A review request is not a completed review. Bot integrations require actual repository evidence or explicit owner configuration.

## Verification

1. Exercise sandbox isolation: no credentials, host filesystem, Git metadata, network or Docker socket visible to model commands.
2. Test acceptance against forbidden paths, changed smoke checks, forged reports and source-policy changes.
3. Test exact-head CI/review matching, rejected-candidate rollback, and approval surviving later reviewer-permission changes.
4. Run a real inexpensive model-backed recipe update in scratch and feed the accepted result to the existing read-only build gates before merging automation.
5. Run plugin/native checks only where affected; never publish a package as part of the evaluation.

## Alternatives

The first implementation supplied its own provider loops, file tools, rollback snapshots and download timers. That duplicated the OMP execution layer and accumulated maintenance and security burden. The selected design reuses OMP while retaining a small trusted adapter and controller. Running OMP unrestricted with host-shell access was rejected: a prompt is not a credential or filesystem security boundary.
