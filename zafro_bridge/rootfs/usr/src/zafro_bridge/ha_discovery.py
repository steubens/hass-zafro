"""Builds the Home Assistant MQTT *device-based* discovery payload.

One retained message per appliance declares every entity at once:
a climate entity, config switches, and diagnostic sensors. Pure Python so the
exact payload can be unit-tested; ``ha_mqtt`` only publishes what this returns.

Topic layout (``~`` is the per-device base, e.g. ``zafro/<SN>``):

* ``~/availability``     online / offline
* ``~/state``            one JSON document with every value (see protocol.device_state_to_ha)
* ``~/set/<command>``    HA → bridge commands, one topic per control

Every entity's availability is the logical AND of two topics: the device's
own (this appliance specifically) and the bridge's (the add-on process itself,
see ``bridge_availability_topic``) — so every entity goes unavailable
together if the add-on dies, not just if the appliance drops offline.
"""

from __future__ import annotations

from typing import Any

from . import protocol
from .state import DeviceState

REPOSITORY_URL = "https://github.com/steubens/hass-zafro"
MANUFACTURER = "Rowan Electric Appliance (Zafro)"

_AVAILABLE = "online"
_NOT_AVAILABLE = "offline"


def device_base_topic(state_prefix: str, serial_number: str) -> str:
    return f"{state_prefix}/{serial_number}"


def discovery_topic(discovery_prefix: str, serial_number: str) -> str:
    return f"{discovery_prefix}/device/zafro_{serial_number}/config"


def bridge_availability_topic(state_prefix: str) -> str:
    """The bridge-wide (not per-device) availability topic, backed by an MQTT last will.

    Every device's discovery entry lists this alongside its own per-device
    availability topic so every entity goes unavailable if the add-on itself
    dies, even while the appliance's own connection looks fine.
    """
    return f"{state_prefix}/_bridge/availability"


def _availability_entry(topic: str) -> dict[str, str]:
    """One entry of a discovery payload's "availability" list."""
    return {"topic": topic, "payload_available": _AVAILABLE, "payload_not_available": _NOT_AVAILABLE}


def _sensor(uid: str, state_topic: str, key: str, name: str, *, icon: str | None = None,
            unit: str | None = None, device_class: str | None = None, state_class: str | None = None) -> dict[str, Any]:
    """A diagnostic sensor component reading one key out of the state document."""
    component: dict[str, Any] = {
        "p": "sensor",
        "name": name,
        "unique_id": f"{uid}_{key}",
        "state_topic": state_topic,
        "value_template": f"{{{{ value_json.{key} }}}}",
        "entity_category": "diagnostic",
    }
    if icon:
        component["icon"] = icon
    if unit:
        component["unit_of_measurement"] = unit
    if device_class:
        component["device_class"] = device_class
    if state_class:
        component["state_class"] = state_class
    return component


def _config_switch(uid: str, state_topic: str, base: str, key: str, name: str, icon: str) -> dict[str, Any]:
    """A config-category switch component that both reads and writes one key."""
    return {
        "p": "switch",
        "name": name,
        "unique_id": f"{uid}_{key}",
        "state_topic": state_topic,
        "value_template": f"{{{{ value_json.{key} }}}}",
        "command_topic": f"{base}/set/{key}",
        "payload_on": protocol.HA_ON,
        "payload_off": protocol.HA_OFF,
        "icon": icon,
        "entity_category": "config",
    }


