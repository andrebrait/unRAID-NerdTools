# Explicit Package Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every NerdTools package action explicit, preview the exact transaction in a confirmation modal, and prevent unrelated upgrades.

**Architecture:** Add one dependency-aware planner in `NerdToolsHelpers.php`; both row state and execution consume its result. The GUI sends explicit row intents, receives a complete server plan, and confirms its content hash. Execution recomputes the plan against current catalog and installed state, rejects stale confirmations, then runs only confirmed operations.

**Tech Stack:** PHP 8, jQuery, native HTML `<dialog>`, Unraid webGUI controls, Slackware pkgtools, existing assertion-based PHP tests.

**Spec:** `docs/superpowers/specs/2026-11-15-explicit-package-actions-design.md`

## Global Constraints

- Do not add dependencies or a second package-management convention.
- Ordinary upgrades and downgrades require an explicit row intent.
- Only a newly installed package may trigger automatic upgrades of installed dependencies and their installed dependents.
- Keep ownership checks, full preflight, verification, dependency ordering, rollback releases, restore behavior, and external-package protection intact.
- Every visible state must use text; color may only reinforce it.
- Confirmed execution must reject stale catalog, ownership, installed-record, preference, or settings state before invoking pkgtools.

## Review Focus

- An outdated selected package with `keep` must remain installed at its current version when another package changes.
- Installing a package with an outdated installed dependency must include that dependency and affected installed dependents, but no unrelated update.
- An explicit removal that would break a selected dependent must be rejected before settings or packages change.
- A catalog refresh or installed-record change between preview and confirmation/execution must invalidate the plan.
- Rapid selector changes must never render or confirm an older asynchronous plan response.

---

### Task 1: Dependency-aware transaction planner

**Files:**
- Modify: `source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php`
- Modify: `tests/plugin.php`

**Interfaces:**
- Produces: `nt_package_version(string $record, string $name): ?string`.
- Produces: `nt_plan(array $packages, array $owned, array $installed, array $config, array $actions, int $hours): array`.
- Plan shape: `id`, `rows`, `changes`, `preferences`, `hours`, and state fingerprint inputs. Each row contains `intent`, `change`, `from`, `to`, `automatic`, and `reason`; each actionable change contains the same package transition fields.
- Valid intents: `keep`, `install`, `upgrade`, `downgrade`, `remove`.

- [ ] **Step 1: Add failing planner contract tests**

Add assertions covering version extraction, no-op outdated packages, one explicit upgrade without unrelated upgrades, install-triggered stale dependency and dependent upgrades, missing dependency installs, explicit removal ordering, settings-only changes, invalid intents, and removal blocked by a selected dependent.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `php tests/plugin.php`

Expected: failure because `nt_plan()` and `nt_package_version()` do not exist.

- [ ] **Step 3: Implement the pure planner**

Use existing `nt_resolve()` ordering. Derive reverse dependency edges only for the automatic installed-dependent cascade. Keep outdated packages at their installed records unless explicitly targeted or reached by the permitted new-install cascade. Return rows for unchanged packages as well as ordered actionable changes.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `php tests/plugin.php`

Expected: `Plugin contracts passed`.

- [ ] **Step 5: Commit the planner slice**

```bash
git add source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php tests/plugin.php
git commit -m "feat: plan explicit package transactions"
```

### Task 2: Bind confirmation to exact execution

**Files:**
- Modify: `source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php`
- Modify: `source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/Settings.php`
- Modify: `source/NerdTools/usr/local/emhttp/plugins/NerdTools/scripts/packagemanager`
- Modify: `tests/plugin_lifecycle.php`

**Interfaces:**
- Consumes: `nt_plan(...)` from Task 1.
- Produces: `NerdTools::plan(array $actions, $hours): array`.
- Produces: `NerdTools::confirmPlan(string $id, array $actions, $hours): string`, which recomputes the plan and writes `pending-plan.json` only after `hash_equals` succeeds.
- Produces: `NerdTools::applyPlan(string $id, bool $download): void`, which consumes one pending plan, revalidates it, saves its preferences/interval, and executes only its ordered changes.
- HTTP contract: `action=plan` returns the complete plan; `action=confirm` returns `{ok:true, plan:<64-lowercase-hex-id>}`.
- CLI contract: `--plan=<id>` executes the confirmed plan. Existing direct CLI and remove-all behavior remain available.

- [ ] **Step 1: Add failing lifecycle tests**

Add a catalog with an outdated selected tool, a new package, one outdated dependency, an affected installed dependent, and one unrelated update. Assert preview contents, no unrelated upgrade, one-use confirmation, stale-plan refusal before pkgtools, and exact confirmed operations.

- [ ] **Step 2: Run the focused lifecycle test and verify RED**

Run: `php tests/plugin_lifecycle.php`

Expected: failure because `plan()`, `confirmPlan()`, and `applyPlan()` do not exist.

- [ ] **Step 3: Implement plan confirmation and constrained execution**

Keep one ownership-checked `pending-plan.json` under the existing state directory. Validate plan IDs with `/^[a-f0-9]{64}$/D`. Recompute before confirmation and execution. Consume the pending file before package mutation so a confirmed transaction cannot replay. Refactor the existing apply loop only enough to accept the planner's install/replace/remove sets; retain all current download, archive, collision, migration, and removal checks.

- [ ] **Step 4: Add endpoint and CLI routing**

