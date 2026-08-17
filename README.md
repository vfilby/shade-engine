# Shade Engine

> **Note:** This is heavily based on
> [Adaptive Covers](https://github.com/basbruss/adaptive-cover), and
> ultimately is a stripped down version that externalizes the state
> management so that I can control it with automations instead of
> influencing it with inputs. For most people you should probably try it
> first; it is more mature, has configuration built into the UI, and will
> certainly be better supported. If you need more control and you are ok
> with some manual config then you can give this one a try. On a side note,
> manual YAML config is quite a bit more friendly to agents so that is a
> side benefit.

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

### Eye zone & reflected glare

`protect_depth` protects a strip of floor. If your actual problem is *eyes* —
including sun that bounces off a shiny floor or countertop and up into them —
replace it with an `eye_zone` and optional `reflectors`:

```yaml
      window:
        azimuth: 268
        height: 0.74
        sill_height: 0.9      # meters from floor to the bottom of the glass
        eye_zone:
          height: [0.8, 1.4]  # meters above the floor to keep sun out of
          depth: [2.0, 4.0]   # meters from the window where eyes live
        reflectors:
          - height: 0.0       # the floor
          - height: 0.75      # a countertop...
            from: 0.0         # ...spanning this range of distance
            to: 0.6           #    from the window (omit "to" for unbounded)
```

All geometry is solved in the vertical plane along the sun's azimuth. Direct
glare is excluded when the steepest admitted ray passes below the eye zone
before reaching it. Each reflector adds one more constraint by mirror
symmetry: a bounce off a surface at height *r* into the zone is a straight
ray into the zone's reflection below that surface. The published position is
the highest one satisfying every constraint — which is naturally
**non-monotonic** over a day: high sun can force the shade down (floor bounce
climbs into eyes), mid-descent can open up (bounces fall short of the zone),
low sun closes again (direct rays at eye height).

Notes:

- `protect_depth` is exactly `eye_zone: {height: [0, x], depth: [d, inf]}` —
  existing configs behave identically. Provide one of the two.
- Reflectors assume worst-case specular (mirror) bounce and full window
  width. That over-shades rather than under-shades; if a reflector closes
  the shade at hours nobody experiences glare, narrow its `from`/`to` span
  or remove it.
- `reflectors` require an `eye_zone`, and each reflector must sit below the
  zone's lower height.

## Entities (per zone)

Each zone appears as a **device** under Settings → Devices & Services →
Shade Engine, grouping all of its entities in one place. (The config entry
behind that page is created automatically from the YAML — configuration is
still YAML-only, and the UI "add integration" flow is intentionally
disabled.)

| Entity | Meaning |
|---|---|
| `select.<zone>_shade_mode` | current mode — **the only thing policy writes** |
| `sensor.<zone>_glare_position` | calculator output; attrs: `gamma`, `profile_angle`, `sun_in_window`, `constraint` (`direct` / `reflected` / `none` — what bound the position) |
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
glare actually reaches the spot you care about. With an `eye_zone`, watch the
`constraint` attribute too: `reflected` at hours when nothing actually
bounces into your eyes means a reflector span is too generous.

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
