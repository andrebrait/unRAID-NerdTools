# Graph Report - implement-maintenance  (2026-11-10)

## Corpus Check
- 117 files · ~18,546 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 186 file(s) not represented in the graph (top: .txz 173, (none) 5, .css 4)

## Summary
- 1730 nodes · 1904 edges · 124 communities (111 shown, 13 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 38 edges (avg confidence: 0.85)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ef4fd747`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- NerdTools
- .apply
- channel
- publish
- env
- steps
- with
- check-unraid-releases.py
- build-packages.yml
- package-pycairo.yml
- ReleaseFeedTests
- nt_validate_catalog
- packageQuery
- DownloadHelpers.php
- NerdTools
- unraidmpkg.sh
- README.md
- build-plugin.sh
- pkg_build.sh
- GitImpactTests
- steps
- NerdToolsHelpers.php
- package-alsa-lib.yml
- package-aria2.yml
- package-audit.yml
- package-autossh.yml
- package-axel.yml
- package-bc.yml
- package-bluez.yml
- package-borgbackup.yml
- package-btop.yml
- package-bwm-ng.yml
- package-cksfv.yml
- package-ddrescue.yml
- package-exiftool.yml
- package-fdupes.yml
- package-fio.yml
- package-fish.yml
- package-fzf.yml
- package-gnupg2.yml
- package-gpgme.yml
- package-iftop.yml
- package-iotop.yml
- package-ipmitool.yml
- package-irssi.yml
- package-jdupes.yml
- package-lftp.yml
- package-libassuan.yml
- package-libical.yml
- package-libjodycode.yml
- package-libksba.yml
- package-libmediainfo.yml
- package-libnl.yml
- package-libsodium.yml
- package-liburing.yml
- package-libyaml.yml
- package-libzen.yml
- package-llfuse.yml
- package-lua.yml
- package-mediainfo.yml
- package-mktorrent.yml
- package-mlocate.yml
- package-msgpack-python.yml
- package-ncdu.yml
- package-neofetch.yml
- package-netcat.yml
- package-nload.yml
- package-nmap.yml
- package-nodejs.yml
- package-npth.yml
- package-p7zip.yml
- package-par2cmdline.yml
- package-pigz.yml
- package-pinentry.yml
- package-plocate.yml
- package-powershell.yml
- package-powertop.yml
- package-python2.yml
- package-python3.yml
- package-python-packaging.yml
- package-python-pip.yml
- package-python-setuptools.yml
- package-rar2fs.yml
- package-rclone.yml
- package-restic.yml
- package-rsnapshot.yml
- package-ruby.yml
- package-screen.yml
- package-socat.yml
- package-sshfs.yml
- package-sshpass.yml
- package-stress.yml
- package-stunnel.yml
- package-tcl.yml
- package-tcllib.yml
- package-tcpdump.yml
- package-tmux.yml
- package-unrar.yml
- package-utf8proc.yml
- package-vim.yml
- package-wget2.yml
- package-zsh.yml
- with
- with
- package-pygobject3.yml
- plugin_aliases.php
- package
- package-zenmap-typelibs.yml
- steps
- jobs
- package-libtraceevent.yml
- package-list
- publish
- package-libtracefs.yml
- package-tpm2-tss.yml
- .refresh
- .syncAliases

## God Nodes (most connected - your core abstractions)
1. `NerdTools` - 25 edges
2. `ReleaseFeedTests` - 17 edges
3. `with` - 16 edges
4. `nt_validate_catalog()` - 16 edges
5. `entry()` - 16 edges
6. `env` - 13 edges
7. `with` - 13 edges
8. `GitImpactTests` - 13 edges
9. `ImpactTests` - 11 edges
10. `catalog_entry()` - 10 edges

## Surprising Connections (you probably didn't know these)
- `{closure#1}()` --calls--> `nt_validate_catalog()`  [INFERRED]
  tests/plugin.php → source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php
- `{closure#2}()` --calls--> `nt_validate_catalog()`  [INFERRED]
  tests/plugin.php → source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php
- `{closure#3}()` --calls--> `nt_validate_catalog()`  [INFERRED]
  tests/plugin.php → source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php
- `{closure#4}()` --calls--> `nt_validate_catalog()`  [INFERRED]
  tests/plugin.php → source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php
- `{closure#5}()` --calls--> `nt_validate_catalog()`  [INFERRED]
  tests/plugin.php → source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php

## Import Cycles
- None detected.

## Communities (124 total, 13 thin omitted)

### Community 0 - "NerdTools"
Cohesion: 0.25
Nodes (4): NerdTools, nt_atomic_write(), {closure#25}(), {closure#26}()

### Community 1 - ".apply"
Cohesion: 0.13
Nodes (15): {closure#10}(), {closure#11}(), {closure#16}(), {closure#17}(), {closure#19}(), {closure#20}(), {closure#22}(), {closure#24}() (+7 more)

### Community 2 - "channel"
Cohesion: 0.06
Nodes (34): default, description, options, required, type, channel, publish, unraid_sha256 (+26 more)

### Community 3 - "publish"
Cohesion: 0.07
Nodes (32): cancel-in-progress, group, queue, DEFAULT_BRANCH, GH_TOKEN, TAG, jobs, publish (+24 more)

### Community 4 - "env"
Cohesion: 0.17
Nodes (12): BASELINE_ROOT, DEVELOPMENT_ROOT, GH_REPO, GH_TOKEN, PUBLISH, RELEASE_SHA, RELEASE_TAG, TARGET_CHANNEL (+4 more)

### Community 5 - "steps"
Cohesion: 0.06
Nodes (30): concurrency, cancel-in-progress, group, outputs, runs-on, steps, timeout-minutes, jobs (+22 more)

### Community 6 - "with"
Cohesion: 0.06
Nodes (36): needs, runs-on, steps, strategy, timeout-minutes, BASELINE_ROOT, DEVELOPMENT_ROOT, PACKAGE (+28 more)

### Community 7 - "check-unraid-releases.py"
Cohesion: 0.06
Nodes (22): attach_checksums(), checked_digest(), fetch_feed(), fetch_json(), main(), make_report(), parse_download_url(), parse_releases() (+14 more)

### Community 8 - "build-packages.yml"
Cohesion: 0.33
Nodes (5): concurrency, cancel-in-progress, group, name, run-name

### Community 9 - "package-pycairo.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 10 - "ReleaseFeedTests"
Cohesion: 0.23
Nodes (3): catalog_entry(), entry(), ReleaseFeedTests

### Community 11 - "nt_validate_catalog"
Cohesion: 0.27
Nodes (9): nt_validate_catalog(), {closure#1}(), {closure#2}(), {closure#3}(), {closure#4}(), {closure#5}(), {closure#6}(), {closure#7}() (+1 more)

### Community 12 - "packageQuery"
Cohesion: 0.53
Nodes (8): Apply(), checkDepends(), visit(), packageError(), packageQuery(), requestError(), saveSettings(), syncPreferences()

### Community 14 - "NerdTools"
Cohesion: 0.33
Nodes (5): Checks and failures, NerdTools, Package settings, Retained releases and rollback, Upgrades and removal

### Community 26 - "GitImpactTests"
Cohesion: 0.15
Nodes (4): GitImpactTests, ImpactTests, package(), WorkflowGenerationTests

### Community 27 - "steps"
Cohesion: 0.11
Nodes (17): jobs, validate, name, permissions, contents, branches, name, run (+9 more)

### Community 28 - "NerdToolsHelpers.php"
Cohesion: 0.14
Nodes (11): nt_log(), nt_package_name(), nt_payload_allowed(), nt_preference(), nt_record(), nt_resolve(), nt_tool_name(), nt_verified() (+3 more)

### Community 29 - "package-alsa-lib.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 30 - "package-aria2.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 31 - "package-audit.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 32 - "package-autossh.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 33 - "package-axel.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 34 - "package-bc.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 35 - "package-bluez.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 36 - "package-borgbackup.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 37 - "package-btop.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 38 - "package-bwm-ng.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 39 - "package-cksfv.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 40 - "package-ddrescue.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 41 - "package-exiftool.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 42 - "package-fdupes.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 43 - "package-fio.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 44 - "package-fish.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 45 - "package-fzf.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 46 - "package-gnupg2.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 47 - "package-gpgme.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 48 - "package-iftop.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 49 - "package-iotop.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 50 - "package-ipmitool.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 51 - "package-irssi.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 52 - "package-jdupes.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 53 - "package-lftp.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 54 - "package-libassuan.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 55 - "package-libical.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 56 - "package-libjodycode.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 57 - "package-libksba.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 58 - "package-libmediainfo.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 59 - "package-libnl.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 60 - "package-libsodium.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 61 - "package-liburing.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 62 - "package-libyaml.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 63 - "package-libzen.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 64 - "package-llfuse.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 65 - "package-lua.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 66 - "package-mediainfo.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 67 - "package-mktorrent.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 68 - "package-mlocate.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 69 - "package-msgpack-python.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 70 - "package-ncdu.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 71 - "package-neofetch.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 72 - "package-netcat.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 73 - "package-nload.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 74 - "package-nmap.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 75 - "package-nodejs.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 76 - "package-npth.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 77 - "package-p7zip.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 78 - "package-par2cmdline.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 79 - "package-pigz.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 80 - "package-pinentry.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 81 - "package-plocate.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 82 - "package-powershell.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 83 - "package-powertop.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 84 - "package-python2.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 85 - "package-python3.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 86 - "package-python-packaging.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 87 - "package-python-pip.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 88 - "package-python-setuptools.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 89 - "package-rar2fs.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 90 - "package-rclone.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 91 - "package-restic.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 92 - "package-rsnapshot.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 93 - "package-ruby.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 94 - "package-screen.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 95 - "package-socat.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 96 - "package-sshfs.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 97 - "package-sshpass.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 98 - "package-stress.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 99 - "package-stunnel.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 100 - "package-tcl.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 101 - "package-tcllib.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 102 - "package-tcpdump.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 103 - "package-tmux.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 104 - "package-unrar.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 105 - "package-utf8proc.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 106 - "package-vim.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 107 - "package-wget2.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 108 - "package-zsh.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 109 - "with"
Cohesion: 0.15
Nodes (13): with, bzroot-sha256, if-no-files-found, merge-multiple, name, path, pattern, persist-credentials (+5 more)

### Community 110 - "with"
Cohesion: 0.05
Nodes (48): if, needs, runs-on, steps, timeout-minutes, BASELINE_ROOT, DEVELOPMENT_ROOT, PACKAGE (+40 more)

### Community 111 - "package-pygobject3.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 112 - "plugin_aliases.php"
Cohesion: 0.18
Nodes (6): {closure#10}(), {closure#6}(), {closure#8}(), {closure#9}(), refuse_alias(), verify_alias()

### Community 113 - "package"
Cohesion: 0.18
Nodes (11): package, package, needs, strategy, uses, with, fail-fast, matrix (+3 more)

### Community 114 - "package-zenmap-typelibs.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 115 - "steps"
Cohesion: 0.42
Nodes (9): steps, steps, steps, id, if, name, run, uses (+1 more)

### Community 117 - "jobs"
Cohesion: 0.25
Nodes (8): if, needs, runs-on, timeout-minutes, jobs, build, validate, runs-on

### Community 118 - "package-libtraceevent.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 121 - "package-list"
Cohesion: 0.33
Nodes (6): package-list, names, if, needs, outputs, runs-on

### Community 122 - "publish"
Cohesion: 0.25
Nodes (8): publish, permissions, actions, contents, if, needs, permissions, runs-on

### Community 127 - "package-libtracefs.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 128 - "package-tpm2-tss.yml"
Cohesion: 0.14
Nodes (13): uses, with, jobs, build, name, permissions, contents, branches (+5 more)

### Community 129 - ".refresh"
Cohesion: 0.25
Nodes (5): nt_asset_url(), nt_next_check(), nt_supported(), {closure#13}(), {closure#14}()

## Knowledge Gaps
- **13 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `jobs` connect `jobs` to `build-packages.yml`, `package`, `publish`, `package-list`?**
  _High betweenness centrality (0.003) - this node is a cross-community bridge._
- **Are the 8 inferred relationships involving `nt_validate_catalog()` (e.g. with `{closure#1}()` and `{closure#2}()`) actually correct?**
  _`nt_validate_catalog()` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Should `.apply` be split into smaller, more focused modules?**
  _Cohesion score 0.13043478260869565 - nodes in this community are weakly interconnected._
- **Why does `True` connect `channel` to `build-packages.yml`?**
  _High betweenness centrality (0.002) - this node is a cross-community bridge._
- **Should `channel` be split into smaller, more focused modules?**
  _Cohesion score 0.058823529411764705 - nodes in this community are weakly interconnected._
- **Should `publish` be split into smaller, more focused modules?**
  _Cohesion score 0.07007575757575757 - nodes in this community are weakly interconnected._
- **Should `steps` be split into smaller, more focused modules?**
  _Cohesion score 0.06451612903225806 - nodes in this community are weakly interconnected._