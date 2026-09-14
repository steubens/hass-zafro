# Security Policy

## Supported versions

Only the latest published release of the Zafro Bridge add-on is supported.
Security fixes are made against the current release; please upgrade before
reporting an issue you haven't reproduced on the latest version.

## Reporting a vulnerability

Please report security vulnerabilities **privately**, not through a public
issue. Use GitHub's private vulnerability reporting on this repository:

**[steubens/hass-zafro → Security → Report a vulnerability](https://github.com/steubens/hass-zafro/security/advisories/new)**

This opens a private advisory visible only to you and the maintainer, where
you can include reproduction details (including anything you wouldn't want
in a public issue, such as a debug log) without exposing it before a fix is
available.

If that link doesn't open a report form, private reporting isn't available
on the repository yet. In that case, open an issue titled **"Security
contact request"** containing no details about the vulnerability, and the
maintainer will reply with a private way to send them.

## Scope

In scope:

- The add-on's device-facing listener (`device_server.py`, the TLS/WebSocket
  endpoint appliances connect to on port 8443).
- The vendor cloud relay (`cloud_relay.py`).
- MQTT packet handling, both the device-facing broker emulation and the
  Home Assistant MQTT client (`mqtt_codec.py`, `device_session.py`,
  `ha_mqtt.py`).
- The add-on's Docker image, AppArmor profile, and Supervisor configuration
  (`Dockerfile`, `apparmor.txt`, `config.yaml`).

Out of scope:

- The appliance's own firmware and the vendor's cloud service — neither is
  this project's code, and neither can be fixed here.
- Issues that require already controlling the Home Assistant host or the
  router itself. (A hostile or compromised client elsewhere on the LAN that
  can reach the device endpoint *is* in scope.)

## Known, by-design limitations

These are documented trade-offs, not vulnerabilities to report — see
`zafro_bridge/DOCS.md` for the full explanation of each:

- **The appliance does not validate the TLS certificate** presented by
  whatever answers for the vendor cloud hostname. This is inherent to the
  appliance's firmware, not something the add-on can change, and is in fact
  what makes local control possible at all.
- **The device endpoint (port 8443) is unauthenticated unless
  `allowed_serials` is set.** The add-on cannot verify the credentials the
  vendor issues to appliances. Set `allowed_serials` and restrict who can
  reach port 8443 to narrow this; see `DOCS.md`'s Security section.
