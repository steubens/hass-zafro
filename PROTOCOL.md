# Zafro / Rowan (nbrowan) appliance protocol

Reverse-engineered 2026-09-13 by intercepting a Zafro 12,000 BTU U-shaped
window AC (heat+cool, model 54091EWA1, product code `W15491-8K`, an i4season
"Combo" Wi-Fi module). Capture was done device-side (the device
connects out to the vendor cloud), so this is the exact protocol the
**appliance** speaks. See `CAPTURE.md` for the method.

## Key finding: the device does not validate its server's TLS certificate

When redirected to a look-alike server presenting an unrelated (mitmproxy) cert
for `zafro.nbrowan.com`, the device connected and proceeded normally. This makes
a **local cloud-replacement server** viable: a local broker can impersonate the
vendor cloud without reproducing any certificate or credential, because the
device trusts whatever answers for its cloud hostname.

## Transport

Two channels, same host (default `zafro.nbrowan.com`, AWS-hosted, port 443 only):

### 1. REST bootstrap (HTTPS)

```
POST https://<host>/iot1/device/login
Content-Type: application/json
{"clientId":"<32hex>","clientSecret":"<32hex>","deviceSn":"<SN>","bizuserId":"<accountId>"}
-> {"code":0,"msg":"ok","data":{"access_token":"<40hex>","expires_in":604800,"token_type":"Bearer"}}

GET https://<host>/iot1/time/second      (Authorization: Bearer <access_token>)
-> {"code":0,"msg":"ok","data":<unix_epoch_seconds>}
```

For local replacement, a stub that returns any well-formed token and a plausible
epoch is sufficient; the device does not appear to verify the token's contents.

### 2. Realtime: MQTT 3.1 over WebSocket-Secure

- URL: `wss://<host>/ws/iot1/`
- MQTT 3.1 (protocol name `MQIsdp`), client id `dev_<SN>`, keepalive 30s.
- MQTT username/password are 40-hex tokens. **A local broker can accept any
  credentials** — they need not be reproduced.
- Last-will: topic `lwt/I4SEASON/<SN>`, payload `{"cmd":1,"sn":"<SN>","status":false}`.

On connect the device:
1. SUBSCRIBEs `dev/I4SEASON/<SN>/command/request` (QoS 1) — commands are delivered here.
2. SUBSCRIBEs `dev/time/sync` (QoS 0).
3. PUBLISHes `lwt/I4SEASON/<SN>` `{"cmd":1,"sn":"<SN>","status":true}` (online/birth).

## Topics

| Topic | Direction | Purpose |
|---|---|---|
| `dev/I4SEASON/<SN>/command/request` | to device | set commands (publish here to control) |
| `dev/I4SEASON/<SN>/command/reply` | from device | acks + spontaneous state reports |
| `lwt/I4SEASON/<SN>` | from device | online/offline (`status` bool) |
| `dev/time/sync` | to device | time sync |

## Message format

**Set a value** — publish to `dev/I4SEASON/<SN>/command/request`:

```json
{"cmd":6,"sn":null,"user":"app_<id>","data":{"state":{"<key>":<value>, ...}}}
```

Multiple keys may be combined in one `state` object.

**Device reply / report** — device publishes to `dev/I4SEASON/<SN>/command/reply`:

```json
{"cmd":4,"sn":"<SN>","user":"<id or empty>","result":{"<key>":<value>, ...,"origin":<n>}}
```

- `origin:1` — the change was commanded (echoes an app/HA command).
- `origin:0` — the device reported this on its own (local button, sensor update,
  or the per-mode values recalled on a mode change). `user` is empty for these.

### Message types (`cmd` codes)

| cmd | Direction | Meaning |
|---|---|---|
| 1 | device → cloud | online / last-will status (`{"status":bool}`) |
| 2 | cloud → device | request a full-state report (`{"cmd":2,"data":null}`) → device answers with cmd 3 |
| 3 | device → cloud | **full-state snapshot** (every key at once) — use for initial state |
| 4 | device → cloud | reply / state delta (echoes changed keys + `origin`) |
| 5 | cloud → device (`{"cmd":5}`) / device → cloud | **device info**: `{"v":"I4SEASON","p":"<product>","ver":"<fw>","sn":"<SN>","ssid":"<wifi>","rssi":<int>,"mcu_ver":"<mcu fw>","mp":"<mcu part>"}` |
| 6 | cloud → device | **set** one or more state keys |

