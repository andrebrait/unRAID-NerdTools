# Explicit Package Actions Design

## Intent

Make NerdTools behave like a modern package manager frontend. Every package row must show current state, available state, and the exact action that Apply would perform. Applying one requested change must never silently install unrelated updates.

## Goals

- Show installed and available versions in separate columns.
- Give every row an explicit planned state: No change, Install, Upgrade, Downgrade, Remove, required dependency action, unavailable, or externally managed.
- Keep update availability visible even when the package remains unchanged.
- Require users to explicitly stage ordinary upgrades and downgrades.
- Permit automatic upgrades only when installing software requires an already-installed dependency upgrade and therefore upgrades to its installed dependents.
- Before execution, show a modal containing every package and settings action, its source, and any version transition.
- Execute exactly the confirmed backend plan. Reject stale plans and require review again.

## Non-goals

- Background or unattended package upgrades.
- A second package details screen, bulk update-all action, update policy settings, or new dependencies.
- Managing packages owned by Unraid or another installer.

## Constraints

- Preserve NerdTools ownership, archive verification, collision checks, dependency ordering, rollback-release support, and removal safety.
- Use existing Unraid controls and visual language. Text must communicate state without relying on color.
- Automatic dependency actions must name the package that requires them.
- Apply remains disabled when neither package actions nor settings changes are staged.

## Approaches

1. Keep On/Off switches and add update buttons and status columns. Smallest diff, but two controls per row remain ambiguous.
2. Replace switches with one row action selector and an inline summary. Clear rows, but the summary is too easy to miss before a destructive transaction.
3. Use one row action selector plus a server-generated confirmation modal. Selected because it makes intent explicit and binds confirmation to the transaction the backend will execute.

## Design

Each package row contains Action, Package, Dependencies, Installed version, Available version, Planned change, and Size. The action selector offers only valid explicit actions for that row. Unchanged packages say No change. An available update remains visible while the action stays Keep. Changed rows receive a subtle highlight; badges and text identify action type.

Selecting Install automatically stages missing dependencies. If an already-installed dependency must move to the catalog version, the planner also stages that upgrade and upgrades installed dependents that must remain consistent. Each automatic row says `Required by <package>`. No other available update enters the plan.

`Apply N changes` asks the backend for an immutable transaction plan. The modal lists every Install, Upgrade, Downgrade, Remove, required dependency action, and settings change. Version actions display `installed → target`. Confirm submits the plan identity; Cancel changes nothing. Execution revalidates the plan against catalog, ownership, installed records, selections, and settings. Any mismatch rejects execution and asks the user to review a fresh plan.

Copy changes: `Force Updates` becomes `Check for updates`; generic status text becomes Planned change. External packages remain visibly protected.
