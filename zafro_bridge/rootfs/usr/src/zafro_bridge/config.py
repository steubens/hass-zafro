"""Runtime settings for the bridge.

The s6 ``run`` script reads the add-on's ``options.json`` (via bashio) and the
Supervisor's MQTT service discovery, then exports everything as environment
variables. Keeping the Python side env-driven means it also runs outside the
add-on (e.g. in tests or a plain container) with no Supervisor present.

Nothing site-specific is hard-coded here; every default is the vendor's
public value or a neutral fallback.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import protocol


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _env_serial_set(name: str) -> frozenset[str]:
    """Parse a comma- and/or whitespace-separated allow-list of serials.

    Blank entries (a stray comma, a trailing newline from the ``run`` script,
    surrounding whitespace) are dropped rather than turning into a bogus
    empty-string "serial". An unset or empty variable yields an empty
    frozenset, which every consumer treats as "accept any appliance".
    """
    raw = os.environ.get(name, "")
    return frozenset(token for token in re.split(r"[,\s]+", raw) if token)


@dataclass(frozen=True, slots=True)
class Settings:
    """All tunables in one immutable object."""

    # Device-facing listener (the appliance is redirected here)
    listen_host: str
    listen_port: int
    tls_certificate_path: Path
    tls_private_key_path: Path

    # Vendor-cloud relay (keeps the official app working)
    cloud_relay_enabled: bool
    cloud_host: str
    cloud_connect_timeout_seconds: float

    # Home Assistant MQTT broker
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None
    mqtt_use_tls: bool
    discovery_prefix: str
    state_topic_prefix: str

    # Refresh cadence for data the device only sends when asked
    state_refresh_seconds: int
    info_refresh_seconds: int

    log_level: str
    bridge_version: str

    # Optional hardening / tuning, all with defaults that reproduce the
    # original unrestricted, single-timeout behaviour when left unset.
    allowed_serials: frozenset[str] = frozenset()
    max_device_sessions: int = 16
    relay_retry_seconds: float = 300.0
    device_connect_timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls, bridge_version: str) -> Settings:
        return cls(
            listen_host=os.environ.get("ZAFRO_LISTEN_HOST", "0.0.0.0"),
            listen_port=_env_int("ZAFRO_LISTEN_PORT", 8443),
            tls_certificate_path=Path(os.environ.get("ZAFRO_TLS_CERT", "/data/tls/cert.pem")),
            tls_private_key_path=Path(os.environ.get("ZAFRO_TLS_KEY", "/data/tls/key.pem")),
            cloud_relay_enabled=_env_bool("ZAFRO_CLOUD_RELAY", True),
            cloud_host=os.environ.get("ZAFRO_CLOUD_HOST", protocol.DEFAULT_CLOUD_HOST),
            cloud_connect_timeout_seconds=_env_float("ZAFRO_CLOUD_TIMEOUT", 10.0),
            mqtt_host=os.environ.get("MQTT_HOST", "core-mosquitto"),
            mqtt_port=_env_int("MQTT_PORT", 1883),
            mqtt_username=os.environ.get("MQTT_USERNAME") or None,
            mqtt_password=os.environ.get("MQTT_PASSWORD") or None,
            mqtt_use_tls=_env_bool("MQTT_SSL", False),
            discovery_prefix=os.environ.get("ZAFRO_DISCOVERY_PREFIX", "homeassistant"),
            state_topic_prefix=os.environ.get("ZAFRO_STATE_PREFIX", "zafro"),
            state_refresh_seconds=_env_int("ZAFRO_STATE_REFRESH", 300),
            info_refresh_seconds=_env_int("ZAFRO_INFO_REFRESH", 900),
            log_level=os.environ.get("ZAFRO_LOG_LEVEL", "info"),
            bridge_version=bridge_version,
            allowed_serials=_env_serial_set("ZAFRO_ALLOWED_SERIALS"),
            max_device_sessions=_env_int("ZAFRO_MAX_DEVICE_SESSIONS", 16),
            relay_retry_seconds=_env_float("ZAFRO_RELAY_RETRY", 300.0),
            device_connect_timeout_seconds=_env_float("ZAFRO_DEVICE_CONNECT_TIMEOUT", 30.0),
        )
