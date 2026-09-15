"""Zafro / Rowan (i4season) appliance protocol: constants, messages, HA mapping.

Everything here is pure Python with no I/O so it can be unit-tested in isolation.
The wire format was reverse-engineered from a device-side capture; see
PROTOCOL.md at the repository root for the authoritative description.

Two worlds meet in this module:

* the **device world** — integer/boolean keys the appliance understands
  (``poweron``, ``mode``, ``templevel``, ``windlevel`` ...), and
* the **Home Assistant world** — the string vocabulary MQTT climate/switch
  entities use (``cool``, ``fan_only``, ``ON`` ...).

The ``*_to_device`` / ``device_to_*`` helpers are the single place that
translation happens, so the rest of the bridge never has to know both.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Final

# --------------------------------------------------------------------------- #
# Vendor cloud endpoints (defaults; the add-on lets the user override the host)
# --------------------------------------------------------------------------- #
DEFAULT_CLOUD_HOST: Final = "zafro.nbrowan.com"
REST_BASE_PATH: Final = "/iot1"
REST_DEVICE_LOGIN_PATH: Final = "/iot1/device/login"
REST_TIME_PATH: Final = "/iot1/time/second"
WEBSOCKET_PATH: Final = "/ws/iot1/"
WEBSOCKET_SUBPROTOCOL: Final = "mqtt"

# The cloud answers unknown paths with this exact (misspelled) status line.
CLOUD_NOT_FOUND_REASON: Final = "Not Find"

# --------------------------------------------------------------------------- #
# MQTT topics (formatted with the device serial number)
# --------------------------------------------------------------------------- #
TOPIC_COMMAND_REQUEST: Final = "dev/I4SEASON/{sn}/command/request"
TOPIC_COMMAND_REPLY: Final = "dev/I4SEASON/{sn}/command/reply"
TOPIC_LWT: Final = "lwt/I4SEASON/{sn}"
# The device SUBSCRIBEs to this topic on connect, but the bridge deliberately
# never publishes to it: the appliance gets wall-clock time from the REST
# /time/second stub instead, and nothing we've captured relies on a PUBLISH here.
TOPIC_TIME_SYNC: Final = "dev/time/sync"

# MQTT client id the device uses: "dev_<SN>"
DEVICE_CLIENT_ID_PREFIX: Final = "dev_"

# --------------------------------------------------------------------------- #
# Message-type codes carried in the "cmd" field
# --------------------------------------------------------------------------- #
CMD_ONLINE: Final = 1  # device -> cloud: online/last-will status
CMD_REQUEST_ONLINE_STATUS: Final = 2  # cloud -> device: online query; the device answers with a cmd 1, not a snapshot
CMD_STATE_SNAPSHOT: Final = 3  # both ways: {"cmd":3} asks for a full snapshot; the reply carries every key in "result"
# What the bridge sends to ask for that snapshot. The capture shows the vendor
# cloud requesting it as {"user":"app_<id>","cmd":3}; a cmd 2 only gets cmd 1 back.
CMD_REQUEST_STATE: Final = CMD_STATE_SNAPSHOT
CMD_REPLY: Final = 4  # device -> cloud: ack/delta with an "origin" marker
CMD_DEVICE_INFO: Final = 5  # both ways: request / report firmware, wifi, mcu
CMD_SET: Final = 6  # cloud -> device: set one or more state keys

# "origin" values in cmd 4 replies
ORIGIN_DEVICE: Final = 0  # the device changed on its own (button, sensor, mode recall)
ORIGIN_COMMANDED: Final = 1  # echo of a command we (or the app) sent

# The "user" tag we stamp on messages we originate. The device simply echoes it.
BRIDGE_USER_TAG: Final = "hass_bridge"

# --------------------------------------------------------------------------- #
# State keys
# --------------------------------------------------------------------------- #
KEY_POWER: Final = "poweron"
KEY_MODE: Final = "mode"
KEY_TARGET_TEMP: Final = "templevel"
KEY_CURRENT_TEMP: Final = "temperature"
KEY_FAN_LEVEL: Final = "windlevel"
KEY_EXTRA_FAN: Final = "extra"
KEY_ECO: Final = "eco"
KEY_SLEEP: Final = "sleep"
KEY_SWING_VERTICAL: Final = "oscset1"
KEY_SWING_HORIZONTAL: Final = "oscset2"
KEY_MUTE: Final = "muteon"
KEY_DISPLAY_LIGHT: Final = "lighton"
KEY_CHILD_LOCK: Final = "childlockon"
KEY_RUNTIME: Final = "worktime"
KEY_FILTER_COUNTER: Final = "filterthr"
KEY_FAULT_CODE: Final = "wrong"
KEY_TEMP_UNIT: Final = "tempunit"
KEY_TIMER_ON: Final = "timeron"
KEY_TIMER_OFF: Final = "timeroff"

# Device-info keys (cmd 5)
INFO_VENDOR: Final = "v"
INFO_PRODUCT: Final = "p"
INFO_MODULE_FIRMWARE: Final = "ver"
INFO_SERIAL: Final = "sn"
INFO_WIFI_SSID: Final = "ssid"
INFO_WIFI_RSSI: Final = "rssi"
INFO_MCU_FIRMWARE: Final = "mcu_ver"
INFO_MCU_PART: Final = "mp"

# --------------------------------------------------------------------------- #
# Enumerations and ranges (captured from the real unit)
# --------------------------------------------------------------------------- #
# HVAC mode <-> device integer. There is no "auto" HVAC mode on this product;
# "off" is expressed through poweron=false, orthogonal to `mode`.
HVAC_MODE_TO_DEVICE: Final[dict[str, int]] = {"cool": 1, "dry": 2, "fan_only": 3, "heat": 4}
DEVICE_TO_HVAC_MODE: Final[dict[int, str]] = {v: k for k, v in HVAC_MODE_TO_DEVICE.items()}
HA_HVAC_MODES: Final[tuple[str, ...]] = ("off", "cool", "dry", "fan_only", "heat")

# Fan speed <-> device integer. "extra" is the separate boolean boost.
FAN_MODE_TO_DEVICE: Final[dict[str, int]] = {"low": 1, "medium": 2, "high": 3, "auto": 4}
DEVICE_TO_FAN_MODE: Final[dict[int, str]] = {v: k for k, v in FAN_MODE_TO_DEVICE.items()}
FAN_MODE_EXTRA: Final = "extra"
HA_FAN_MODES: Final[tuple[str, ...]] = ("auto", "low", "medium", "high", FAN_MODE_EXTRA)

HA_PRESET_NONE: Final = "none"
HA_PRESET_ECO: Final = "eco"
HA_PRESET_SLEEP: Final = "sleep"
HA_PRESET_MODES: Final[tuple[str, ...]] = (HA_PRESET_ECO, HA_PRESET_SLEEP)

HA_SWING_ON: Final = "on"
HA_SWING_OFF: Final = "off"
HA_SWING_MODES: Final[tuple[str, ...]] = (HA_SWING_ON, HA_SWING_OFF)

HA_ON: Final = "ON"
HA_OFF: Final = "OFF"

# These bounds (and every temperature published to HA) assume tempunit == 1
# (Fahrenheit) — the only unit ever observed on the captured unit. If a device
# reports tempunit == 0 (Celsius) these limits and the "F" discovery hint
# would both be wrong; nothing in the bridge currently detects that case.
MIN_TARGET_TEMP_F: Final = 61
MAX_TARGET_TEMP_F: Final = 86
TARGET_TEMP_STEP_F: Final = 1

TEMP_UNIT_CELSIUS: Final = 0
TEMP_UNIT_FAHRENHEIT: Final = 1

# --------------------------------------------------------------------------- #
# Serial number validation
# --------------------------------------------------------------------------- #
# Serials are used as MQTT topic segments, so they must not contain "/", "+",
# "#", or whitespace, and a leading underscore is reserved (mirrors internal
# topics like "_bridge"). 64 characters is generous headroom over every serial
# observed on real hardware.
_SERIAL_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_valid_serial(serial: str) -> bool:
    """True if ``serial`` is safe to embed as an MQTT topic segment."""
    return bool(_SERIAL_PATTERN.match(serial))


# --------------------------------------------------------------------------- #
# Message builders (payloads the bridge publishes to the device)
# --------------------------------------------------------------------------- #
def build_set_command(state_changes: dict[str, Any]) -> bytes:
    """Build a cmd-6 payload that sets one or more state keys."""
    message = {
        "cmd": CMD_SET,
        "sn": None,
        "user": BRIDGE_USER_TAG,
        "data": {"state": dict(state_changes)},
    }
    return json.dumps(message, separators=(",", ":")).encode()


def build_state_request() -> bytes:
    """Build the cmd-3 request that makes the device reply with a full-state snapshot."""
    message = {"cmd": CMD_REQUEST_STATE, "user": BRIDGE_USER_TAG}
    return json.dumps(message, separators=(",", ":")).encode()


def build_device_info_request() -> bytes:
    """Build a cmd-5 payload asking for firmware / Wi-Fi / MCU details."""
    message = {"cmd": CMD_DEVICE_INFO, "user": BRIDGE_USER_TAG}
    return json.dumps(message, separators=(",", ":")).encode()


def command_request_topic(serial_number: str) -> str:
    return TOPIC_COMMAND_REQUEST.format(sn=serial_number)


def command_reply_topic(serial_number: str) -> str:
    return TOPIC_COMMAND_REPLY.format(sn=serial_number)


def lwt_topic(serial_number: str) -> str:
    return TOPIC_LWT.format(sn=serial_number)


def serial_from_client_id(client_id: str) -> str | None:
    """Extract the device serial from its MQTT client id ("dev_<SN>").

    Returns None both when the prefix is absent and when the extracted
    serial is not topic-safe, so callers never have to validate separately.
    """
    if client_id.startswith(DEVICE_CLIENT_ID_PREFIX):
        candidate = client_id[len(DEVICE_CLIENT_ID_PREFIX):]
        return candidate if is_valid_serial(candidate) else None
    return None


def serial_from_topic(topic: str) -> str | None:
    """Extract the serial from a dev/I4SEASON/<SN>/... or lwt/I4SEASON/<SN> topic."""
    parts = topic.split("/")
    if len(parts) >= 3 and parts[1] == "I4SEASON":
        candidate = parts[2]
        return candidate if is_valid_serial(candidate) else None
    return None


def parse_device_message(payload: bytes) -> dict[str, Any] | None:
    """Decode a JSON payload from the device; None if it is not valid JSON."""
    try:
        decoded = json.loads(payload.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


# --------------------------------------------------------------------------- #
# REST stub responses (the bridge answers these locally instead of relaying)
# --------------------------------------------------------------------------- #
def build_local_login_response(epoch_seconds: int) -> dict[str, Any]:
    """The device/login response the bridge fabricates in local (non-relay) mode.

    The appliance only checks that this parses and carries a token; it never
    validates the token's contents, so a locally-minted value is sufficient.
    """
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "access_token": f"local-{epoch_seconds}",
            "expires_in": 604800,
            "scope": "all",
            "token_type": "Bearer",
        },
    }


def build_local_time_response(epoch_seconds: int) -> dict[str, Any]:
    """The time/second response the bridge fabricates in local (non-relay) mode."""
    return {"code": 0, "msg": "ok", "data": epoch_seconds}


# Keys whose values are redacted regardless of nesting depth when logging a
# raw device payload for debugging.
_REDACTED_KEYS: Final = frozenset({"ssid", "clientId", "clientSecret", "bizuserId", "access_token"})


def _redact_value(value: Any) -> Any:
    """Recursively replace sensitive keys' values with "***" in a decoded JSON value."""
    if isinstance(value, dict):
        return {
            key: ("***" if key in _REDACTED_KEYS else _redact_value(nested))
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def redact_for_log(payload: bytes, limit: int = 160) -> str:
    """Render a device payload for a debug log line with secrets scrubbed.

    JSON payloads have the values of any ``ssid``/``clientId``/``clientSecret``/
    ``bizuserId``/``access_token`` keys (at any nesting depth) replaced before
    truncation; anything that isn't valid JSON is rendered as a truncated
    ``repr`` of the raw bytes instead.
    """
    try:
        decoded = json.loads(payload.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        text = repr(payload)
    else:
        text = json.dumps(_redact_value(decoded), separators=(",", ":"))
    if len(text) > limit:
        return text[:limit] + "..."
    return text


# --------------------------------------------------------------------------- #
# Device state -> Home Assistant vocabulary
# --------------------------------------------------------------------------- #
# Sentinel distinguishing "key absent" from "key present with value None",
# since state.get(key) alone can't tell them apart.
_ABSENT: Final = object()


def _mapped_enum_or_default(mapping: dict[Any, str], raw_value: Any, default: str | None) -> str | None:
    """Look up ``raw_value`` in an enum mapping, tolerating any input safely.

    A misbehaving or hostile device can send a list/dict where an int is
    expected; dict lookups raise TypeError on unhashable keys, which used to
    propagate out of device_state_to_ha and poison the stored state
    permanently. Any lookup failure (missing key, unrecognized value, or an
    unhashable value) falls back to ``default`` instead.
    """
    try:
        return mapping.get(raw_value, default)
    except TypeError:  # raw_value is unhashable (list/dict from the device)
        return default


def _bool_flag(value: Any) -> bool:
    """Coerce any device value to a boolean flag; never raises (bool() doesn't)."""
    return bool(value)


def _on_off_if_present(state: dict[str, Any], key: str) -> str | None:
    """"ON"/"OFF" when ``key`` is present, else None (HA renders None as unknown)."""
    if key not in state:
        return None
    return HA_ON if _bool_flag(state[key]) else HA_OFF


def _numeric_or_none(value: Any) -> int | float | None:
    """Pass through int/float device values; everything else (including bool) is None."""
    if isinstance(value, bool):  # bool is a subclass of int but not a real measurement
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _text_or_none(value: Any) -> str | None:
    """Strings pass through; other scalars are stringified; containers become None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return None
    return str(value)


def device_state_to_ha(state: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    """Translate raw device keys into the flat JSON the HA entities read.

    Every value here is referenced by a ``value_template`` in the discovery
    payload, so the key names form a small contract with ``ha_discovery``.
    This function must never raise: a buggy or hostile device can send any
    JSON-decodable value under any key, and one bad message must not corrupt
    the merged state or crash the bridge.
    """
    poweron_raw = state.get(KEY_POWER, _ABSENT)
    if poweron_raw is _ABSENT:
        hvac_mode: str | None = None
    elif not _bool_flag(poweron_raw):
        hvac_mode = "off"
    else:
        hvac_mode = _mapped_enum_or_default(DEVICE_TO_HVAC_MODE, state.get(KEY_MODE), None)

    if _bool_flag(state.get(KEY_EXTRA_FAN)):
        fan_mode: str | None = FAN_MODE_EXTRA
    else:
        fan_mode = _mapped_enum_or_default(DEVICE_TO_FAN_MODE, state.get(KEY_FAN_LEVEL), None)

    # Louver-less products never report oscset1/oscset2/eco/sleep at all; default
    # to off/none rather than surfacing them as invalid so HA's log stays quiet.
    if _bool_flag(state.get(KEY_ECO)):
        preset = HA_PRESET_ECO
    elif _bool_flag(state.get(KEY_SLEEP)):
        preset = HA_PRESET_SLEEP
    else:
        preset = HA_PRESET_NONE

    fault_raw = state.get(KEY_FAULT_CODE, _ABSENT)
    if fault_raw is _ABSENT or isinstance(fault_raw, bool) or not isinstance(fault_raw, int):
        fault: int | None = None
        problem: str | None = None
    else:
        fault = fault_raw
        problem = HA_ON if fault_raw != 0 else HA_OFF

    return {
        "power": _on_off_if_present(state, KEY_POWER),
        "hvac_mode": hvac_mode,
        "target_temp": _numeric_or_none(state.get(KEY_TARGET_TEMP)),
        "current_temp": _numeric_or_none(state.get(KEY_CURRENT_TEMP)),
        "fan_mode": fan_mode,
        "swing_v": HA_SWING_ON if _bool_flag(state.get(KEY_SWING_VERTICAL)) else HA_SWING_OFF,
        "swing_h": HA_SWING_ON if _bool_flag(state.get(KEY_SWING_HORIZONTAL)) else HA_SWING_OFF,
        "preset": preset,
        "mute": _on_off_if_present(state, KEY_MUTE),
        "light": _on_off_if_present(state, KEY_DISPLAY_LIGHT),
        "child_lock": _on_off_if_present(state, KEY_CHILD_LOCK),
        "runtime": _numeric_or_none(state.get(KEY_RUNTIME)),
        "filter": _numeric_or_none(state.get(KEY_FILTER_COUNTER)),
        "fault": fault,
        "problem": problem,
        "rssi": _numeric_or_none(info.get(INFO_WIFI_RSSI)),
        "ssid": _text_or_none(info.get(INFO_WIFI_SSID)),
        "fw": _text_or_none(info.get(INFO_MODULE_FIRMWARE)),
        "mcu_fw": _text_or_none(info.get(INFO_MCU_FIRMWARE)),
        "product": _text_or_none(info.get(INFO_PRODUCT)),
    }


# --------------------------------------------------------------------------- #
# Home Assistant command -> device state changes
# --------------------------------------------------------------------------- #
class UnknownCommandError(ValueError):
    """Raised when an HA command topic/payload cannot be translated."""


def _clamp_target_temp(raw_value: str) -> int:
    """Parse an HA temperature payload and clamp it into the supported range.

    Anything float() can't parse ("abc", "") or that parses to a non-finite
    value ("nan", "inf", "-inf", "1e999" -> inf) raises UnknownCommandError
    instead of letting ValueError/OverflowError escape to the caller.
    """
    try:
        parsed = float(raw_value)
    except (ValueError, OverflowError):
        raise UnknownCommandError(f"unparsable temperature {raw_value!r}") from None
    if not math.isfinite(parsed):
        raise UnknownCommandError(f"non-finite temperature {raw_value!r}")
    temperature = int(round(parsed))
    return max(MIN_TARGET_TEMP_F, min(MAX_TARGET_TEMP_F, temperature))


def _on_off_to_bool(payload: str) -> bool:
    """Parse an HA switch/climate boolean payload.

    HA's own switches only ever send "ON"/"OFF", but "TRUE"/"1"/"FALSE"/"0"
    are also accepted as deliberate leniency for anyone publishing to these
    command topics by hand (e.g. via mosquitto_pub) rather than through a
    real HA entity.
    """
    normalized = payload.strip().upper()
    if normalized in (HA_ON, "TRUE", "1"):
        return True
    if normalized in (HA_OFF, "FALSE", "0"):
        return False
    raise UnknownCommandError(f"expected ON/OFF, got {payload!r}")


def _swing_to_bool(payload: str) -> bool:
    """Parse an HA swing-mode payload: only "on"/"off" (case-insensitive), strict.

    Unlike _on_off_to_bool, no synonyms are accepted here — a typo in a swing
    command previously fell through to "off" silently, which is worse than
    surfacing it as a rejected command.
    """
    normalized = payload.strip().lower()
    if normalized == HA_SWING_ON:
        return True
    if normalized == HA_SWING_OFF:
        return False
    raise UnknownCommandError(f"expected on/off, got {payload!r}")


def ha_command_to_device(command: str, payload: str) -> dict[str, Any]:
    """Translate an HA command (last topic segment + payload) into device keys.

    Fan speed selection deliberately mirrors the official app, which clears
    eco/sleep/extra whenever a numbered or auto speed is chosen. Presets are
    written explicitly (target true, sibling false) so the outcome is
    deterministic regardless of the device's prior state.
    """
    value = payload.strip()

    if command == "hvac_mode":
        if value == "off":
            return {KEY_POWER: False}
        if value not in HVAC_MODE_TO_DEVICE:
            raise UnknownCommandError(f"unsupported hvac_mode {value!r}")
        return {KEY_POWER: True, KEY_MODE: HVAC_MODE_TO_DEVICE[value]}

    if command == "power":
        return {KEY_POWER: _on_off_to_bool(value)}

    if command == "temperature":
        return {KEY_TARGET_TEMP: _clamp_target_temp(value)}

    if command == "fan_mode":
        if value == FAN_MODE_EXTRA:
            return {KEY_EXTRA_FAN: True}
        if value not in FAN_MODE_TO_DEVICE:
            raise UnknownCommandError(f"unsupported fan_mode {value!r}")
        return {
            KEY_ECO: False,
            KEY_EXTRA_FAN: False,
            KEY_SLEEP: False,
            KEY_FAN_LEVEL: FAN_MODE_TO_DEVICE[value],
        }

    if command == "swing":
        return {KEY_SWING_VERTICAL: _swing_to_bool(value)}

    if command == "swing_horizontal":
        return {KEY_SWING_HORIZONTAL: _swing_to_bool(value)}

    if command == "preset":
        if value == HA_PRESET_ECO:
            return {KEY_ECO: True, KEY_SLEEP: False}
        if value == HA_PRESET_SLEEP:
            return {KEY_SLEEP: True, KEY_ECO: False}
        if value == HA_PRESET_NONE:
            return {KEY_ECO: False, KEY_SLEEP: False}
        raise UnknownCommandError(f"unsupported preset {value!r}")

    if command == "mute":
        return {KEY_MUTE: _on_off_to_bool(value)}
    if command == "light":
        return {KEY_DISPLAY_LIGHT: _on_off_to_bool(value)}
    if command == "child_lock":
        return {KEY_CHILD_LOCK: _on_off_to_bool(value)}

    raise UnknownCommandError(f"unknown command {command!r}")