def build_discovery_payload(device: DeviceState, state_prefix: str, bridge_version: str) -> dict[str, Any]:
    """Return the full ``homeassistant/device/.../config`` document.

    Device-based discovery does not accept the ``~`` base-topic shorthand at the
    payload root, so every topic below is written in full from ``base``.
    """
    serial = device.serial_number
    base = device_base_topic(state_prefix, serial)
    state_topic = f"{base}/state"
    availability_topic = f"{base}/availability"
    uid = f"zafro_{serial}"

    def sensor(key: str, name: str, *, icon: str | None = None, unit: str | None = None,
               device_class: str | None = None, state_class: str | None = None) -> dict[str, Any]:
        return _sensor(uid, state_topic, key, name, icon=icon, unit=unit, device_class=device_class, state_class=state_class)

    def config_switch(key: str, name: str, icon: str) -> dict[str, Any]:
        return _config_switch(uid, state_topic, base, key, name, icon)

    climate: dict[str, Any] = {
        "p": "climate",
        "name": None,  # inherit the device name → entity is just "Zafro …"
        "unique_id": f"{uid}_climate",
        "modes": list(protocol.HA_HVAC_MODES),
        "mode_command_topic": f"{base}/set/hvac_mode",
        "mode_state_topic": state_topic,
        "mode_state_template": "{{ value_json.hvac_mode }}",
        "power_command_topic": f"{base}/set/power",
        "payload_on": protocol.HA_ON,
        "payload_off": protocol.HA_OFF,
        "temperature_command_topic": f"{base}/set/temperature",
        "temperature_state_topic": state_topic,
        "temperature_state_template": "{{ value_json.target_temp }}",
        "current_temperature_topic": state_topic,
        "current_temperature_template": "{{ value_json.current_temp }}",
        "temperature_unit": "F",  # mirrors protocol's tempunit == 1 (Fahrenheit) assumption
        "min_temp": protocol.MIN_TARGET_TEMP_F,
        "max_temp": protocol.MAX_TARGET_TEMP_F,
        "temp_step": protocol.TARGET_TEMP_STEP_F,
        "precision": 1.0,
        "fan_modes": list(protocol.HA_FAN_MODES),
        "fan_mode_command_topic": f"{base}/set/fan_mode",
        "fan_mode_state_topic": state_topic,
        "fan_mode_state_template": "{{ value_json.fan_mode }}",
        "swing_modes": list(protocol.HA_SWING_MODES),
        "swing_mode_command_topic": f"{base}/set/swing",
        "swing_mode_state_topic": state_topic,
        "swing_mode_state_template": "{{ value_json.swing_v }}",
        "swing_horizontal_modes": list(protocol.HA_SWING_MODES),
        "swing_horizontal_mode_command_topic": f"{base}/set/swing_horizontal",
        "swing_horizontal_mode_state_topic": state_topic,
        "swing_horizontal_mode_state_template": "{{ value_json.swing_h }}",
        "preset_modes": list(protocol.HA_PRESET_MODES),
        "preset_mode_command_topic": f"{base}/set/preset",
        "preset_mode_state_topic": state_topic,
        "preset_mode_value_template": "{{ value_json.preset }}",
        "optimistic": False,
    }

    components: dict[str, Any] = {
        "climate": climate,
        "mute": config_switch("mute", "Mute beeper", "mdi:volume-off"),
        "light": config_switch("light", "Display light", "mdi:lightbulb-outline"),
        "child_lock": config_switch("child_lock", "Child lock", "mdi:lock-outline"),
        "problem": {
            "p": "binary_sensor",
            "name": "Problem",
            "unique_id": f"{uid}_problem",
            "state_topic": state_topic,
            "value_template": "{{ value_json.problem }}",
            "payload_on": protocol.HA_ON,
            "payload_off": protocol.HA_OFF,
            "device_class": "problem",
            "entity_category": "diagnostic",
        },
        "rssi": sensor("rssi", "Wi-Fi signal", icon="mdi:wifi", state_class="measurement"),
        "ssid": sensor("ssid", "Wi-Fi network", icon="mdi:wifi-settings"),
        "fw": sensor("fw", "Module firmware", icon="mdi:chip"),
        "mcu_fw": sensor("mcu_fw", "MCU firmware", icon="mdi:memory"),
        "runtime": sensor("runtime", "Runtime", icon="mdi:timer-outline", state_class="total_increasing"),
        "filter": sensor("filter", "Filter counter", icon="mdi:air-filter"),
        "fault": sensor("fault", "Fault code", icon="mdi:alert-circle-outline"),
    }

    info = device.info
    product = info.get(protocol.INFO_PRODUCT)
    # Only include optional device fields once known; HA rejects null values.
    device_block: dict[str, Any] = {
        "ids": [uid],
        "name": device.display_name,
        "mf": MANUFACTURER,
        "mdl": product if isinstance(product, str) and product else "Smart appliance",
        "sn": serial,
    }
    if info.get(protocol.INFO_MODULE_FIRMWARE):
        device_block["sw"] = info[protocol.INFO_MODULE_FIRMWARE]
    if info.get(protocol.INFO_MCU_PART):
        device_block["hw"] = info[protocol.INFO_MCU_PART]

    return {
        "dev": device_block,
        "o": {
            "name": "Zafro Bridge",
            "sw": bridge_version,
            "url": REPOSITORY_URL,
        },
        "availability": [
            _availability_entry(availability_topic),
            _availability_entry(bridge_availability_topic(state_prefix)),
        ],
        "availability_mode": "all",
        "qos": 1,
        "cmps": components,
    }
