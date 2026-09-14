# Home Assistant Add-on: Zafro Bridge

_Local, cloud-independent control of Zafro / Rowan smart appliances, with the official app still working._

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Zafro window air conditioners (and related Rowan / i4season appliances) have no
local API: they only talk to the vendor cloud. This add-on becomes that cloud
on your own network. Your appliances connect to it, Home Assistant gets a full
`climate` entity plus switches and diagnostics through MQTT Discovery, and the
vendor app keeps working because the bridge relays to the real cloud for you.

If the internet or the vendor cloud goes down, local control keeps working.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