**Full-state read on startup:** publish `{"cmd":2,"sn":null,"user":"...","data":null}` to
`.../command/request`; the device replies with a `cmd:3` snapshot on `.../command/reply`.
Publish `{"cmd":5}` for device info (firmware, Wi-Fi RSSI, MCU part `HC32F030K8`).

## State keys (complete)

| Key | Type | Meaning | Values |
|---|---|---|---|
| `poweron` | bool | Power | true/false |
| `mode` | int | Operating mode | 1=Cool, 2=Dry, 3=Fan, 4=Heat (no Auto mode) |
| `templevel` | int | Target setpoint (°F) | **61–86** (Cool and Heat) |
| `temperature` | int | **Current room temp (°F), read-only** | device-reported (origin 0) |
| `windlevel` | int | Fan speed | 1=Low, 2=Med, 3=High, 4=**Auto** |
| `extra` | bool | "Extra fan" boost (part of the fan selector) | true/false |
| `eco` | bool | Eco preset | true/false |
| `sleep` | bool | Sleep preset | true/false |
| `oscset1` | bool | Swing louver up/down (vertical) | true/false |
| `oscset2` | bool | Swing louver side-to-side (horizontal) | true/false |
| `muteon` | bool | Beeper tones muted (`true` = tones off) | true/false |
| `lighton` | bool | Display panel light | true/false |
| `childlockon` | bool | Child lock | true/false |
| `worktime` | int | Runtime counter, read-only diagnostic | device-reported |
| `tempunit` | int | Temperature unit | 1 = °F, confirmed on the tested unit. `0` (presumably °C) has not been observed. |
| `filterthr` | int | Filter-life counter/threshold | e.g. 250 |
| `wrong` | int | Fault/error code | 0 = healthy |
| `timeron` / `timeroff` | obj | Scheduled on/off timer | `{"ts":<int>,"du":<int>}` — the meaning of `ts` and `du` is unconfirmed (presumably a timestamp/time-of-day and a duration, but neither the units nor which clock `ts` is relative to were verified). Not currently exposed as a Home Assistant entity. |

Device-info fields (from `cmd:5`), read-only: `v` (vendor "I4SEASON"), `p`
(product code, e.g. `W15491-8K`), `ver` (Wi-Fi module firmware, `1.0.4`),
`ssid` (joined Wi-Fi), `rssi` (signal), `mcu_ver` (appliance MCU firmware),
`mp` (MCU part, `HC32F030K8`).

### Notes / behaviors observed

- **Fan selector** UI = Auto, 1, 2, 3, Extra. Picking a numbered speed sends
  `{"eco":false,"extra":false,"sleep":false,"windlevel":N}` (it clears the
  special fan states). "Extra" sends `{"extra":true}`. "Auto" is `windlevel:4`.
- **Per-mode memory:** changing `mode` makes the device report (origin 0) that
  mode's remembered `templevel` (and sometimes `windlevel`). Each mode keeps its
  own setpoint/fan.
- **Dry mode** hides the fan control in the app (treat fan as unavailable there).
- **Current temperature** arrives only via spontaneous `temperature` reports
  (origin 0); track it from `command/reply`.
- **No full-state snapshot is pushed automatically on connect** — the device
  waits to be asked. The bridge requests one explicitly: publishing
  `{"cmd":2,"data":null}` to `.../command/request` reliably gets a `cmd:3`
  full-state reply back (see the `cmd` table above and the "Full-state read
  on startup" note). This is confirmed behavior, not speculative.
- **Temperature unit:** the bridge assumes the appliance always reports
  `templevel`/`temperature` in °F, since `tempunit:0` (°C) was never observed
  on the tested unit and its behavior there is unconfirmed (see the state-key
  table above). It does not change this assumption based on the appliance's
  reported `tempunit`. If a °C-region unit turns out to behave differently,
  this will need revisiting.

## Home Assistant mapping (climate entity)

- `hvac_mode`: off (via `poweron:false`) + Cool/Dry/Fan_only/Heat (`mode` 1–4).
- `target_temperature`: `templevel` (°F, min ~60, max 86, step 1).
- `current_temperature`: `temperature`.
- `fan_mode`: Low/Med/High/Auto (`windlevel` 1/2/3/4) + an "Extra" option (`extra`).
- `swing_mode`: from `oscset1` (vertical) and `oscset2` (horizontal).
- switches/selects: eco, sleep, mute (tones), display light, child lock.
- diagnostic sensors: worktime.
