# Home Assistant App: Zafro Bridge

_Local, cloud-independent control of Zafro / Rowan smart window air conditioners, with the official Zafro mobile app still working._

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Zafro window air conditioners have no local API: they only talk to the vendor
cloud. This app (formerly called an add-on) becomes that cloud on your own
network. Your appliances connect to it, Home Assistant gets a full `climate`
entity plus switches and diagnostics through MQTT Discovery, and the Zafro
mobile app keeps working because the bridge relays to the real cloud for you
(optional, on by default).

If the internet or the vendor cloud goes down, local control keeps working.

**Before you install:** you need the Mosquitto broker app with the MQTT
integration, and a router where you can add a NAT rule that redirects the
appliance's cloud traffic to this app. The **Documentation** tab walks
through both.

Tested on one Zafro 12,000 BTU window AC (product code `W15491-8K`). Other
Zafro air conditioners are expected to work but are untested; please
[report yours](https://github.com/steubens/hass-zafro/issues/new?template=new_device_report.yml).

Not affiliated with, endorsed by, or supported by Zafro, Rowan, or i4season.
Use at your own risk; see the disclaimer in the Documentation tab.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
