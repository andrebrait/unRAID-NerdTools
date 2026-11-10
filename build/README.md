# Native builds

The original repository contains 75 package artifacts: 74 tool/library names and proprietary RAR. `baseline.json` records the complete name set; Unraid's liburing is informational provider metadata, never replaced. RAR is an external official download only. Native recipes and runtime dependencies have SHA256-pinned inputs. PowerShell7.6.9 now has a source recipe using a pinned SDK, build-only bootstrap and local package feed; these binary development inputs are not represented as source code or substituted for the application build. Publication rejects any future catalog application still declared `upstream-binary`. No complete-catalog build success is implied.

No successful build or compatibility result is implied by committing these recipes. A release is created only after the Actions gates below actually pass.

## Pinned baseline and isolated roots

`baseline.json` is the canonical Unraid **7.3.4** input lock. Its ZIP SHA256 was obtained from the [official release feed](https://releases.unraid.net/os) and independently checked against the downloaded versioned ZIP; its `bzroot` digest was independently checked too. The bootstrap extracts the compressed root filesystem from `bzroot` and the `/usr` SquashFS layer from `bzmodules`, both authenticated by the ZIP checksum.

The official root is unpacked only inside a disposable Linux x86_64 runner. SHA256-locked development packages are added to a separate copy. Existing baseline runtime files are not replaced, including glibc and libstdc++. Standard makepkg symlinks are restored only when absent; arbitrary toolchain install scripts are never executed. Each package gets a fresh copy of that development root, its declared dependency packages, checked recipe files, and verified input archives. Compilation has no network access.

Compilation uses Bubblewrap with a separate user namespace mapping guest IDs0–65535 to host IDs100000–165535. The process has no host-root identity or initial-namespace capabilities; only user-namespace-scoped file-ownership capabilities are retained. Bubblewrap exposes the private copied root and minimal devices, with separate PID/network/IPC/UTS namespaces. Bare chroot is not the isolation boundary. GitHub Ubuntu24.04 setup explicitly installs bubblewrap/uidmap and grants the root mapper that subordinate range; a functional mapping/host-path/mount-denial probe must pass before compilation. Namespace restrictions fail closed, with no unsandboxed fallback. Default Docker Desktop seccomp blocks this setup; use the actual GitHub VM or a separately configured disposable environment, not an assumed-compatible local container.

`recipe-files.json` pins every recipe file, sidecar, symlink target and executable bit. Validation rejects changed or newly added sidecars before copying any into a build. After intentional reviewed edits, update the recipe's lock hash and run `python3 build/native.py pin-recipes`; CI never regenerates pins automatically. Downloaded inputs cannot overwrite checked sidecars. Package and toolchain URL/filename/SHA256 schemas are checked up front without downloading. .NET invocations disable development-certificate generation and use a private CLI home inside the sandbox.

`inputs` contains downloaded build files only. Already checked-in recipe sidecars are copied from `build/recipes/<tool>` and pinned by `recipe-files.json`; they must not also be declared as downloads, even when their bytes match. Corresponding-source bundles include the checked build tree, retaining those sidecars without a second download.

The original Unraid ZIP, extracted roots and bootable test initramfs remain private to the job. They are neither uploaded as artifacts nor cached or published. Do not run this privileged build on an Unraid host or other persistent machine.

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

The internal libical dependency retains upstream source documentation but does not generate GTK-Doc API pages; its disabled introspection bindings are not part of the user-facing catalog.
PowerTOP includes private, source-built libtracefs and libtraceevent dependencies. The GnuPG build uses headers from the matching TPM2-TSS 4.1.3 source as a build-only dependency and links Unraid's existing TPM libraries.
Nmap's Zenmap includes source-built private Python bindings and generated GObject typelibs against baseline GTK libraries; an available graphical session is still required to display its windows.

`locks/toolchain.json` is an array of `{name,url,file,sha256}` inputs, separate from distributable package recipes. Toolchain versions must be selected against the baseline, not a mutable rolling image.

Recipe working directory: `/work/recipe`. Its input archives are copied alongside the `.SlackBuild`. Important environment values:

- `ARCH=x86_64`, `PKGTYPE=txz`, `BUILD=1`, `TAG=_NT73`
- `TMP=/tmp/nerdtools-build`, `OUTPUT=/out`
- `SOURCE_DATE_EPOCH` from `baseline.json`, `LC_ALL=C`, `TZ=UTC`
- fixed generic x86-64 C/C++ flags, not `-march=native`
- private library rpath and pkg-config paths under `/opt/nerdtools`

Each recipe produces exactly one `/out/*.txz`. Packages retain required features, while executable/library payloads use `/opt/nerdtools`. New service policy/configuration can use `/etc`, `/usr/share` and `/var`; collisions cannot overwrite baseline files. Generated `.new` hooks preserve existing settings and dangling links; upstream hooks are not executed and no daemon starts automatically. Private `bin`/`sbin` executables get owned `/usr/local/bin/nerdtools-<command>` launchers with process-scoped private PATH/library paths. The plugin may add separately tracked bare aliases only when the host command is absent; foreign commands are never overwritten or shadowed. Shared libraries never replace Unraid's libraries.

Each package build has a five-hour compilation limit inside the six-hour CI job; large source builds such as Node.js can exceed one hour on standard runners.

Secure locate tools retain the baseline `slocate` GID 21, database privacy permissions and narrowly allowed setgid executable metadata. Other unexpected ownership or privileged files are rejected, not normalized into a root-privileged executable.

Inputs, recipes, compiler/development packages, epoch and packaging are pinned or canonicalized. This is an inspectable repeatable build procedure, not a claim that every upstream build is bit-identical across independent runs. The default pipeline builds each package once; it does not double the catalog build cost to assert byte reproducibility.

## Acceptance and publication

`.github/workflows/build-packages.yml` first runs the build-runner regression tests, recipe digest validation, full catalog coverage and dependency-cycle checks. A dispatched native build then:

1. Validates all source/toolchain input structures, the complete checked recipe inventory, and every downloaded input SHA256.
2. Builds packages and their dependencies in parallel subordinate-ID Bubblewrap jobs after the functional isolation probe. The full-catalog job consumes only artifacts from that same workflow run; a missing or unexpected package fails the gate.
3. Audits package paths, links, privileges and ownership collisions, preserving pre-existing directory permissions. Only reviewed integration files are allowed outside the private prefix; the BlueZ root-executed udev rule and Screen PAM policy have independent exact byte-digest pins.
4. Installs all native tools and explicit runtime dependencies into a pristine target root.
5. Resolves private ELF dependencies and symbol versions against the target's own loader in a read-only sandbox, so the audit cannot alter trusted VM binaries. Host ELF parsing also runs without root identity/capabilities or privilege escalation.
6. Boots the exact target kernel in QEMU without disks, NIC or host mounts. A root-only PID1 harness holds an unpredictable nonce and protected per-command completion records. Smoke children run as UID65534 with no supplementary groups/capabilities and no-new-privs; they cannot read the nonce, forge completion through stdout, or regain root through setuid programs. The nonce is emitted only after every bounded command succeeds. Critical root harness programs do not inherit private library paths.
7. On an explicitly requested publishable build, produces native assets, corresponding-source inputs and exact adapted scripts in `sources-7.3.tar.xz`, provenance, acceptance evidence and checksums. RAR, the Unraid image/rootfs and supplemental Slackware toolchain archives are excluded. Application-specific SDK/bootstrap inputs retain their actual role and original licensing/notices. Every asset must be smaller than2GiB and the release have fewer than1000 assets; oversize output fails rather than losing source coverage. Individual package job artifacts expire after one day, including on read-only runs.

The VM checks kernel startup and declared command execution, not Bluetooth/IPMI hardware, physical disk recovery, daemon integration, or all possible tool operations. No hardware tuning or service activation is part of acceptance. A rootfs-only ABI result is not labeled a target-kernel pass.

`source-blockers.json` prevents reintroducing unlicensed Julusian/godocdown, chzyer/logex and robertkrimen/godocdown inputs. A maintenance-time offline Linux amd64 restic build, using empty caches and a file-only module proxy, succeeded without all three modules. Keep their nine lock/proxy inputs out of corresponding-source bundles; this proof permits removing unnecessary inputs, not redistributing them. The guard clears only when their hashes are absent or permission is established. A source hash or upstream availability alone is not a redistribution grant.

Manual dispatch inputs are `unraid_version`, `unraid_sha256`, `unraid_url`, `channel` (`stable` or `next`) and `publish` (default `false`). Daily release discovery dispatches read-only compatibility runs with `publish=false`, including public prereleases. Source compilation always uses the locked 7.3.4 baseline; the requested image is the compatibility target. Publication is restricted to explicitly requested stable 7.3-family runs after every gate succeeds. A prerelease observation or dispatched run is not evidence of compatibility.

Each package also has its own `package-<name>.yml` workflow. Pushes and pull requests use the complete commit diff and dependency graph to skip unaffected package builds; uncertain history rebuilds all. To debug one package independently, run `gh workflow run package-<name>.yml --ref <branch>`. These runs build its dependencies too but do not publish or claim full-catalog compatibility. Regenerate wrappers after adding or removing recipes with `python3 scripts/package-change-workflows.py`. Publication is permitted only from the default branch.

Published tags are unique `packages-7.3-<run_id>-<run_attempt>` and use `make_latest=false`, preserving the plugin release's `latest` link. Existing releases/assets are never pruned or overwritten. The plugin discovers the newest applicable catalog-bearing package release rather than assuming GitHub's latest release contains packages.

The catalog shape is:

```json
{"schema":1,"min_unraid":"7.3.4","family":"7.3","packages":[{"name":"nerdtools-example","tool":"example","version":"1.0","file":"nerdtools-example-1.0-x86_64-1_NT73.txz","sha256":"<SHA256>","size":123,"dependencies":[],"commands":["example"]}],"external_packages":[],"provided_packages":[{"name":"liburing","version":"2.15","provider":"unraid","min_unraid":"7.3.4"}]}
```

Each package file belongs to that same release. Optional `internal: true` marks an automatically managed dependency. `commands` names are derived from actual qualified archive launchers, not guessed from package names. `external_packages` is the link-only RAR offer; `provided_packages` is host-supplied information with no installer/removal path. Baseline-provided library metadata and actual target ELF symbol availability are checked before acceptance.

## Reusable bootstrap action

`build/unraid-build-root` is a composite action with no NerdTools catalog, recipe, QEMU or publication policy. Other plugin repositories can pin this repository's action subpath to a reviewed commit; no separate action repository is required.

Inputs: `version`, `url`, `sha256`, optional `bzroot-sha256`, and `toolchain-lock` (path in the consuming checkout). Outputs: `baseline-root`, `development-root`, `kernel`. It requires a disposable Ubuntu-style Linux x86_64 GitHub runner with sudo; it installs archive tools and creates private roots under `RUNNER_TEMP`. Its Python bootstrap module is self-contained and standard-library-only. Callers own their recipe execution, tests and publication. Never upload or cache those roots or the original Unraid image.

For local validation without builds:

```sh
python3 -m unittest discover -s build -p 'test_*.py'
python3 build/native.py validate
```

To reproduce a baseline build, run `sudo python3 build/native.py build` only in a disposable Linux x86_64 VM with the documented archive, ELF and QEMU tools. If an original source URL disappears, recover a retained corresponding-source release asset and pass `--source-bundle /path/to/sources-7.3.tar.xz`; each recovered input must still match its lock SHA256. Missing inputs fail closed. No source availability is promised before the first successful publication. Publication uses the GitHub workflow's explicit input; the local script never uploads anything.
