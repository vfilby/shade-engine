# Shade Engine

Sun-tracking shade control for Home Assistant with **externalized policy**.

Shade Engine does two things and refuses to do a third:

1. **Calculates** the highest shade position that keeps direct sun off your
   room, from window geometry (pure trigonometry, published as sensors).
2. **Actuates** covers toward a target, with a movement deadband, rate
   limiting that defers rather than drops, and a visible hold whenever a
   human moves a cover.

It deliberately does **not decide** anything. All policy — when to track the
sun, when to stay open for the view, when heat matters, schedules, seasons —
lives in your own automations, which set a single `select` entity per zone.
If you can read an automation trace, you can understand this system.

## Why

Integrations that internalize the decision tree (climate strategies, weather
vetoes, presence branches, hidden toggles) become impossible to reason about:
configuration ends up split across a config flow, runtime switch entities,
and behavior you only discover in the source. Shade Engine compartmentalizes
instead:

| Layer | Where it lives | What it is |
|---|---|---|
| Calculate | this integration | pure geometry → `sensor.<zone>_glare_position` |
| Decide | **your automations** | write `select.<zone>_shade_mode`, nothing else |
| Actuate | this integration | mode → target → `cover.set_cover_position` |

## Install

1. HACS → Integrations → ⋮ → *Custom repositories* → add this repo as
   category **Integration**, then install.
2. Add configuration to `configuration.yaml` (below) and restart.

## Configuration

```yaml
shade_engine:
  zones:
    kitchen:
      name: Kitchen
      covers:
        - cover.kitchen_shades
      window:
        azimuth: 268          # compass direction the window faces
        fov_left: 90          # degrees of sky left of the normal
        fov_right: 90         # degrees right
        height: 0.74          # meters of glass the shade covers
        protect_depth: 1.7    # meters into the room to keep sun off
        min_elevation: 0      # ignore sun below this elevation
      modes:
        night: 37             # constant position
        open: 100
        track: glare          # follow the calculator
        shield:               # calculator, clamped: never above 50
          max: 50
      default_mode: open
      motion:
        deadband: 3           # % change worth moving for
        min_interval: 300     # seconds between commands (deferred, not dropped)
        hold_duration: 3600   # seconds to stand down after a manual move
        settle: 90            # seconds to ignore reports after our own command
```

A **zone** is a set of covers that share geometry and move together. Mode
names are yours; each maps to either a constant position (0–100), the string
`glare` (pure calculator passthrough), or a mapping with `min`/`max` clamps
applied to the calculator value.

## Entities (per zone)

| Entity | Meaning |
|---|---|
| `select.<zone>_shade_mode` | current mode — **the only thing policy writes** |
| `sensor.<zone>_glare_position` | calculator output; attrs: `gamma`, `profile_angle`, `sun_in_window` |
| `sensor.<zone>_shade_target` | what the actuator wants; attrs: `mode`, `last_decision` (`command` / `in_sync` / `rate_limited` / `hold_active`), `hold_until`, `last_command` |
| `binary_sensor.<zone>_sun_in_window` | direct sun geometrically possible now |
| `binary_sensor.<zone>_shade_hold` | a human moved a cover; engine is standing down |

## Services

| Service | Purpose |
|---|---|
| `shade_engine.hold` | start/refresh a hold (`zone`, optional `duration` seconds) |
| `shade_engine.release` | clear a hold and reconcile — use in automations that must win over a manual move (e.g. privacy close at dusk) |
| `shade_engine.reconcile` | evaluate immediately, bypassing rate limit (`zone` optional) |

## Behavior guarantees

- **Deferred, never dropped.** A move suppressed by the rate limit or
  deadband is retried on the next evaluation (every 60 s, and on every sun
  or mode change). The system always converges to the current target.
- **Humans win.** A cover position that doesn't match the last command
  (outside the settle window) starts a per-zone hold. The hold is a visible
  `binary_sensor` with a `hold_until` timestamp; `shade_engine.release`
  clears it. A `forced` evaluation (mode change, reconcile service) bypasses
  rate limiting but **never** bypasses a hold.
- **Every non-move is explained.** `sensor.<zone>_shade_target` always says
  why the engine last declined to act.

## Example policy: a daylight state machine

```yaml
# Bright and sunny: follow the sun.
- alias: "Shades: track when bright"
  triggers:
    - trigger: numeric_state
      entity_id: sensor.outdoor_illuminance
      above: 10000
      for: "00:05:00"
  actions:
    - action: select.select_option
      target: { entity_id: select.kitchen_shade_mode }
      data: { option: track }

# Dim: open up for the view.
- alias: "Shades: open when dim"
  triggers:
    - trigger: numeric_state
      entity_id: sensor.outdoor_illuminance
      below: 10000
      for: "00:05:00"
  actions:
    - action: select.select_option
      target: { entity_id: select.kitchen_shade_mode }
      data: { option: open }

# Hot room: shield even when glare geometry alone would open up.
- alias: "Shades: shield when hot"
  triggers:
    - trigger: numeric_state
      entity_id: sensor.kitchen_temperature
      above: 25
  conditions:
    - condition: numeric_state
      entity_id: sensor.outdoor_illuminance
      above: 10000
  actions:
    - action: select.select_option
      target: { entity_id: select.kitchen_shade_mode }
      data: { option: shield }

# Dark: privacy positions. release first so night always wins.
- alias: "Shades: night close"
  triggers:
    - trigger: numeric_state
      entity_id: sensor.outdoor_illuminance
      below: 50
      for: "00:05:00"
  actions:
    - action: shade_engine.release
      data: { zone: kitchen }
    - action: select.select_option
      target: { entity_id: select.kitchen_shade_mode }
      data: { option: night }
```

## Verifying your geometry

Run in shadow mode first: configure zones, restart, and graph
`sensor.<zone>_glare_position` against the sun for a couple of sunny days
**before** pointing `covers` at anything real (or set every mode to a
constant while you watch). Tune `protect_depth` until the curve drops when
glare actually reaches the spot you care about.

## Development

The calculator and decision core are pure Python with no Home Assistant
imports:

```sh
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -v
```

## Roadmap

- `shade_engine.reload` (YAML reload without restart)
- Optional tilt support for venetian-style slats
- Hold persistence across restarts
