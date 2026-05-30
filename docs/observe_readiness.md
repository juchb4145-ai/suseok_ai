# OBSERVE Readiness

OBSERVE readiness now depends on the dynamic theme engine, not a CSV mapping
file.

Key warning codes:

- `THEME_CONTEXT_NOT_READY`: canonical themes or current memberships have not
  been built yet.
- `NO_ACTIVE_THEME_FOR_ACTIVE_CANDIDATES`: active candidates do not currently
  resolve to ACTIVE/WATCH dynamic themes.
- `NO_ACTIVE_THEME`: a candidate has no active dynamic theme context.

Operational warm-up:

1. Run a DynamicThemeEngine source sync.
2. Build `theme_membership_current`.
3. Score current ticks into `theme_activity_snapshots`.
4. Start OBSERVE only after theme data status is `ready`.

Static CSV imports and CSV template generators are retired.
