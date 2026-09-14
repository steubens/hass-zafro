# Changelog

<!-- https://developers.home-assistant.io/docs/add-ons/presentation#keeping-a-changelog -->

## Unreleased

- **New option `allowed_serials`**: restrict which appliance serial numbers
  the add-on will accept (default: empty, accepts any). A CONNECT for an
  unlisted serial, or a client id that isn't shaped like a device's, is
  refused and the connection closed.
- **Idle appliances are now detected and marked unavailable** in Home
  Assistant after 1.5x their MQTT keepalive (45 seconds for the tested unit)
  of silence, instead of showing stale state indefinitely.
- **A bridge-level availability topic with a last will** now covers every
  entity: if the add-on itself dies unexpectedly, all appliances go
  unavailable in Home Assistant rather than silently freezing.
- **Relay auto-recovery**: while `cloud_relay` is on but an appliance is being
  served locally because the vendor cloud was unreachable or the relay
  dropped, the add-on now
  periodically probes the cloud in the background and, once it's reachable
  again, reconnects that appliance through the relay automatically (a brief
  offline/online blip in Home Assistant is expected when this happens).
- **Discovery and state are now published only once real data has arrived**
  from the appliance, so a Home Assistant restart no longer shows a
  momentary incorrect "off" state before the appliance reports in.
- **Everything is republished after an MQTT broker reconnect**, so a
  Mosquitto restart no longer leaves discovery or state stale.
- A concurrent-session cap now bounds how many device connections the add-on
  will hold open at once, protecting it against unbounded connection floods.
- An appliance connection can no longer publish state for a different serial
  number, and a connection that never completes its MQTT handshake is closed.
- Swing commands with an unrecognized payload are now rejected instead of
  silently turning swing off.
- Routine Wi-Fi signal refreshes no longer re-publish the discovery document.
- **Crash fixes**: malformed or unexpected commands from Home Assistant, odd
  values reported by the appliance, and truncated or oversized MQTT frames
  are now handled without crashing the add-on.
- Minimum supported Home Assistant version raised to **2025.3.0**, the first
  release whose MQTT climate entity supports horizontal swing (which the
  discovery payload already used).
- Removed the unused `hassio_api` permission from the add-on manifest.
- Documentation: disclaimer, privacy, supported-devices, security, and
  uninstall sections added to `DOCS.md`; new `SECURITY.md` and
  `CONTRIBUTING.md`; issue and pull-request templates added.

## 0.1.0

- Initial release.
- Acts as a local cloud-replacement broker for Zafro / Rowan (i4season) appliances.
- Optional transparent relay to the vendor cloud so the official app keeps working.
- Home Assistant MQTT device discovery: climate (mode, target/current temperature,
  fan speed incl. Extra, vertical and horizontal swing, eco/sleep presets),
  mute / display light / child lock switches, problem sensor, and diagnostics
  (Wi-Fi signal and network, module and MCU firmware, runtime, filter counter,
  fault code).
- Works offline: if the cloud is unreachable, the bridge answers the appliance itself.
