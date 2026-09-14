# hass-zafro — Home Assistant add-on repository

Local, cloud-independent Home Assistant integration for **Zafro** smart
appliances (Rowan Electric Appliance / i4season platform), delivered as a
Home Assistant add-on.

## Add-ons

| Add-on | What it does |
|---|---|
| [**Zafro Bridge**](./zafro_bridge) | Becomes the appliance's cloud on your LAN, exposes it to Home Assistant via MQTT Discovery, and (optionally) relays to the vendor cloud so the official app keeps working. |

## Supported devices

| Status | Device |
|---|---|
| **Tested** | Zafro 12,000 BTU U-shaped window AC (heat + cool), model `54091EWA1`, product code `W15491-8K`, Wi-Fi module firmware `1.0.4`. |
| **Expected to work** | Other appliances controlled by the same Zafro app. The protocol (`PROTOCOL.md`) is generic i4season-platform MQTT, so other models likely behave the same, but only one unit has been verified. Please [report your product code](https://github.com/steubens/hass-zafro/issues/new?template=new_device_report.yml) if you try one. |
| **Unknown** | Other i4season-platform product categories (the platform is used by appliances beyond air conditioners). |

## Install

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fsteubens%2Fhass-zafro)

Or manually: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add
`https://github.com/steubens/hass-zafro`, then install **Zafro Bridge**.
Full setup instructions, including the one router rule you need, are in the
add-on's Documentation tab.

## Router setup (the one manual step)

The appliance uses DNS baked into its firmware, so it must be redirected with
a **NAT rule on your router**: *from the appliance, TCP 443 → Home Assistant
port 8443*. Step-by-step instructions for common routers are in the add-on's
[Documentation](./zafro_bridge/DOCS.md#the-router-rule-required):

- UniFi Dream Machine / Cloud Gateway (native NAT rule in the UI)
- UniFi USG (persistent `config.gateway.json`)
- OpenWrt, pfSense / OPNsense, MikroTik, Asus-Merlin, generic Linux

## Why an add-on (and not a HACS integration)?

These appliances only talk to the vendor cloud and expose nothing on the LAN.
Local control means *being* that cloud: a small server the appliance connects
to. That is what add-ons are for. Home Assistant entities come from MQTT
Discovery, so no custom integration is needed.

## Project layout

- `zafro_bridge/` — the add-on (config, Dockerfile, AppArmor, docs, source).
- `PROTOCOL.md` — the reverse-engineered appliance protocol, in full.
- `CAPTURE.md` — how the protocol was captured and how to capture again.
- `tests/` — unit tests for the pure modules, plus integration tests with a
  fake appliance and a real mosquitto broker (see `CONTRIBUTING.md`).

## Disclaimer

This project is **not affiliated with, endorsed by, or supported by** Zafro,
Rowan Electric Appliance, i4season, or any of their affiliates. Product and
platform names are used only to identify which appliances this add-on is
compatible with. The protocol it speaks was reverse-engineered from the
author's own appliance, for personal interoperability with hardware already
purchased.

This software is provided **as-is**, under the MIT license (see `LICENSE`),
with no warranty of any kind. Use it at your own risk: redirecting an
appliance's cloud traffic and impersonating the vendor's server may conflict
with the vendor's terms of service or affect your appliance's warranty.

## License

MIT.
