# hass-zafro — Home Assistant app repository

[![CI](https://github.com/steubens/hass-zafro/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/steubens/hass-zafro/actions/workflows/ci.yaml?query=branch%3Amain)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fsteubens%2Fhass-zafro%2Fmain%2Fzafro_bridge%2Fconfig.yaml&query=%24.version&label=version)](zafro_bridge/CHANGELOG.md)
![Supported architectures: aarch64, amd64](https://img.shields.io/badge/arch-aarch64%20%7C%20amd64-blue.svg)

Local, cloud-independent Home Assistant integration for **Zafro** smart
appliances (Rowan Electric Appliance / i4season platform), delivered as a
Home Assistant app (formerly called an add-on).

_Not affiliated with Zafro, Rowan, or i4season; see the [disclaimer](#disclaimer)._

## Apps

| App | What it does |
|---|---|
| [**Zafro Bridge**](./zafro_bridge) | Becomes the appliance's cloud on your LAN, exposes it to Home Assistant via MQTT Discovery, and (optionally) relays to the vendor cloud so the official Zafro mobile app keeps working. |

## Supported devices

| Status | Device |
|---|---|
| **Tested** | Zafro 12,000 BTU U-shaped window AC (heat + cool), model `54091EWA1`, product code `W15491-8K`, Wi-Fi module firmware `1.0.4`. |
| **Expected to work** (untested) | Other Zafro air conditioners set up in the Zafro mobile app. The protocol ([`PROTOCOL.md`](PROTOCOL.md)) is generic i4season-platform MQTT, so other models likely behave the same, but only one unit has been verified. Please [report your product code](https://github.com/steubens/hass-zafro/issues/new?template=new_device_report.yml) if you try one, whether it works or not. |
| **Unknown** | Other i4season-platform product categories (the platform is used by appliances beyond air conditioners). |

## Install

Requires Home Assistant 2025.3 or newer installed as Home Assistant OS (apps
are only available on Home Assistant OS), the **Mosquitto broker** app with
the **MQTT** integration, and a router where you can add one NAT rule
(below).

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fsteubens%2Fhass-zafro)

Or manually: **Settings → Apps → Install app → ⋮ (top right) →
Repositories**, add `https://github.com/steubens/hass-zafro`, select
**Add**, then install **Zafro Bridge** from the app store. On Home Assistant
releases before 2026.2, where apps were still called add-ons, the path is
**Settings → Add-ons → Add-on store → ⋮ → Repositories**. If the button above
opens the store without offering to add the repository, use the manual steps.

Full setup instructions, including the one router rule you need, are on the
app's **Documentation** tab, also readable here as
[`zafro_bridge/DOCS.md`](zafro_bridge/DOCS.md).

## Router setup (the one manual step)

The tested appliance ignores your network's DNS (its firmware uses its own
DNS servers), so it must be redirected with a **NAT rule on your router**:
*from the appliance, TCP 443 → Home Assistant port 8443*. Step-by-step
instructions for common routers are in the app's
[Documentation](./zafro_bridge/DOCS.md#the-router-rule-required):

- UniFi Dream Machine / Cloud Gateway (native NAT rule in the UI)
- UniFi USG (persistent `config.gateway.json`)
- OpenWrt, pfSense / OPNsense, MikroTik, Asus-Merlin, generic Linux

## Why an app (and not a HACS integration)?

These appliances only talk to the vendor cloud and expose nothing on the LAN.
Local control means *being* that cloud: a small server the appliance connects
to. That is what Home Assistant apps are for. Home Assistant entities come
from MQTT Discovery, so no custom integration is needed.

## Related projects

Other projects for Zafro appliances, which may suit a different setup:

- [cgstever/zafro-ha-local](https://github.com/cgstever/zafro-ha-local): also
  local, as a standalone server (nginx, Python, systemd) redirected with a DNS
  override; no relay to the vendor cloud. Tested on a different model.
- [jamesshannon/ha-zafro](https://github.com/jamesshannon/ha-zafro): a HACS
  integration that controls units through the vendor cloud with your Zafro
  account.

Zafro Bridge differs by running as a Home Assistant app and by optionally
relaying to the vendor cloud, so the Zafro mobile app keeps working alongside
local control.

## Help and reporting

- **Your appliance works, or doesn't:** open a
  [new device report](https://github.com/steubens/hass-zafro/issues/new?template=new_device_report.yml).
  It is the only way the supported-devices list grows.
- **Something is broken:** open a
  [bug report](https://github.com/steubens/hass-zafro/issues/new?template=bug_report.yml)
  with a debug log (see
  [Getting help](zafro_bridge/DOCS.md#getting-help) for what to include).
- **Security vulnerability:** report it privately as described in
  [`SECURITY.md`](SECURITY.md), not in a public issue.

## Project layout

- [`zafro_bridge/`](zafro_bridge) — the app (config, Dockerfile, AppArmor, docs, source).
- [`PROTOCOL.md`](PROTOCOL.md) — the reverse-engineered appliance protocol, in full.
- [`CAPTURE.md`](CAPTURE.md) — how the protocol was captured and how to capture again.
- [`tests/`](tests) — unit tests for the pure modules, plus integration tests with a
  fake appliance and a real mosquitto broker (see [`CONTRIBUTING.md`](CONTRIBUTING.md)).

## Disclaimer

This project is **not affiliated with, endorsed by, or supported by** Zafro,
Rowan Electric Appliance, i4season, or any of their affiliates. Product and
platform names are used only to identify which appliances this app is
compatible with. The protocol it speaks was reverse-engineered from the
author's own appliance, for personal interoperability with hardware already
purchased.

This software is provided **as-is**, under the MIT license (see
[`LICENSE`](LICENSE)), with no warranty of any kind. Use it at your own risk:
redirecting an appliance's cloud traffic and impersonating the vendor's
server may conflict with the vendor's terms of service or affect your
appliance's warranty.

## License

MIT — see [`LICENSE`](LICENSE).
