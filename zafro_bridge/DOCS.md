# Home Assistant Add-on: Zafro Bridge

Local, cloud-independent control of Zafro / Rowan smart window air
conditioners, integrated into Home Assistant through MQTT Discovery, with the
official Zafro app still working. Other appliances that use the Zafro app may
work but are untested (see [Supported devices](#supported-devices)).

## How it works

These appliances have no local API. Each one keeps an outbound, encrypted
connection to the vendor cloud (`zafro.nbrowan.com`), and the app only ever
talks to that cloud. The appliance, however, does **not** verify the cloud's
TLS certificate. This add-on takes advantage of that:

1. You add one rule on your router that redirects the appliance's cloud traffic
   to this add-on instead.
2. The add-on presents itself as the cloud. The appliance connects and speaks
   its normal protocol (MQTT over WebSocket).
3. The add-on publishes the appliance to Home Assistant via MQTT Discovery and
   relays your commands back to it, entirely on your LAN.
4. Optionally (default on), the add-on also relays the appliance's traffic to
   the real vendor cloud, so the Zafro app keeps working exactly as before.

If your internet or the vendor cloud is down, the add-on answers the appliance
itself and Home Assistant control keeps working. Only the app is affected,
because the app is inherently cloud-based.

## Disclaimer

This add-on is **not affiliated with, endorsed by, or supported by** Zafro,
Rowan Electric Appliance, i4season, or any of their affiliates. Product and
platform names are used only to identify which appliances it is compatible
with. The protocol it speaks was reverse-engineered from the author's own
appliance, for personal interoperability with hardware already purchased, and
is documented in `PROTOCOL.md`.

This software is provided **as-is**, under the MIT license (see the
repository's `LICENSE`), with no warranty of any kind. Use it at your own
risk: redirecting an appliance's cloud traffic and impersonating the vendor's
server may conflict with the vendor's terms of service or affect your
appliance's warranty.

## Supported devices

See the [supported devices table](https://github.com/steubens/hass-zafro#supported-devices)
in the repository README for what has actually been tested versus what is
expected to work. If your appliance works (or doesn't), please
[report it](https://github.com/steubens/hass-zafro/issues/new?template=new_device_report.yml) —
it's the only way the "expected to work" list grows.

## Privacy

What leaves your network depends on the `cloud_relay` option:

- **`cloud_relay: true` (default).** The appliance's connection is relayed to
  the vendor cloud exactly as if the add-on weren't there. Everything the
  appliance would normally send still reaches the vendor: its full state
  (mode, setpoints, sensors), Wi-Fi SSID and signal strength, firmware
  versions, and fault codes. The appliance's login (`device/login`) also still
  goes to the vendor. Commands from Home Assistant go straight to the
  appliance, but the appliance reports each resulting change to the vendor as
  usual, echoing the `hass_bridge` tag the bridge puts on its commands, so the
  vendor can tell those changes came through this add-on. The bridge's
  periodic state and info requests (every `state_refresh_seconds` /
  `info_refresh_seconds`) also make the appliance send full snapshots, which
  reach the vendor too.
- **`cloud_relay: false`.** Nothing leaves your network — the add-on answers
  the appliance itself and never contacts the vendor. The trade-off is that
  the official Zafro app stops working, because the app only ever talks to
  the vendor cloud.

The add-on itself sends data to exactly two places: the vendor relay (only
when `cloud_relay` is on) and your configured MQTT broker. It does not phone
home, collect telemetry, or talk to anything else.

## Requirements

- Home Assistant OS or Supervised (this is an add-on).
- The **Mosquitto broker** add-on installed and the **MQTT** integration set up
  (the add-on finds the broker automatically). Any other broker works too via
  the optional `mqtt_*` settings.
- Your appliance already set up in the Zafro app (joined to Wi-Fi). The add-on
  does not do Wi-Fi pairing.
- A router where you can add a NAT (port redirect) rule. Instructions below.

## Installation

1. Add this repository to the add-on store: **Settings → Add-ons → Add-on
   store → ⋮ → Repositories**, paste the repository URL.
2. Install **Zafro Bridge** and start it. The defaults are correct for Zafro.
3. Open the add-on **Log**. You should see it listening on port 8443 and
   connected to MQTT.
4. Add the router rule (next section). Within a minute the log shows
   `device CONNECT` and the appliance appears in **Settings → Devices &
   services → MQTT**.

   A healthy first connection looks like this at the default `info` log
   level. The lines to look for are `device CONNECT` (the appliance reached
   the add-on) and `Published discovery` (Home Assistant will pick it up);
   `relaying to` appears only with `cloud_relay` on:

   ```
   09:02:34 INFO    __main__: Zafro Bridge 0.1.0 starting
   09:02:34 INFO    zafro_bridge.bridge: allowed_serials is empty; accepting a connection from any appliance
   09:02:34 INFO    zafro_bridge.device_server: Device endpoint listening on https://0.0.0.0:8443 (relay enabled)
   09:02:34 INFO    zafro_bridge.ha_mqtt: Connected to MQTT broker core-mosquitto:1883
   09:02:51 INFO    zafro_bridge.device_server: Device login from 192.168.1.50 (sn=<serial>)
   09:02:52 INFO    zafro_bridge.device_server: Device WebSocket from 192.168.1.50
   09:02:52 INFO    zafro_bridge.device_session: [<serial>] device CONNECT (proto MQIsdp v3, keepalive 30s, will=lwt/I4SEASON/<serial>)
   09:02:52 INFO    zafro_bridge.device_session: [<serial>] relaying to zafro.nbrowan.com (app stays functional)
   09:02:52 INFO    zafro_bridge.bridge: [<serial>] connected; add it to allowed_serials to lock this listener down
   09:02:54 INFO    zafro_bridge.ha_mqtt: Published discovery for <serial> (Zafro W15491-8K 000123)
   ```

   `<serial>` stands for your appliance's serial number and `192.168.1.50`
   for its IP (or your router's, with a hairpin rule; see below).

## The router rule (required)

The appliance uses DNS servers baked into its firmware, so a DNS override on
your network will not redirect it. A NAT rule on the router is the reliable
way. The rule redirects the appliance's outbound **TCP port 443** to the
add-on's port (**8443** by default) on your Home Assistant host.

You need two facts:

- **The appliance's IP.** Find it in your router's client list; its MAC
  address starts with `00:1c:c2`. Give it a DHCP reservation so the rule keeps
  matching. (The add-on log also prints the source IP on `Device login`.)
- **Your Home Assistant host IP.**

If you have more than one appliance, repeat the whole rule (and, per below,
the hairpin rule if it applies) once per appliance IP — each needs its own
NAT entry.

> **If the appliance and Home Assistant are on the same subnet**, a plain
> destination-NAT rule isn't enough: the router only sees the first packet,
> and Home Assistant's reply would go straight back to the appliance instead
> of through the router, so the appliance never sees an answer. You also need
> a source-NAT ("hairpin" / "NAT reflection" / masquerade) rule for that one
> flow so replies return via the router. Most of the router sections below
> have a one-line equivalent (OpenWrt's `reflection`, pfSense/OPNsense's "NAT
> reflection" checkbox, MikroTik's `srcnat`); a generic iptables example is in
> [If the appliance and Home Assistant are on the same subnet](#if-the-appliance-and-home-assistant-are-on-the-same-subnet)
> below. Appliances on a separate VLAN from Home Assistant don't need this.
>
> One side effect: under a source-NAT/hairpin rule, the add-on log's logged
> source IP for the connection is the **router's** address, not the
> appliance's — that's expected and doesn't mean the wrong device connected.

Redirecting *all* of the appliance's port-443 traffic is recommended: it
survives vendor IP changes, and the appliance uses 443 for nothing but its
cloud. (If the vendor ever ships firmware updates from another host on 443,
they would be blocked while the rule is active; disable the rule temporarily
in that case.)

In every example below, replace `AC_IP` with the appliance's IP and `HA_IP`
with your Home Assistant host's IP. If you changed the add-on's host port,
replace `8443` too.

### UniFi — Dream Machine / Cloud Gateway / UXG (UniFi OS, current)

UniFi Network 8.x and newer has native NAT rules in the UI:

**Settings → Routing → NAT → Create New**

| Field | Value |
|---|---|
| Type | Destination NAT |
| Interface | the network the appliance is on (e.g. your IoT VLAN) |
| Protocol | TCP |
| Source | `AC_IP` |
| Destination port | `443` |
| Translated IP address | `HA_IP` |
| Translated port | `8443` |

Apply, then power-cycle the appliance (or wait a few minutes) so it
reconnects. This persists like any other UniFi setting.

On older UniFi OS versions without the NAT UI, use a boot script (the
community `unifios-utilities` / `on_boot.d` mechanism) containing the iptables
line from the *Generic Linux* section below.

### UniFi — USG / USG-Pro (legacy, `config.gateway.json`)

The USG's configuration is written by the controller on every provision, so a
rule added over SSH is temporary. The persistent way is a
`config.gateway.json` file on the **controller** (CloudKey or self-hosted),
which is merged into every provision.

1. Find the USG interface the appliance's network is on. SSH into the USG and
   run `show interfaces`; a tagged VLAN appears as e.g. `eth1.10` (LAN port
   `eth1`, VLAN 10). An untagged LAN is just `eth1`.
2. On the controller, create the file (site name is `default` unless you
   renamed it):
   - CloudKey Gen2 / Gen2 Plus: `/srv/unifi/data/sites/default/config.gateway.json`
   - Self-hosted Linux controller: `/usr/lib/unifi/data/sites/default/config.gateway.json`
   - Windows: `%userprofile%\Ubiquiti UniFi\data\sites\default\config.gateway.json`

   ```json
   {
     "service": {
       "nat": {
         "rule": {
           "5001": {
             "description": "Zafro appliance -> Home Assistant Zafro Bridge",
             "type": "destination",
             "protocol": "tcp",
             "inbound-interface": "eth1.10",
             "source": { "address": "AC_IP" },
             "destination": { "port": "443" },
             "inside-address": { "address": "HA_IP", "port": "8443" }
           }
         }
       }
     }
   }
   ```

   Rule numbers below 5000 are reserved by UniFi; use 5001 or higher. Validate
   the JSON (a stray comma breaks provisioning).
3. Force-provision the USG: **UniFi Network → Devices → USG → Settings /
   Manage → Force provision.** The rule is now applied on every provision and
   reboot.

To test before making it permanent, the same rule can be added live over SSH
(and is lost on the next provision):

```bash
sudo iptables -t nat -I PREROUTING 1 -s AC_IP -p tcp --dport 443 -j DNAT --to-destination HA_IP:8443
sudo conntrack -D -s AC_IP      # drop its current cloud session so it reconnects
```

### OpenWrt

**Network → Firewall → Port Forwards → Add**, or in `/etc/config/firewall`:

```
config redirect
    option name        'Zafro appliance to HA bridge'
    option src         'iot'          # zone the appliance is in (often 'lan')
    option src_ip      'AC_IP'
    option src_dport   '443'
    option proto       'tcp'
    option dest        'lan'          # zone Home Assistant is in
    option dest_ip     'HA_IP'
    option dest_port   '8443'
    option target      'DNAT'
```

Then `/etc/init.d/firewall restart`. If the appliance and Home Assistant are in
the same zone, also enable `option reflection '1'` (hairpin) on the redirect.

### pfSense / OPNsense

**Firewall → NAT → Port Forward → Add**

| Field | Value |
|---|---|
| Interface | the interface the appliance is on (e.g. IOT or LAN) |
| Protocol | TCP |
| Source | Single host, `AC_IP` |
| Destination | any |
| Destination port range | 443 to 443 |
| Redirect target IP | `HA_IP` |
| Redirect target port | 8443 |
| Filter rule association | Add associated filter rule |

Save and apply. (On OPNsense the same fields live under Firewall → NAT →
Port Forward.) Leaving Destination as *any* is what turns a port forward into
an outbound intercept.

### MikroTik (RouterOS)

```
/ip firewall nat add chain=dstnat src-address=AC_IP protocol=tcp dst-port=443 \
    action=dst-nat to-addresses=HA_IP to-ports=8443 \
    comment="Zafro appliance -> Home Assistant Zafro Bridge"
```

Place it above any generic masquerade/dstnat rules. If both devices share a
subnet, add a matching `chain=srcnat action=masquerade` for that flow.

### Asus (Merlin firmware)

Stock Asus firmware can't add custom NAT rules; Asuswrt-Merlin can. Enable
custom scripts (Administration → System → *Enable JFFS custom scripts and
configs*), then add to `/jffs/scripts/firewall-start`:

```bash
#!/bin/sh
iptables -t nat -I PREROUTING -s AC_IP -p tcp --dport 443 -j DNAT --to-destination HA_IP:8443
```

Make it executable (`chmod +x`) and reboot or run it once.

### Generic Linux router (iptables / nftables)

iptables:

```bash
iptables -t nat -I PREROUTING 1 -s AC_IP -p tcp --dport 443 -j DNAT --to-destination HA_IP:8443
iptables -I FORWARD 1 -s AC_IP -d HA_IP -p tcp --dport 8443 -j ACCEPT
```

nftables:

```
table ip nat {
  chain prerouting {
    type nat hook prerouting priority dstnat;
    ip saddr AC_IP tcp dport 443 dnat to HA_IP:8443
  }
}
```

Persist them with your distribution's usual mechanism.

### If the appliance and Home Assistant are on the *same* subnet

When both are on one network the router only sees the first packet, and the
reply from Home Assistant would go straight back to the appliance and be
rejected. Add a source-NAT (masquerade / "hairpin" / "NAT reflection") for that
flow so replies return through the router:

```bash
iptables -t nat -I POSTROUTING 1 -s AC_IP -d HA_IP -p tcp --dport 8443 -j MASQUERADE
```

Most UIs above have an equivalent (OpenWrt "reflection", pfSense "NAT
reflection", MikroTik srcnat). Separate VLANs don't need this.

### No custom NAT rules on your router?

ISP-supplied gateways and most consumer mesh systems (eero, Google/Nest Wifi,
and similar) cannot do destination NAT to an arbitrary internal host and
port, so none of the rules above are possible on them. Options, in order of
preference:

- **Put a router capable of destination NAT in front of the appliance.**
  OpenWrt, OPNsense/pfSense, UniFi, and MikroTik all support it (see above);
  even a cheap secondary router flashed with OpenWrt, placed between your ISP
  gateway and the appliance's network, works.
- **Use a small travel router in "router" (not access-point/bridge) mode**
  for just the appliance's Wi-Fi. Most travel-router firmware (GL.iNet,
  OpenWrt-based units, etc.) supports port forwarding, so the appliance joins
  that router's own subnet and gets NAT'd from there.
- **Wait.** Without one of the above, this add-on can't intercept the
  appliance's traffic.

A DNS override (e.g. a Pi-hole entry or router DNS rewrite for
`zafro.nbrowan.com`) does **not** work as a substitute: the appliance's
firmware uses hard-coded DNS servers and ignores whatever your network hands
out, so redirecting the hostname does nothing. Only a NAT rule that
intercepts the appliance's traffic by IP and port works.

### Verifying

The add-on log shows `Device login from …` and `device CONNECT` when the
appliance arrives, then `Published discovery`. If nothing appears within a
minute, drop the appliance's existing cloud session (clear its conntrack entry
on the router, or power-cycle the unit) so it reconnects into the rule. The
Zafro app should still control the unit (with `cloud_relay` on).

## Configuration

| Option | Default | Description |
|---|---|---|
| `cloud_relay` | `true` | Relay to the vendor cloud so the official app keeps working. Off = fully offline. |
| `cloud_host` | `zafro.nbrowan.com` | Vendor cloud hostname to relay to. |
| `tls_hostname` | `zafro.nbrowan.com` | Name on the self-signed certificate shown to the appliance. |
| `discovery_prefix` | `homeassistant` | Home Assistant MQTT discovery prefix. |
| `state_refresh_seconds` | `300` | How often to request a full state snapshot from each appliance. |
| `info_refresh_seconds` | `900` | How often to refresh Wi-Fi signal and firmware info. |
| `log_level` | `info` | Add-on log verbosity. `debug` logs every MQTT packet type. |
| `allowed_serials` | *(empty list)* | Restrict which appliances the add-on will accept, by serial number. Empty (the default) accepts any appliance that connects. See [Security](#security) for how to find a serial and why you'd set this. |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | *(empty)* | Optional broker override. Leave empty to use the Mosquitto add-on. |

The container listens on port 8443; change the **host** port in the add-on's
Network section if 8443 is taken, and use that port in your router rule.

## What you get in Home Assistant

One device per appliance, with:

- **Climate**: modes Off / Cool / Dry / Fan only / Heat; target temperature
  (61–86 °F); current room temperature; fan Auto / Low / Medium / High /
  Extra; vertical swing; horizontal swing; presets Eco and Sleep.
- **Switches** (configuration): Mute beeper, Display light, Child lock.
- **Binary sensor**: Problem (from the appliance's fault code).
- **Diagnostic sensors**: Wi-Fi signal, Wi-Fi network, Module firmware,
  MCU firmware, Runtime, Filter counter, Fault code.

Behaviour notes, matching the appliance and its app:

- Each mode remembers its own setpoint and fan speed; switching modes recalls them.
- Choosing a numbered or Auto fan speed clears Eco/Sleep, exactly as the app does.
  "Extra" is the appliance's boost fan setting.
- In Dry mode the app offers no fan control; the bridge does not enforce this.
- There is no Auto HVAC mode on these units; "Auto" exists only for the fan.

## Troubleshooting

- **No `device CONNECT` in the log.** The router rule isn't catching the
  appliance. Confirm its IP, that the rule is active, and clear its existing
  session (conntrack, or power-cycle the unit). If the appliance and Home
  Assistant are on the same subnet, confirm the hairpin/source-NAT rule is
  also in place (see [above](#the-router-rule-required)) — without it the
  appliance's SYN reaches the add-on but the reply never finds its way back.
- **NAT rule matches too broadly.** A rule scoped to the whole subnet (rather
  than the appliance's specific IP) can also catch the Home Assistant host's
  own outbound traffic on port 443, including the add-on's own relay
  connection to the vendor cloud — looping it back to itself instead of
  reaching the internet. Symptoms look like `cloud unreachable` or a relay
  that never connects even though the internet is fine. Scope the rule to the
  appliance's IP specifically, not the subnet.
- **The appliance connects, then reconnects every ~30 seconds.** Something in
  the path is altering the WebSocket. The bridge disables WebSocket
  compression deliberately; a proxy or firewall that re-enables it or
  rewrites frames can cause this. Connect the appliance to the bridge directly.
- **Log says no MQTT broker.** Install/start the Mosquitto add-on, or fill in
  the `mqtt_*` options.
- **Device appears but the app stopped working.** Check `cloud_relay` is on and
  the add-on host can reach the internet; the log reports `cloud unreachable`
  if the relay could not connect.
- **AppArmor denials** (`apparmor="DENIED"` in the host journal). Please report
  them with the log line; the profile only includes Home Assistant's standard
  add-on base abstraction (see [Security](#security)), so a denial likely
  means the add-on needs an adjustment, not that something unexpected is
  being blocked.
- **Remove an appliance from Home Assistant.** Remove its router rule first,
  then delete the MQTT device in Settings → Devices & services. While the
  appliance can still reach the add-on, the bridge publishes it again the next
  time it connects (or the add-on reconnects to the MQTT broker).

## What happens when the add-on stops

While the router NAT rule is in place, the appliance's port-443 traffic is
unconditionally pointed at the add-on — that's true whether the add-on is
running or not. So while the rule exists but the add-on is stopped, crashed,
or being reinstalled, the appliance can't reach **any** cloud: local control
through Home Assistant stops, and so does the official Zafro app, until the
add-on (or the rule) comes back. This is the same trade-off as any local
cloud-replacement: the add-on becomes a single point of failure for that
appliance's connectivity in exchange for not depending on the internet for
local control the rest of the time.

## Uninstalling

To remove the add-on and hand the appliance back to the vendor cloud, in this
order:

1. **Remove the router NAT rule** (and its hairpin/source-NAT counterpart, if
   you added one).
2. **Power-cycle the appliance** so it drops its current connection and
   reconnects straight to the real vendor cloud.
3. **Delete the device in Home Assistant**: Settings → Devices & services →
   MQTT → the appliance's device → Delete.
4. **Uninstall the add-on**: Settings → Add-ons → Zafro Bridge → Uninstall.

Optionally, clear any leftover retained MQTT topics under `zafro/` (and the
`homeassistant/device/...` discovery topic, if it wasn't already removed by
step 3) with a tool like [MQTT Explorer](http://mqtt-explorer.com/) — retained
messages otherwise sit on the broker indefinitely.

## Security

**The device endpoint (port 8443) is unauthenticated by default.** The
add-on has no way to verify the credentials the vendor issues to each
appliance, so out of the box, anything on your network that can reach port
8443 and sends a well-formed `dev_<serial>` CONNECT is treated as an
appliance. Two things narrow that:

- **Set `allowed_serials`** (see [Configuration](#configuration)) to the
  serial number(s) of your own appliance(s). Once set, a CONNECT for any
  other serial is refused and the connection is closed immediately. Find your
  appliance's serial either in the add-on log on its first connection, or on
  its device page in Home Assistant (Settings → Devices & services → MQTT →
  the appliance's device) once it has connected once with `allowed_serials`
  still empty. This is a filter, not authentication: a client that knows an
  allowed serial (it is printed on the appliance's label) can still present
  it.
- **Restrict who can reach port 8443** at your router or firewall where your
  network allows it (for example, from the appliance's VLAN only to the Home
  Assistant host), rather than exposing it to your whole network.

Other relevant behavior:

- The add-on does not use host networking, runs under an AppArmor profile,
  and needs only the Supervisor MQTT service (no Supervisor API access). The
  AppArmor profile follows Home Assistant's add-on template, which grants
  general file access so the s6 process supervisor can start the add-on; it
  limits signals and network families but is not a filesystem sandbox.
- The TLS listener on 8443 intentionally accepts old, otherwise-deprecated TLS
  versions and ciphers, because the appliance's embedded TLS stack requires
  them to connect at all. The appliance does not validate the certificate it
  is shown (see `PROTOCOL.md`), so the listener's certificate is self-signed,
  generated on first start into the add-on's private `/data` folder, and
  never needs to be trusted by anything but the appliance.
- Your Zafro account credentials are never needed. The appliance's own device
  credentials pass through the relay untouched and are not stored.
- **Idle connections are detected.** An appliance that goes silent for more
  than 1.5x its MQTT keepalive (45 seconds, for the tested unit's 30-second
  keepalive) is marked unavailable in Home Assistant rather than left
  showing stale state indefinitely.
- **A cap limits concurrent device sessions** so a misbehaving or malicious
  client can't exhaust the add-on's resources by opening unbounded
  connections; this uses a built-in default and is not currently exposed as
  an add-on option.
- **Relay auto-recovery.** When `cloud_relay` is on but the appliance is being
  served locally (the vendor cloud was unreachable at connect time, or the
  relay dropped later), the add-on probes the cloud every 5 minutes. Once it's
  reachable again, the add-on closes the appliance's connection so it
  reconnects through the relay instead. (If the cloud actively refuses the
  appliance's login, the add-on stays local for that connection instead.) Home Assistant sees a brief offline/online blip
  for that appliance when this happens — that's expected, not a fault.
- **A bridge-level availability topic** (in addition to each device's own
  availability) has a last-will set on the broker, so if the add-on itself
  dies unexpectedly, every entity for every appliance goes unavailable rather
  than silently showing stale state.

## Getting help

Issues and feature requests: the [repository issue
tracker](https://github.com/steubens/hass-zafro/issues), using the templates
provided — a [bug report](https://github.com/steubens/hass-zafro/issues/new?template=bug_report.yml)
or a [new device report](https://github.com/steubens/hass-zafro/issues/new?template=new_device_report.yml).
Before posting, please:

- Set `log_level: debug` and reproduce the problem, then include the relevant
  log excerpt. The bridge redacts obviously sensitive values (Wi-Fi SSID,
  account/client identifiers, tokens) before logging a device payload, but
  serial numbers and appliance state are logged in full — review the excerpt
  yourself before pasting it into a public issue if you'd rather keep those
  private too.
- Include your appliance's product code (shown as the device model in Home
  Assistant) and whether `cloud_relay` is on.

Security vulnerabilities should **not** go through a public issue — see
[`SECURITY.md`](https://github.com/steubens/hass-zafro/blob/main/SECURITY.md)
in the repository root for how to report those privately.

## License

MIT.
