# Native builds

The original repository contains 75 package artifacts: 74 tool/library names and proprietary RAR. `baseline.json` records the complete name set; Unraid's liburing is informational provider metadata, never replaced. RAR is an external official download only. Native recipes and runtime dependencies have SHA256-pinned inputs. PowerShell7.6.9 now has a source recipe using a pinned SDK, build-only bootstrap and local package feed; these binary development inputs are not represented as source code or substituted for the application build. Publication rejects any future catalog application still declared `upstream-binary`. No complete-catalog build success is implied.

No successful build or compatibility result is implied by committing these recipes. A release is created only after the Actions gates below actually pass.

## Pinned baseline and isolated roots

`baseline.json` is the canonical Unraid **7.3.4** input lock. Its ZIP SHA256 was obtained from the [official release feed](https://releases.unraid.net/os) and independently checked against the downloaded versioned ZIP; its `bzroot` digest was independently checked too. The bootstrap extracts the compressed root filesystem from `bzroot` and the `/usr` SquashFS layer from `bzmodules`, both authenticated by the ZIP checksum.

The official root is unpacked only inside a disposable Linux x86_64 runner. SHA256-locked development packages are added to a separate copy. Existing baseline runtime files are not replaced, including glibc and libstdc++. Standard makepkg symlinks are restored only when absent; arbitrary toolchain install scripts are never executed. Each package gets a fresh copy of that development root, its declared dependency packages, checked recipe files, and verified input archives. Compilation has no network access.

Uncached source and toolchain downloads resolve every HTTPS destination to public addresses and pin the TLS connection to a validated address. Redirect destinations undergo the same checks; ambient proxies cannot bypass this boundary. Downloaded and cached inputs must still match their SHA256 pins.

Compilation uses Bubblewrap with a separate user namespace mapping guest IDs0–65535 to host IDs100000–165535. The process has no host-root identity or initial-namespace capabilities; only user-namespace-scoped file-ownership capabilities are retained. Bubblewrap exposes the private copied root and minimal devices, with separate PID/network/IPC/UTS namespaces. Bare chroot is not the isolation boundary. GitHub Ubuntu24.04 setup explicitly installs bubblewrap/uidmap and grants the root mapper that subordinate range; a functional mapping/host-path/mount-denial probe must pass before compilation. Namespace restrictions fail closed, with no unsandboxed fallback. Default Docker Desktop seccomp blocks this setup; use the actual GitHub VM or a separately configured disposable environment, not an assumed-compatible local container.

`recipe-files.json` pins every recipe file, sidecar, symlink target and executable bit. Validation rejects changed or newly added sidecars before copying any into a build. After intentional reviewed edits, update the recipe's lock hash and run `python3 build/native.py pin-recipes`; CI never regenerates pins automatically. Downloaded inputs cannot overwrite checked sidecars. Package and toolchain URL/filename/SHA256 schemas are checked up front without downloading. .NET invocations disable development-certificate generation and use a private CLI home inside the sandbox.

`inputs` contains downloaded build files only. Already checked-in recipe sidecars are copied from `build/recipes/<tool>` and pinned by `recipe-files.json`; they must not also be declared as downloads, even when their bytes match. Corresponding-source bundles include the checked build tree, retaining those sidecars without a second download.

The original Unraid ZIP, extracted roots and bootable test initramfs remain private to the job. They are neither uploaded as artifacts nor cached or published. Do not run this privileged build on an Unraid host or other persistent machine.

The host runner is explicitly pinned to Ubuntu 24.04, not `ubuntu-latest`. Ubuntu 26.04 host utility changes therefore do not affect the current jobs. Recipe commands use the locked Unraid development root's utilities, not the host's coreutils; a future runner upgrade still requires bootstrap and sandbox verification.

## Recipes and locks

`locks/slackware.json` and `locks/slackbuilds.json` are arrays of:

```json
{
  "name": "example",
  "version": "1.0",
  "recipe": "build/recipes/example/example.SlackBuild",
  "recipe_sha256": "<SHA256 of the checked recipe>",
  "provenance": {"kind": "source", "url": "<upstream recipe URL>", "revision": "<immutable revision>"},
  "inputs": [{"url": "https://upstream.example/example-1.0.tar.gz", "file": "example-1.0.tar.gz", "sha256": "<SHA256>"}],
  "dependencies": [],
  "build_dependencies": [],
  "smoke": [["/opt/nerdtools/bin/example", "--help"]]
}
```

The example documents the schema, not an actual recipe or fallback. Real entries require complete pins and files. `provenance.kind` is `source` or `upstream-binary`; binary repacks are never described as source-built. Extra runtime dependencies use `internal: true`, are explicit assets and have sole file ownership. Build-only dependencies use `catalog: false`. Every original name must be covered by a native package or explicit baseline provider. Recipe graph names stay original; emitted Slackware/catalog identities and dependency edges use `nerdtools-<original>`, with `tool` retaining the display/migration identity.

Prefer versioned upstream archive URLs over source files in rolling mirror directories, which can disappear when the mirror advances. Replacing an unavailable location still requires verifying the pinned bytes. If the archive encoding changes, verify and pin the new archive, confirm the source version and extraction layout, and keep the recipe version unchanged unless an upgrade is intended.

The internal libical dependency retains upstream source documentation but does not generate GTK-Doc API pages; its disabled introspection bindings are not part of the user-facing catalog.
PowerTOP includes private, source-built libtracefs and libtraceevent dependencies. The GnuPG build uses headers from the matching TPM2-TSS 4.1.3 source as a build-only dependency and links Unraid's existing TPM libraries.
Nmap retains Ndiff and Zenmap. Zenmap includes source-built private Python bindings and generated GObject typelibs against baseline GTK libraries, plus a desktop entry; displaying windows requires an available graphical session.

Node.js reuses the [official Slackware recipe](https://git.slackware.nl/current/plain/source/l/nodejs/nodejs.SlackBuild?id=8dbbc18b2042228d0189b2ddf267c1028c860f61), adapted to the private prefix, rather than inventing a build process. Existing [Unraid packages](https://github.com/shinji257/unraid_pkgs) were evaluated, but their older Node package has no checked Node recipe there. Importing Slackware's `/usr` binary would still require npm/path adaptation and exact-baseline ABI validation, so the existing source recipe remains the simpler auditable choice.

`locks/toolchain.json` is an array of `{name,url,file,sha256}` inputs, separate from distributable package recipes. Toolchain versions must be selected against the baseline, not a mutable rolling image.

Recipe working directory: `/work/recipe`. Its input archives are copied alongside the `.SlackBuild`. Important environment values:

- `ARCH=x86_64`, `PKGTYPE=txz`, `BUILD=1`, `TAG=_NT73`
- `TMP=/tmp/nerdtools-build`, `OUTPUT=/out`
- `SOURCE_DATE_EPOCH` is the commit time of the commit being built (set by the workflows; the `baseline.json` date when run locally), `LC_ALL=C`, `TZ=UTC`. Tools that print a build date therefore show when that package was last compiled. A carried-over package keeps the date of the commit that compiled it.
- Package files use Slackware's BUILD field, e.g. `nerdtools-vim-9.2.1252-x86_64-2_NT73.txz`. The catalog plan keeps a carried-over package's number, gives a recompiled package of the same upstream version the previous number plus one, and restarts at 1 for a new upstream version. A different binary therefore always has a different package name, and the plugin offers it as an update. The plan's numbers and date are recorded in `provenance-<family>.json` with the commit, so a package can be rebuilt with the same name, date and contents.
- fixed generic x86-64 C/C++ flags, not `-march=native`
- private library rpath and pkg-config paths under `/opt/nerdtools`

Each recipe produces exactly one `/out/*.txz`. Packages retain required features, while executable/library payloads use `/opt/nerdtools`. New service policy/configuration can use `/etc`, `/usr/share` and `/var`; collisions cannot overwrite baseline files. Generated `.new` hooks preserve existing settings and dangling links; upstream hooks are not executed and no daemon starts automatically. Private `bin`/`sbin` executables get owned `/usr/local/bin/nerdtools-<command>` launchers with process-scoped private PATH/library paths. The plugin may add separately tracked bare aliases only when the host command is absent; foreign commands are never overwritten or shadowed. Shared libraries never replace Unraid's libraries.

Each package build has a five-hour compilation limit inside the six-hour CI job; large source builds such as Node.js can exceed one hour on standard runners.

Secure locate tools retain the baseline `slocate` GID 21, database privacy permissions and narrowly allowed setgid executable metadata. Other unexpected ownership or privileged files are rejected, not normalized into a root-privileged executable.

Inputs, recipes, compiler/development packages, epoch and packaging are pinned or canonicalized. This is an inspectable repeatable build procedure, not a claim that every upstream build is bit-identical across independent runs. The default pipeline builds each package once; it does not double the catalog build cost to assert byte reproducibility.

## Acceptance and publication

`.github/workflows/build-packages.yml` first runs the build-runner regression tests, recipe digest validation, full catalog coverage and dependency-cycle checks. A dispatched native build then:

1. Validates all source/toolchain input structures, the complete checked recipe inventory, and every downloaded input SHA256.
2. Builds packages and their dependencies in parallel subordinate-ID Bubblewrap jobs after the functional isolation probe. Each package job then installs only that package's runtime closure into its own pristine target root and runs the ELF audit and kernel smoke test for it, so packages are verified independently and in parallel. The full-catalog job consumes only artifacts from that same workflow run; a missing or unexpected package fails the gate. It repeats the audit and smoke test on the combined installation to catch collisions and cross-package interference.
3. Audits package paths, links, privileges and ownership collisions, preserving pre-existing directory permissions. Only reviewed integration files are allowed outside the private prefix; the BlueZ root-executed udev rule and Screen PAM policy have independent exact byte-digest pins.
4. Installs all native tools and explicit runtime dependencies into a pristine target root.
5. Resolves private ELF dependencies and symbol versions against the target's own loader in a read-only sandbox, so the audit cannot alter trusted VM binaries. Host ELF parsing also runs without root identity/capabilities or privilege escalation.
6. Boots the exact target kernel in QEMU without disks, NIC or host mounts. Smoke commands run as root with a clean environment, because Unraid runs everything as root and several tools refuse to start otherwise. The PID1 harness records per-command completion and emits an unpredictable nonce only after every bounded command succeeds. This is compatibility evidence, not a defence against a malicious package: a root smoke command could forge it. Critical harness programs do not inherit private library paths.
7. On an explicitly requested publishable build, produces native assets, corresponding-source inputs and exact adapted scripts in `sources-7.3.tar.xz`, provenance, acceptance evidence and checksums. RAR, the Unraid image/rootfs and supplemental Slackware toolchain archives are excluded. Application-specific SDK/bootstrap inputs retain their actual role and original licensing/notices. Every asset must be smaller than2GiB and the release have fewer than1000 assets; oversize output fails rather than losing source coverage. Individual package job artifacts expire after one day, including on read-only runs.

The VM checks kernel startup and declared command execution, not Bluetooth/IPMI hardware, physical disk recovery, daemon integration, or all possible tool operations. No hardware tuning or service activation is part of acceptance. A rootfs-only ABI result is not labeled a target-kernel pass.

`source-blockers.json` prevents reintroducing unlicensed Julusian/godocdown, chzyer/logex and robertkrimen/godocdown inputs. A maintenance-time offline Linux amd64 restic build, using empty caches and a file-only module proxy, succeeded without all three modules. Keep their nine lock/proxy inputs out of corresponding-source bundles; this proof permits removing unnecessary inputs, not redistributing them. The guard clears only when their hashes are absent or permission is established. A source hash or upstream availability alone is not a redistribution grant.

Manual dispatch inputs are `unraid_version`, `unraid_sha256`, `unraid_url`, `channel` (`stable` or `next`) and `publish` (default `false`). Daily release discovery dispatches a publishing run for every official release at or above the baseline that has no successful run yet, including public betas and release candidates, so packages exist before a release becomes stable. Source compilation always uses the locked 7.3.4 baseline, the oldest supported system; the requested image is the compatibility target. A release is published only after every gate succeeds on that exact target. A failed run publishes nothing and is retried daily; it means a recipe fix or a newer build baseline is needed for that version.

Each family (`7.3`, `7.4`, ...) has its own `catalog-<family>.json`. For the baseline family its `min_unraid` is the baseline; for a later family it is the first version, prerelease or not, that passed. The plugin reads only the catalog of its own family and refuses versions older than `min_unraid`, so an untested family gets nothing. A later version of an already published family is served the existing catalog until its own run finishes.

A catalog run compiles only what changed. Every package has a build identity: a hash of its lock entry and inputs, its checked recipe files, the baseline and toolchain locks, the root bootstrap script, `REBUILD_GENERATION` in `native.py`, and the identities of its dependencies. Other edits to the build scripts do not rebuild anything; raise `REBUILD_GENERATION` to deliberately recompile every package. The previous release's identities are recomputed from its recorded commit with the current code, so changing how identities are computed never forces rebuilds by itself. Smoke commands are excluded. Each release publishes these in `build-keys.json`. A later run carries over, from the newest release, every package whose identity is unchanged, verifying its SHA256 against that release's catalog, and compiles the rest. A new Unraid version with unchanged recipes therefore compiles nothing; a fixed recipe rebuilds that package and its dependents. Inside a run, a package job never recompiles a dependency: an unchanged dependency is downloaded from the previous release with its checksum verified, and a changed one is built once in the same run and taken from that job. `build-packages.yml` has one job per catalog package, generated by `scripts/package-change-workflows.py`, that waits only for its own dependencies' jobs, so any dependency depth works and unrelated packages never wait for each other. Jobs for packages the plan does not list are skipped. Carried-over and freshly compiled packages then pass the same combined installation, ABI audit and kernel smoke test on the target, and every release still contains the complete package set.

Each package also has its own `package-<name>.yml` workflow. Pushes and pull requests use the complete commit diff and dependency graph to skip unaffected package builds; uncertain history rebuilds all. To debug one package independently, run `gh workflow run package-<name>.yml --ref <branch>`. These runs build its dependencies too but do not publish or claim full-catalog compatibility. Regenerate wrappers after adding or removing recipes with `python3 scripts/package-change-workflows.py`. Publication is permitted only from the default branch.

Published tags are unique `packages-<family>-<run_id>-<run_attempt>` and use `make_latest=false`, preserving the plugin release's `latest` link. Existing releases/assets are never pruned or overwritten. The plugin discovers the newest applicable catalog-bearing package release rather than assuming GitHub's latest release contains packages.

The catalog shape is:

```json
{"schema":1,"min_unraid":"7.3.4","family":"7.3","packages":[{"name":"nerdtools-example","tool":"example","version":"1.0","file":"nerdtools-example-1.0-x86_64-1_NT73.txz","sha256":"<SHA256>","size":123,"dependencies":[],"commands":["example"]}],"external_packages":[],"provided_packages":[{"name":"liburing","version":"2.15","provider":"unraid","min_unraid":"7.3.4"}]}
```

Each package file belongs to that same release. Optional `internal: true` marks an automatically managed dependency. `commands` names are derived from actual qualified archive launchers, not guessed from package names. `external_packages` is the link-only RAR offer; `provided_packages` is host-supplied information with no installer/removal path. Baseline-provided library metadata and actual target ELF symbol availability are checked before acceptance.

## Upstream recipe automation

`.github/workflows/upstream-versions.yml` compares locked recipes with Slackware-current and SlackBuilds.org daily and resumes after catalog builds. It no longer opens per-version issues. One bounded agent session handles the complete batch and maintains `automation/upstream-recipes` with one pull request titled "Update upstream recipes". The trusted controller `scripts/upstream-updates.py` owns GitHub operations and commits only recipes, package locks and their inventory. The agent cannot execute commands, read credentials or edit acceptance code. Dependency-graph updates that would require generated workflow changes are rejected with an explicit manual-handling reason: a live Actions probe confirmed that `GITHUB_TOKEN` cannot push workflow-file changes. Other passing updates remain in the batch. Rejected recipes and unused dependencies return to their state on `main`.

Secrets and variables:

- Environment `Upstream Updates` (deployment branch `main` only) provides the secrets `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`. Only the `update` step receives them. Recipe builds never do.
- `OPENAI_MODEL` defaults to `gpt-6-luna` with **high** reasoning; `ANTHROPIC_MODEL` defaults to `claude-haiku-5-5` with **medium** effort. Optional model variables override the model IDs. These settings match the [OMP repair-agent configuration](https://github.com/ompweb-dev/oh-my-pi/blob/f936e49c1a7984a8755a05561bcd0511c89d794d/.github/actions/omp-agent/action.yml).
- `UPSTREAM_REVIEW_BOTS` is an optional repository-level variable containing comma-separated review bot logins that the owner confirms are installed here, for example `coderabbitai[bot],chatgpt-codex-connector[bot]`. Both the agent and feedback relay receive it, so configured comment-only bots can trigger repairs before submitting a review. Bot-authored reviews also establish availability. Copilot is never requested. Unverified integrations are reported rather than tagged as discovery probes; a request is not counted as a completed review.

No personal access token is used. The automation authenticates only with the workflow's `GITHUB_TOKEN`. Pushes made with that token do not start workflows, so `update` explicitly dispatches `build-packages.yml` on the automation branch with `publish=false` (a read-only catalog build). When that build completes, `upstream-versions.yml` runs again and reads the exact current pull request head before acting on results.

Under repository **Settings → Actions → General → Workflow permissions**, enable **Allow GitHub Actions to create and approve pull requests**. GitHub combines these capabilities in one setting; this automation never exercises approval. Protect `main` with required CI and human approval, dismiss stale approvals, and give the automation no bypass. The main-only environment needs no reviewer gate: approval belongs on the generated pull request, not every scheduled assessment.

`upstream-feedback.yml` relays current-head reviews and comments from confirmed bots and repository contributors to `main`. The controller independently filters authors, stale feedback, approvals and simple acknowledgements before calling a paid model. The relay checks out `main`, never the pull request branch, and receives no provider secrets. Unavailable review integrations are reported rather than discovered by tagging them.

Repairs are bounded to three sessions per unchanged candidate batch once a PR exists, sharing one 30-minute deadline per workflow run. A new batch resets the budget; assessments that leave an already-passing head unchanged do not consume a repair attempt. Exhaustion, merge conflicts and explicitly closed unmerged PRs require human intervention; automation does not discard that history. Rejections remain documented in the PR or workflow summary and are reassessed on later scheduled runs rather than permanently ignored. Anthropic handles provider availability failures only, never refusals or safety decisions. Large pinned module feeds use paged reads and partial updates; trusted code hashes new inputs in batches, with bounded download counts, bytes, tool calls and output.

Human approval is required, and automation never approves, merges or changes repository settings. The merge callback and daily release discovery check current-head approval by a human with repository write access, including earlier automation merges contained in the source revision. The publishing job repeats authorization immediately before creating a release, using its own immutable `GITHUB_SHA` and checkout rather than the discovery run's earlier revision. Publication still requires every existing compatibility gate. Deduplication requires the exact target, current `main` SHA and a publishing run; read-only successes do not suppress publication. Consequently, a new `main` commit can trigger fresh target checks even when its package binaries are carried over unchanged.

This section describes intended behavior. Nothing here shows that the agent or a native build has run live. Check the first real run on its pull request, and treat no build or release as verified until its workflows pass.

The provider protocols follow [OpenAI Responses function calling](https://developers.openai.com/api/docs/guides/function-calling) and [Anthropic Messages tool definitions](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools). Explicit build dispatch follows [GitHub's token-trigger restrictions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

## Reusable bootstrap action

`build/unraid-build-root` is a composite action with no NerdTools catalog, recipe, QEMU or publication policy. Other plugin repositories can pin this repository's action subpath to a reviewed commit; no separate action repository is required.

Inputs: `version`, `url`, `sha256`, optional `bzroot-sha256`, and `toolchain-lock` (path in the consuming checkout). Outputs: `baseline-root`, `development-root`, `kernel`. It requires a disposable Ubuntu-style Linux x86_64 GitHub runner with sudo; it installs archive tools and creates private roots under `RUNNER_TEMP`. Its Python bootstrap module is self-contained and standard-library-only. Callers own their recipe execution, tests and publication. Never upload or cache those roots or the original Unraid image.

For local validation without builds:

```sh
python3 -m unittest discover -s build -p 'test_*.py'
python3 build/native.py validate
```

To reproduce a baseline build, run `sudo python3 build/native.py build` only in a disposable Linux x86_64 VM with the documented archive, ELF and QEMU tools. If an original source URL disappears, recover a retained corresponding-source release asset and pass `--source-bundle /path/to/sources-7.3.tar.xz`; each recovered input must still match its lock SHA256. Missing inputs fail closed. No source availability is promised before the first successful publication. Publication uses the GitHub workflow's explicit input; the local script never uploads anything.
