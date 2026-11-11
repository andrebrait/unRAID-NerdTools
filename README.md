# NerdTools for Unraid

This fork maintains the NerdTools plugin originally developed by UnRAIDES. It installs native packages and does not require Docker at runtime.

The supported baseline is Unraid 7.3.4. Later releases, including public betas and release candidates, are tested automatically as they appear and receive packages once they pass. Package catalogs and binaries are published as GitHub Release assets. Historical releases are retained for rollback.

NerdTools checks for catalog updates daily by default. The interval is configurable, automatic checks can be disabled, and failed checks are retried hourly. Existing verified packages and the last successful catalog are retained if GitHub is unavailable. Checks do not install updates automatically.

RAR is proprietary trial software and is offered through its original upstream download, not repackaged in this repository. Review its license before installing it.

Plugin behavior, safety rules and rollback are described in the [plugin guide](docs/plugin.md). The build recipes, pinned inputs, and GitHub Actions workflows are maintained in this repository. See [build instructions](build/README.md). Builds use an isolated Unraid-derived environment; hardware-specific behavior requires validation on the target hardware.

