# Contributing

Thanks for considering a contribution. This is a small, single-maintainer
add-on, so keep changes focused; for anything larger than a small fix, open
an issue first to discuss the approach.

## Dev setup

You need Python 3.13 and `pytest`. The runtime dependencies (`aiohttp`,
`aiomqtt`) are only needed for the integration tests:

```bash
# Pure-module tests only — no extra dependencies:
python -m pytest -q tests

# Full suite, including integration tests that need aiohttp/aiomqtt and a
# local mosquitto broker on PATH:
pip install pytest aiohttp aiomqtt
python -m pytest -q tests
```

Tests that need `aiohttp` or `aiomqtt` guard themselves with
`pytest.importorskip`, so the plain `python -m pytest -q tests` above always
runs (skipping what it can't) even without those packages installed.
Async tests are driven with plain `asyncio.run(...)` inside ordinary test
functions — this project doesn't use `pytest-asyncio` or any other pytest
plugin.

Byte-compile check and lint (CI runs both, plus the Home Assistant add-on
linter and a Docker build for both architectures):

```bash
python -m py_compile zafro_bridge/rootfs/usr/src/zafro_bridge/*.py
ruff check .
```

No appliance? `tests/fake_appliance.py` simulates one. Run it against a
local or installed add-on with
`python tests/fake_appliance.py --host <ha-host> --port 8443` (with
`zafro_bridge/rootfs/usr/src` on `PYTHONPATH`).

## Code conventions

- Descriptive names; docstrings and comments explain *intent*, not just
  what the code does.
- `protocol.py`, `mqtt_codec.py`, `state.py`, `ha_discovery.py`, and
  `config.py` are **pure** — no network or filesystem I/O. Keep it that way:
  protocol and mapping logic belongs there, not in the network layers
  (`device_server.py`, `device_session.py`, `cloud_relay.py`, `ha_mqtt.py`,
  `bridge.py`). This is what makes the pure modules unit-testable without a
  network.
- Match the surrounding style in whichever file you're editing.

## Changing the protocol

If a change touches how the bridge talks to the appliance, update
`PROTOCOL.md`, `protocol.py`, and the relevant tests **together**, in the
same change. `PROTOCOL.md` is the authoritative reference — code and docs
drifting apart is worse than either being incomplete.

If you're reverse-engineering a new control or a different product to do
this, see `CAPTURE.md`.

## Never commit captures or site-specific data

Raw protocol captures contain device credentials, account ids, and other
real data, and personal deploy details (hosts, IPs, serial numbers) don't
belong in a public repository. Before committing:

- Never add `wsdump.py` or any raw capture file (`.mitm`, `.pcap`, etc.) —
  `.gitignore` already excludes the usual extensions and the `research/`
  directory, but check any new file you add isn't one of these under a
  different name.
- Test vectors and examples should use placeholder values of the same shape
  as real ones (`sn`, `clientId`, tokens, etc.), never real captured data.
- A quick self-check before pushing:
  `grep -rnIE "10\.[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{32}|[0-9a-f]{40}" --exclude-dir=.git .`
  should show nothing real (the pinned action SHAs in `.github/workflows` and
  the obviously sequential hex placeholders in the codec tests are expected).

## Versioning

This project follows semantic versioning loosely, applied to what a Home
Assistant user of the add-on actually experiences:

- **Breaking (major bump)**: renamed entity `unique_id`s (forces users to
  re-link entities), a discovery or state MQTT topic layout change, or a
  removed add-on option.
- **Feature (minor bump)**: new add-on options, new entities, new supported
  behavior.
- **Fix (patch bump)**: bug fixes that don't change the two things above.

A version bump touches **three places**, all in the same commit:

1. `zafro_bridge/config.yaml` (`version:`)
2. `zafro_bridge/rootfs/usr/src/zafro_bridge/version.py` (`VERSION`)
3. `zafro_bridge/CHANGELOG.md` (move the `## Unreleased` entries under the
   new version heading)
