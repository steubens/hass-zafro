# Changelog

<!-- https://developers.home-assistant.io/docs/apps/presentation#keeping-a-changelog -->

## 0.1.0

First public release.

- Local cloud-replacement broker for Zafro window air conditioners: the
  appliance connects to the app instead of the vendor cloud (via one router
  NAT rule), and Home Assistant control works with no internet at all.
- Optional relay to the vendor cloud (`cloud_relay`, on by default) so the
  Zafro mobile app keeps working. If the cloud is unreachable or the relay
  drops, the app serves the appliance locally and reconnects it through the
  relay once the cloud is back (a brief offline/online blip in Home Assistant).
- Home Assistant MQTT device discovery: climate (mode, target and current
  temperature, fan speed including Extra, vertical and horizontal swing,
  Eco/Sleep presets), Mute beeper / Display light / Child lock switches, a
  Problem sensor, and diagnostics (Wi-Fi signal and network, module and MCU
  firmware, runtime, filter counter, fault code).
- `allowed_serials` option to accept only your own appliances.
- Silent appliances are marked unavailable after 1.5x their MQTT keepalive,
  and every entity goes unavailable if the app itself stops (bridge-level
  availability with an MQTT last will).
- Discovery and state are published only once real data has arrived, and
  everything is republished after an MQTT broker reconnect.
- Hardened device endpoint: size, session and handshake limits, malformed
  input rejected without crashing, and no publishing on behalf of another
  serial.
- Supervisor watchdog via a Docker health check on the device port.
- Requires Home Assistant 2025.3.0 or newer on Home Assistant OS.