Replace GUI `action=save` with `plan` and `confirm`. Keep CSRF and lock behavior. Add `--plan` parsing and pass the confirmed ID into `applyPlan()`; do not put package actions in the command URL.

- [ ] **Step 5: Run lifecycle and existing safety tests**

Run:

```bash
php tests/plugin_lifecycle.php
php tests/plugin_aliases.php
php tests/plugin_policy.php
python3 tests/plugin_udev.py
```

Expected: each command exits 0 and prints its existing success line.

- [ ] **Step 6: Commit the execution slice**

```bash
git add source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/Settings.php source/NerdTools/usr/local/emhttp/plugins/NerdTools/scripts/packagemanager tests/plugin_lifecycle.php
git commit -m "feat: execute only confirmed package plans"
```

### Task 3: Explicit row actions and confirmation modal

**Files:**
- Modify: `source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/PackageQuery.php`
- Modify: `source/NerdTools/usr/local/emhttp/plugins/NerdTools/NerdTools.page`
- Modify: `source/NerdTools/usr/local/emhttp/plugins/NerdTools/js/jquery.NerdTools.js`
- Modify: `source/NerdTools/usr/local/emhttp/plugins/NerdTools/css/NerdTools.css`

**Interfaces:**
- Consumes: Task 2 `plan` and `confirm` HTTP actions.
- Package query adds `installed_version`, `available_version`, `update_kind`, `default_action`, and valid `actions`; it retains existing ownership and external flags.
- UI sends `actions[package]=intent` plus `update_check_hours`.
- Modal displays every `changes[]` entry with action, package, `from → to`, automatic reason, and settings changes.

- [ ] **Step 1: Extend package-query display data**

Use `nt_package_version()` for owned/current records. Distinguish Upgrade from Downgrade by comparing installed and catalog versions. Emit only actions valid for the row; external packages expose Keep only.

- [ ] **Step 2: Replace switches with action selectors and explicit columns**

Use columns Action, Package, Dependencies, Installed version, Available version, Planned change, and Size. Keep rows updateable by tablesorter. Show `No change` and `Update available` explicitly. Rename `Force Updates` to `Check for updates` and set Apply text to `Apply N changes`.

- [ ] **Step 3: Request and render plans after each input change**

Abort the prior request or ignore responses whose revision is not current. Disable Apply while planning or when `changes` is empty. Render planner row state and required-action reasons; never infer automatic actions independently in JavaScript.

- [ ] **Step 4: Add the accessible confirmation dialog**

Add a native `<dialog>` labelled by its heading, with a transaction table, Cancel, and Confirm actions. Apply opens the latest complete plan. Confirm posts the same intents, hours, and plan ID; on success call `openBox()` with `--download`, `--uninstall`, and `--plan=<id>`. Escape and Cancel perform no writes.

- [ ] **Step 5: Run static checks**

Run:

```bash
php -l source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/PackageQuery.php
php -l source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/Settings.php
node --check source/NerdTools/usr/local/emhttp/plugins/NerdTools/js/jquery.NerdTools.js
```

Expected: no syntax errors.

- [ ] **Step 6: Verify the rendered surface**

Serve a throwaway fixture using representative package-query data. In a browser, verify unchanged, update-available, install, remove, required-upgrade, unavailable, and external rows; keyboard-open the modal; inspect the accessibility tree; confirm Cancel writes nothing; test 320px, 768px, 1024px, and 1440px widths; confirm no console errors. Delete the fixture afterward.

- [ ] **Step 7: Commit the UI slice**

```bash
git add source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/PackageQuery.php source/NerdTools/usr/local/emhttp/plugins/NerdTools/NerdTools.page source/NerdTools/usr/local/emhttp/plugins/NerdTools/js/jquery.NerdTools.js source/NerdTools/usr/local/emhttp/plugins/NerdTools/css/NerdTools.css
git commit -m "feat: show and confirm explicit package actions"
```

### Task 4: User documentation and final gates

**Files:**
- Modify: `docs/plugin.md`

**Interfaces:**
- Consumes: completed planner, execution, and UI contracts.
- Produces: user-facing package action, update, dependency exception, confirmation, and stale-plan behavior documentation.

- [ ] **Step 1: Update the plugin guide**

Replace On/Off and `Force Updates` wording. Document explicit actions, separate installed/available versions, required dependency cascades, confirmation contents, stale-plan rejection, and the absence of background upgrades.

- [ ] **Step 2: Run the repository plugin gate once**

Run:

```bash
php tests/plugin.php
php tests/plugin_lifecycle.php
php tests/plugin_aliases.php
php tests/plugin_policy.php
python3 tests/plugin_udev.py
node --check source/NerdTools/usr/local/emhttp/plugins/NerdTools/js/jquery.NerdTools.js
python3 -m unittest discover -s tests -p 'test_plugin_packaging.py'
```

Expected: all commands exit 0; no tests are skipped or disabled.

- [ ] **Step 3: Commit documentation**

```bash
git add docs/plugin.md
git commit -m "docs: explain explicit package transactions"
```

- [ ] **Step 4: Run the required single adversarial review**

Dispatch the repository `reviewer` for contract, correctness/hostile cases, test honesty, and over-engineering. Resolve every should-fix finding, then rerun only the checks affected by fixes.

- [ ] **Step 5: Publish**

Fetch and rebase onto current `main`, push the feature branch, delete its wip ref, open the PR, register pr-watcher, and follow repository CI and bot-review policy.
