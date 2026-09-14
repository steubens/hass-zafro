# Zafro Bridge — project guide for Claude Code

Home Assistant **add-on** that gives local, cloud-independent control of Zafro /
Rowan (i4season-platform) smart appliances. It impersonates the vendor cloud on
the LAN, relays to the real cloud so the official app keeps working, and
exposes everything to Home Assistant via MQTT device discovery.

This file is public. Keep personal or site-specific details (hosts, IPs,
serial numbers, credentials, captures) out of it; put those in the gitignored
`CLAUDE.local.md` instead.

## Layout

```
zafro_bridge/                      the add-on (what Home Assistant installs)
  config.yaml                      add-on manifest: options, schema, ports, services
  Dockerfile                       base-python image + pinned deps + rootfs
  apparmor.txt                     AppArmor profile (keep it parse-safe: base abstraction only)
  DOCS.md / README.md / CHANGELOG.md
  translations/en.yaml             option names/descriptions for the UI
  rootfs/etc/services.d/zafro-bridge/{run,finish}   s6 service (bashio reads options + MQTT service)
  rootfs/usr/src/zafro_bridge/     the Python package
    protocol.py      constants, message builders, HA<->device translation  (pure)
    mqtt_codec.py    MQTT 3.1 packet encode/decode                          (pure)
    state.py         per-device merged state                                (pure)
    ha_discovery.py  device-based discovery payload builder                 (pure)
    config.py        settings from environment                              (pure)
    device_server.py aiohttp TLS endpoint: REST stubs/proxy + WebSocket
    device_session.py one appliance session: relay vs local broker, injection
    cloud_relay.py   upstream WebSocket + REST proxy to the vendor cloud
    ha_mqtt.py       aiomqtt client: discovery, state, commands
    bridge.py        orchestrator
    __main__.py      entry point
PROTOCOL.md          the reverse-engineered wire protocol (authoritative reference)
CAPTURE.md           how to capture the protocol again (device-side interception)
CONTRIBUTING.md / SECURITY.md      contributor workflow, private vulnerability reporting
pyproject.toml       pytest pythonpath + ruff config (no build backend)
.github/             CI (tests, py_compile, ruff, add-on linter, Docker build), Dependabot, issue templates
tests/
  test_protocol/mqtt_codec/state/ha_discovery.py   pure-module unit tests (need only pytest)
  test_addon_config.py       version and option defaults stay in sync across files
  test_bridge.py             bridge ordering with in-memory fakes          (needs aiohttp, aiomqtt)
  test_integration_device.py real DeviceServer + fake appliance over TLS   (needs aiohttp)
  test_integration_bridge.py real Bridge + mosquitto end to end            (needs aiohttp, aiomqtt, mosquitto)
  fake_appliance.py          appliance simulator; also a CLI for manual testing against an add-on
```

The "pure" modules have no I/O so they can be unit-tested and reasoned about in
isolation. Keep it that way: protocol/mapping logic goes in `protocol.py`,
never in the network layers.

## Working on it

- Run tests: `python -m pytest -q tests` (needs only `pytest`; tests needing the
  runtime deps skip themselves). Full suite, as CI runs it:
  `uv run --no-project --python 3.13 --with pytest --with aiohttp --with aiomqtt -- python -m pytest -q tests`
  (with `mosquitto` on PATH). Byte-compile: `python -m py_compile zafro_bridge/rootfs/usr/src/zafro_bridge/*.py`.
  Lint: `uv run --no-project --with ruff -- ruff check .`
- Testing against a real Home Assistant uses the **local add-on** flow: copy
  `zafro_bridge/` to the HA host's `/addons/zafro_bridge`, then
  `ha apps reload`, `ha apps install local_zafro_bridge`, later
  `ha apps rebuild local_zafro_bridge` + `ha apps restart ...`, and read
  `ha apps logs local_zafro_bridge`. Set the `log_level` option to `debug` to
  see every MQTT packet type. The appliance must be redirected to the add-on
  with a router NAT rule (see `zafro_bridge/DOCS.md`).
- Protocol changes: update `PROTOCOL.md`, `protocol.py`, and the tests together.
- Version bumps touch three places: `zafro_bridge/config.yaml`,
  `rootfs/usr/src/zafro_bridge/version.py`, and `CHANGELOG.md`.

## Hard-won gotchas (do not regress these)

- **WebSocket compression must stay disabled** on both the device-facing
  server (`WebSocketResponse(compress=False)`) and the upstream relay
  (`ws_connect(compress=0)`). The appliance offers `permessage-deflate`; with
  it enabled, aiohttp's compressed writer crashed on the 30 s keepalive and the
  shared compression context corrupted the handshake (the device would not
  even SUBSCRIBE). This caused a reconnect-every-30 s loop.
- **Device-based MQTT discovery** (`homeassistant/device/<id>/config`) rejects
  the `~` base-topic shorthand at the payload root, and rejects `null` for
  device fields like `sw_version`/`hw_version`. Use absolute topics and omit
  unknown device fields.
- The appliance acts on any PUBLISH we send it on its command topic and
  PUBACKs it, regardless of its own subscriptions; we are its broker.
- In **relay** mode the vendor cloud answers CONNACK/SUBACK/PINGRESP and we
  forward byte-for-byte; in **local** mode we synthesize them. Injected
  PUBLISHes use packet ids ≥ 60000 so their PUBACKs are recognized and never
  leaked upstream.
- In relay mode, frames forwarded upstream that still await a cloud answer
  sit in a **pending-ack ledger**; every fallback to local mode answers them
  locally. Detach `_upstream` and clear the ledger *before* the first await in
  a fallback, or concurrent traffic double-answers or crashes it.
- **Keepalive counts binary (MQTT) frames only**, and the pre-CONNECT deadline
  runs from the WebSocket upgrade, so a peer cannot hold a session slot open
  with junk frames.
- **Never publish state before `DeviceState.has_core_state`** and never
  publish discovery before the cmd 5 info reply (15 s fallback): a fresh
  process otherwise shows a false "off" and a placeholder device name.
  Discovery republishes only when the device-card fields change.
- Discovery lists two availability topics with `availability_mode: all`: the
  device's and `zafro/_bridge/availability` (the MQTT last will). Serials are
  validated as topic-safe and may not start with `_`, so they can't collide.
- The AppArmor profile may only `#include <abstractions/base>`; other
  abstractions are absent on Home Assistant OS and make the profile fail to
  load (which blocks installation). Keep the top-level `file,` rule from Home
  Assistant's template; removing it untested risks a profile that blocks s6.

## Conventions

- Descriptive names; docstrings and comments explain *intent*.
- Nothing site-specific in code or docs: defaults are the vendor's public
  values, everything else is configuration.
- Never commit captures, device credentials, serial numbers, or account ids.
  Test vectors use same-length placeholders.

## Release checklist

1. Full test suite green (see above); `py_compile` and `ruff check .` clean.
2. Scrub: `grep -rnIE "10\.[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{32}|[0-9a-f]{40}" --exclude-dir=.git --exclude-dir=research --exclude=CLAUDE.local.md .`
   shows nothing real (pinned action SHAs and the codec tests' sequential hex placeholders are expected).
3. Bump the version in all three places; update `CHANGELOG.md`.
4. Verify on a real HA: install as local add-on, appliance connects, entities
   appear, one command round-trips, connection stable for a few minutes.
5. Tag and push.
