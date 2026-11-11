# NerdTools

Native command-line tools for Unraid 7.3.4 and later, including betas and release candidates once packages have been verified and published for them. NerdTools is community software, not supported by Lime Technology. No Docker runtime is used. Unraid versions without a verified catalog are refused instead of guessing ABI compatibility.

Install the rendered `NerdTools.plg` from this fork's GitHub Releases. The checked-in `plugin/NerdTools.plg` is a release template, not an installable published manifest. Packaging supplies the archive version and its actual SHA256.

## Package settings

Open **Utilities → NerdTools**. Search, sort and filter the package table, turn packages on or off, and select **Apply**. Apply saves your selections and the check interval, then installs and removes packages to match. Dependencies are selected automatically and enforced again by the package manager. System packages or packages managed by another installer are protected, not adopted.

Native package identities are `nerdtools-<tool>` so pkgtools cannot replace an Unraid package with the same tool name. The GUI continues to show original tool names. Existing selections are preserved when their settings keys migrate; installed ownership changes only after successful explicit verified Apply. Archives install qualified `nerdtools-<command>` launchers. NerdTools also creates an owned bare-command symlink when no host command exists in the service PATH or standard command directories; host commands are never shadowed. Only unchanged owned symlinks are removed, and foreign replacements are retained. Use the qualified command when a host command conflicts.

Catalogs come from published GitHub Releases containing the catalog of the running Unraid family, for example `catalog-7.4.json` on Unraid 7.4.x. Each catalog declares its minimum Unraid version, package version, exact same-release asset name, SHA256, size and dependencies. Downloads are stored under `/boot/config/plugins/NerdTools/packages`; every installation verifies SHA256 and size. The complete dependency download set is verified before installation starts. Archive metadata, link targets, cross-package file/ancestor collisions and existing non-owned files are checked before installation. Special devices and ambiguous filenames are rejected. The only permitted install hook is the builder's exact no-clobber `.new` configuration handler; other package shell hooks are refused. Installation and removal use native `upgradepkg`/`removepkg`, not a container.

Payloads are restricted to the private prefix, qualified launchers, and the exact audited BlueZ/audit/Screen system integration files. Ownership must be root:root; only the plocate/mlocate private binary and database directory may use group 21 with their required modes. Setuid, other setgid, sticky/world-writable payloads, arbitrary host destinations, and downloadable native RAR are rejected before pkgtools runs. New integration paths require an explicit policy update and native build inventory review.

The BlueZ HID-switching udev rule additionally requires an independent, plugin-shipped digest of the pinned BlueZ 5.88 rule after the audited helper-path rewrite. A catalog checksum cannot authorize different root `RUN` content. Future rule updates require reviewing pinned source bytes and updating this digest deliberately; helper binaries still use the normal release/catalog integrity model.

Screen may install only its three no-clobber configuration files: `/etc/screenrc.new`, `/etc/skel/.screenrc.new`, and `/etc/pam.d/screen.new`. The PAM content must exactly match the independently pinned Slackware policy requiring `pam_securetty` and the host's `system-auth`; arbitrary authentication changes and direct replacement of an existing host policy are refused.

RAR is not redistributed or installed by NerdTools. When offered by the catalog, the GUI shows the proprietary trial notice, original RARLAB download and license terms. Users download and install that original distribution themselves; a license is required after the 40-day trial.

Capabilities listed under **Built into Unraid** come from the catalog's `provided_packages` baseline information. They have no downloadable asset or NerdTools installation/removal controls.

## Checks and failures

Automatic checks default to daily. Choose Disabled, hourly, 6-hourly, 12-hourly, daily or weekly in the GUI. Checks refresh the catalog only; they do not automatically install package updates. **Force Updates** checks immediately unless GitHub's rate-limit reset is still active. Failed checks retry hourly, or after the server's later reset time. Invalid responses never overwrite the last-good catalog. An hourly native cron entry evaluates the configured interval; disabling checks causes no background HTTP requests.

Checks use GitHub's API anonymously, about one request per check, within GitHub's limit of 60 requests per hour per public IP address. If checks keep failing with a rate-limit error, for example because many devices share one connection, enter an optional GitHub token in the settings page. Use a fine-grained personal access token with read-only access to public repositories and no other permissions. NerdTools sends it only to `api.github.com`, never to download hosts, and never shows it in the page again. It is stored in `/boot/config/plugins/NerdTools/github-token` on the USB flash drive, which anyone with access to the flash share can read; **Remove Token** deletes it.

Unraid's plugin boot hook restores only selected, previously owned versions whose recorded SHA256 and size match the existing cache. It never downloads packages or applies newer catalog versions automatically, even when online. First-time and legacy selections need an explicit Apply for a verified installation. Catalog checking happens separately after restoration. Native package installation is not a filesystem transaction: a host failure during `upgradepkg` still requires administrator recovery. Review the package-manager output before assuming a change succeeded.

## Retained releases and rollback

The **Package release** selector lists retained published package releases. Select a tag, choose **Use Release**, then **Apply** to restore its package versions. An incompatible or invalid catalog is rejected without changing the current cache. A specific release stays pinned across reboot and scheduled checks. Choose **Latest compatible release** to resume following new releases. Packages unavailable in the chosen release retain their preferences; an owned selected package absent from that catalog blocks removal so its dependencies are not silently lost.

The equivalent CLI is:

```
/usr/local/emhttp/plugins/NerdTools/scripts/packagemanager --release=packages-<retained-tag>
/usr/local/emhttp/plugins/NerdTools/scripts/packagemanager --download --uninstall
```

Use `--release=latest` to unpin, `--check --force` to refresh the current selection, and `--help` for all commands. These commands are intended only on the supported Unraid host.

## Upgrades and removal

Legacy replacement is refused if any other installed record shares the old package basename, because native `upgradepkg old%new` removes all such records. Positive plugin-dependency checks also apply to migration/replacement, not only uninstall; resolve these conflicts explicitly before retrying.

`NerdTools.cfg` is retained across plugin upgrades, reboot, uninstall and reinstall. Legacy version-keyed selections migrate when their package settings are saved. An exact installed `_nerdtools` record, matching archive and exact version-keyed preference (or existing ownership record) are required for adoption. Legacy boot archives are not moved or deleted before an explicit successful verified replacement; failed migration preserves the original next-boot behavior. Only exact owned old archives are deleted after successful replacement or explicit removal. Unrelated `/boot/extra` files are never moved or deleted. Unavailable owned packages remain visible so users can switch them off; unavailable preferences and unrelated configuration keys are retained.

Ownership is recorded in `owned-packages.json`. Removing the plugin removes only owned packages and its persistent `NerdTools.cron` entry, while preserving settings and verified cache. The cron file is ownership-checked and activated with Unraid's `update_cron` on boot, settings saves and removal. Dependency removal is ordered. Removal is refused when another plugin's manifest or text runtime files name the package, its `nerdtools-<command>` launcher or its `/opt/nerdtools` path. A bare command word such as `bc` is not treated as a reference, so scripts that call a tool by its short name are not detected. Verify other consumers before removing tools. Installation, checking and settings writes share a lock so simultaneous GUI/cron operations do not race.
