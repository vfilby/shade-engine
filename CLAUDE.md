# Shade Engine — project status & context

*Handoff summary, 2026-08-12. This file orients a fresh session; the README
is the user-facing doc.*

## What this is

A from-scratch HACS custom integration (`shade_engine` domain) that will
replace the **Adaptive Cover** (basbruss/adaptive-cover) integration running
Vincent's shades. Built 2026-08-10 after a week of Adaptive Cover failures;
full design rationale lives in the proposal artifact:
<https://claude.ai/code/artifact/147d8848-8864-4e58-89d6-493ce1973ec9>

**Why Adaptive Cover is being replaced:** it internalizes a large hidden
decision tree (winter/summer strategies, weather-forecast vetoes, presence
branches, transparent-blind special case), splits configuration across a
config flow *and* runtime toggle switches, silently drops commands
(`delta_time`), applies invisible 2 h manual overrides, and shipped two real
bugs (a `float(None)` boot crash when the lux sensor hasn't reported yet, and
switches that stay `unavailable` forever because `async_added_to_hass` never
calls `super()`).

## Design: three layers, policy externalized

1. **Calculator** (`calculator.py`, pure, no HA imports) — window geometry +
   sun position → `glare_position` (highest position that keeps direct sun
   off `protect_depth` meters of room) and `sun_in_window`.
2. **Policy** — *not in this integration.* User automations write one entity
   per zone: `select.<zone>_shade_mode`. Mode names are user-defined; each
   maps to a constant position, `glare` passthrough, or a min/max-clamped
   passthrough.
3. **Actuator** (`core.py` pure logic + `__init__.py` HA glue) — resolves
   mode → target, commands covers. Guarantees: suppressed moves are
   **deferred, never dropped** (60 s tick reconciles); a manual cover move
   (position ≠ last commanded, outside a settle window) starts a **visible
   hold** (`binary_sensor.<zone>_shade_hold`); `forced` evaluations (mode
   change, `reconcile` service) bypass rate limiting but **never** a hold;
   `sensor.<zone>_shade_target` always exposes why the engine last declined
   to move (`in_sync` / `rate_limited` / `hold_active`).

Services: `shade_engine.hold`, `.release` (for automations that must beat a
manual move, e.g. night close), `.reconcile`.

Config is **YAML-only** (`shade_engine: zones:` in configuration.yaml) — a
deliberate choice so geometry lives in git; no config flow.

## State of the repo

- Initial commit `b5fc523` on `main`, 2026-08-10, all work committed.
- 19 unit tests pass (`.venv/bin/python -m pytest tests/ -q`) covering the
  two pure modules: FOV wrap-around, elevation floor, monotonicity, deadband,
  rate-limit defer/converge, hold lifecycle, settle window, baseline
  adoption, unavailable-cover skip.
- CI (`.github/workflows/validate.yml`): hassfest, HACS action
  (`ignore: brands`), pytest. **Never run yet** — no remote exists.
- HACS metadata (`hacs.json`), MIT license, README with config reference and
  example policy automations.

## Not done yet (next steps, in order)

1. **GitHub repo**: user will create it; `manifest.json` links assume
   `github.com/vfilby/shade-engine`. Then `git remote add origin … && git
   push -u origin main` and confirm CI passes (hassfest on a YAML-only
   integration is the likeliest first failure).
2. **Real zone config**: known geometry — kitchen az 268, fov 90/90, glass
   height 0.74 m, protect_depth 1.7 m; living-room north az 358, fov 90/45,
   min_elevation 10. **Dining and living-west geometry still needs to be
   read** from the Adaptive Cover config entries on the server
   (`.storage/core.config_entries`, domain `adaptive_cover`).
3. **Shadow mode**: install via HACS custom repo on the home server,
   configure zones, graph `sensor.<zone>_glare_position` against Adaptive
   Cover's `…_cover_position_…` sensors for a few sunny days before pointing
   `covers:` at real entities.
4. **Port policy**: rewrite `automations/adaptive-cover-night.yaml` (on the
   server, untracked by git) to set modes instead of flipping Adaptive Cover
   switches, then remove Adaptive Cover, its 4 config entries, and the local
   patches.
5. Roadmap after that: `reload` service, tilt support, hold persistence.

## Live-system context (the thing being replaced)

- HA runs in Docker on the home server: `ssh vfilby@home`, stack at
  `"/home/vfilby/Docker Services"`, container `homeassistant`, config mount
  `/config`. No API token is available; state is read via sqlite on
  `/config/home-assistant_v2.db` through `docker exec` (see the
  HomeAssistantDockerStack project's memory for the JWT-mint reload trick).
- Current daily cycle (Adaptive Cover + lux state machine on
  `sensor.backyardmotion_illuminance`): first-light open ≥100 lx (06:00
  floor) → bright ≥850 lx hands control to Adaptive Cover (climate mode on)
  → dim <850 lx climate off, open for view → night close <10 lx (lowered
  from 50 on 2026-08-11) → −9° elevation privacy-close backstop
  (`scene.shades`: LR 25 / dining 38 / kitchen 37 — these become the `night`
  mode constants).
- Adaptive Cover on the server is running **locally patched** files
  (`calculation.py`, `switch.py`; originals at `*.bak-20260810`) fixing the
  two bugs above. Any HACS update of adaptive-cover reverts the patches —
  another reason to finish this migration.
- Glare thresholds learned from data: outdoor lux >10 000 ≈ direct-sun
  conditions (sunny days 25k–70k, overcast <18k). Never gate on the indoor
  kitchen lux sensor — closing the blinds darkens the room and oscillates.

## Conventions

- Commit as `Vincent Filby <v@filby.ca>`.
- Keep `calculator.py` and `core.py` free of Home Assistant imports — the
  test suite depends on it, and it's the escape hatch if this ever moves
  back to pyscript.
- Python venv for tests: `.venv/` (gitignored), `pip install pytest`.
