# Dev Panel Overview

This folder contains developer-only tools and config for local testing.

## Big Switch

- `dev_mode_enabled = false`:
  - Dev panels are not registered.
  - Dev panel overrides are ignored.
  - Behavior matches "dev folder not present".
- `dev_mode_enabled = true`:
  - Dev panels are registered under `W3 Dev`.
  - Dev panel overrides are active.

## Config File

Main file: `dev/dev_config.json` (template: `dev/dev_config.example.json`).

### `addon_prefs_defaults`

- Seeds Add-on Preferences only when those preference fields are currently empty.
- Intended for first-run/local setup convenience.
- Does not continuously override user-edited preference values.

### `redkit_projects`

- Seed list for Add-on Preferences `redkit_projects`.
- Applied only when the preference list is still empty.

### `dev_panel_overrides`

- Live read-at-runtime overrides used by code paths that explicitly request them.
- `fallback_*` keys are fallback values, used when add-on preferences are unavailable in that code path.
- Non-`fallback_*` keys are direct dev/test hooks and can alter runtime behavior while Blender is running.
- Legacy key `runtime_overrides` is still accepted for backward compatibility.

### `test_paths`

- Backing data for the Dev Panel path lists.
- Organized per operator type (`w2mesh`, `w2ent`, `w2anims`, etc.) with sections and selectable entries.

## Dev UI

- `W3 Dev > Dev Panel`: test-path management + quick import actions.
- `W3 Dev > Logging Control`: per-module logging controls and counters.

## Visibility

- Dev panel behavior and override info are kept in the dev UI only.
- Add-on Preferences are kept user-facing and do not expose dev override state.
