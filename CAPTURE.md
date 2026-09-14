# Capturing the appliance protocol

How the protocol in `PROTOCOL.md` was recovered, and how to capture again —
for example to decode a control the bridge doesn't support yet, or to confirm
a new product behaves the same way. No vendor account, app modification or
rooted phone is needed: the **appliance itself** is intercepted.

## Why device-side

The Zafro app is a Flutter app that ignores user-installed certificates, so
proxying the phone doesn't work on iOS. The appliance, however, does not
validate its cloud's TLS certificate at all. Redirect its cloud traffic to a
machine you control, present any certificate, and its traffic is readable.

## What you need

- A Linux box on your network (a VM or container is fine) with `mitmproxy`.
- A router that can add a destination-NAT rule for one client (see the router
  guide in the add-on's `DOCS.md`).
- The appliance's IP address.

## Steps

1. **Run an interceptor** that impersonates the cloud and relays to the real one:

   ```bash
   mitmdump --mode reverse:https://zafro.nbrowan.com \
            --listen-host 0.0.0.0 --listen-port 443 --ssl-insecure \
            -s wsdump.py -w capture.mitm
   ```

   `wsdump.py` is a small mitmproxy addon that appends every HTTP body and
   every WebSocket frame (hex + printable ASCII) to a log with immediate
   flush. It is not part of this repository: raw captures contain device
   credentials and account ids, so keep the addon and its output in the
   gitignored `research/` directory (or outside the repo entirely) and never
   commit them. The reverse proxy keeps the app working during the capture.

2. **Redirect the appliance** on your router: from `<appliance IP>`, TCP 443 →
   `<interceptor IP>:443`. If the interceptor is on the *same* subnet as the
   appliance, also source-NAT that flow so replies return via the router.
   Then drop the appliance's existing session (clear its conntrack entry or
   power-cycle it) so it reconnects into the interceptor.

3. **Drive the appliance** from the app. Each control appears as a
   `cmd:6` command on `.../command/request` and its echo on
   `.../command/reply`.

4. **Remove the redirect** when done; the appliance returns to the cloud.

## What you'll see

The first frames are the REST login and time sync, then the MQTT session:
`CONNECT` (client id `dev_<SN>`, a will on `lwt/I4SEASON/<SN>`), two
`SUBSCRIBE`s, the online `PUBLISH`, and from then on commands and replies.
`PROTOCOL.md` documents every message type and key.

## Notes

- Because of a hairpin source-NAT the appliance may appear as your router's
  address in the capture; that's still the appliance.
- Never publish a raw capture: it contains the appliance's device
  credentials and your account id. Scrub or keep it private.

## Capturing a different product

If you have a different Zafro/Rowan/i4season appliance and want to confirm
(or extend) `PROTOCOL.md` for it, the same device-side capture applies. You
don't need to capture the whole protocol — a handful of targeted frames is
enough to confirm the platform behaves the same way:

- **The `cmd:5` device-info reply.** Publish `{"cmd":5}` (or just wait — the
  bridge requests one on connect) and capture the reply on
  `.../command/reply`. This gives the product code (`p`), firmware (`ver`),
  and MCU part (`mp`) for the new device.
- **One `cmd:3` full-state snapshot.** Request it with
  `{"cmd":2,"sn":null,"user":"...","data":null}` on `.../command/request` and
  capture the `cmd:3` reply. This is the complete list of state keys the new
  product supports — compare it against the state-key table in
  `PROTOCOL.md` to see what's the same and what's different (different
  appliance types, e.g. non-AC products, will have different keys entirely).
- **One `cmd:4` reply per control you change in the app.** Toggle each
  control the app exposes for that product (power, mode, setpoint, fan,
  swing, presets, whatever's relevant) one at a time and capture the reply
  each produces, so each key's behavior is attributable to a specific action.

Before sharing anything captured this way, redact:

- The appliance's serial number (`sn` field and the `dev_<SN>` client id).
- `clientId` and `clientSecret` from the REST login.
- `bizuserId` (the account id).
- Any `access_token` value.
- The Wi-Fi `ssid` reported in the `cmd:5` info.

Replace each with a same-length placeholder rather than deleting it, so the
frame structure stays intact for comparison. Attach the redacted notes (not
the raw capture) to a [new device
report](https://github.com/steubens/hass-zafro/issues/new?template=new_device_report.yml) —
that's what grows the "expected to work" list for other products.
