"""The device-based discovery payload must be well-formed for Home Assistant."""

import json

from zafro_bridge.state import DeviceState

from zafro_bridge import ha_discovery, protocol

SERIAL = "6ISEComboWF140ASJ0000000000"


def _payload():
    device = DeviceState(SERIAL, info={"p": "W15491-8K", "ver": "1.0.4", "mp": "HC32F030K8"})
    return ha_discovery.build_discovery_payload(device, "zafro", "0.1.0")


def test_topics():
    assert ha_discovery.discovery_topic("homeassistant", SERIAL) == f"homeassistant/device/zafro_{SERIAL}/config"
    assert ha_discovery.device_base_topic("zafro", SERIAL) == f"zafro/{SERIAL}"


def test_required_device_discovery_structure():
    payload = _payload()
    # Device-based discovery does not accept the "~" shorthand at the root.
    assert "~" not in payload
    assert payload["dev"]["ids"] == [f"zafro_{SERIAL}"]
    assert payload["dev"]["mdl"] == "W15491-8K" and payload["dev"]["sw"] == "1.0.4" and payload["dev"]["hw"] == "HC32F030K8"
    assert payload["o"]["name"] == "Zafro Bridge" and payload["o"]["sw"] == "0.1.0"
    json.dumps(payload)  # must be serializable


def test_availability_lists_device_and_bridge_topics_with_all_mode():
    payload = _payload()
    assert payload["availability_mode"] == "all"
    topics = {entry["topic"] for entry in payload["availability"]}
    assert topics == {f"zafro/{SERIAL}/availability", ha_discovery.bridge_availability_topic("zafro")}
    for entry in payload["availability"]:
        assert entry["payload_available"] == "online"
        assert entry["payload_not_available"] == "offline"
    # The old root-level shorthand fields must be gone now that "availability" carries them.
    assert "availability_topic" not in payload
    assert "payload_available" not in payload
    assert "payload_not_available" not in payload


def test_bridge_availability_topic():
    assert ha_discovery.bridge_availability_topic("zafro") == "zafro/_bridge/availability"


def test_every_component_has_platform_and_unique_id():
    components = _payload()["cmps"]
    assert set(components) == {"climate", "mute", "light", "child_lock", "problem", "rssi", "ssid", "fw", "mcu_fw", "runtime", "filter", "fault"}
    unique_ids = set()
    for component in components.values():
        assert "p" in component and "unique_id" in component
        assert component["unique_id"] not in unique_ids
        unique_ids.add(component["unique_id"])


def test_climate_matches_protocol_vocabulary():
    climate = _payload()["cmps"]["climate"]
    assert climate["p"] == "climate"
    assert climate["modes"] == list(protocol.HA_HVAC_MODES)
    assert climate["fan_modes"] == list(protocol.HA_FAN_MODES)
    assert climate["preset_modes"] == list(protocol.HA_PRESET_MODES)
    assert climate["swing_modes"] == ["on", "off"] and climate["swing_horizontal_modes"] == ["on", "off"]
    assert climate["temperature_unit"] == "F"
    assert (climate["min_temp"], climate["max_temp"], climate["temp_step"]) == (61, 86, 1)
    assert climate["mode_command_topic"] == f"zafro/{SERIAL}/set/hvac_mode"
    assert climate["temperature_command_topic"] == f"zafro/{SERIAL}/set/temperature"
    assert climate["mode_state_topic"] == f"zafro/{SERIAL}/state"
    assert climate["mode_state_template"] == "{{ value_json.hvac_mode }}"
    assert climate["current_temperature_template"] == "{{ value_json.current_temp }}"


def _find_nulls(value, path=()):
    """Walk a JSON-like structure, yielding the path to every None leaf."""
    if value is None:
        yield path
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield from _find_nulls(nested, path + (key,))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _find_nulls(nested, path + (index,))


def test_empty_info_device_has_no_stray_nulls_and_omits_sw_hw():
    """HA rejects null device fields; an appliance with no info yet must not send any.

    The one deliberate exception is cmps.climate.name = null, which is the
    documented HA idiom for "inherit the parent device's name".
    """
    device = DeviceState(SERIAL)  # no info at all
    payload = ha_discovery.build_discovery_payload(device, "zafro", "0.1.0")
    null_paths = set(_find_nulls(payload))
    assert null_paths == {("cmps", "climate", "name")}
    assert "sw" not in payload["dev"]
    assert "hw" not in payload["dev"]
    assert payload["dev"]["mdl"] == "Smart appliance"


def test_state_json_keys_cover_every_template():
    """Every value_json.<key> the discovery references must exist in the state doc."""
    payload = _payload()
    referenced = set()
    for component in payload["cmps"].values():
        for _key, value in component.items():
            if isinstance(value, str) and "value_json." in value:
                referenced.add(value.split("value_json.")[1].split(" ")[0].rstrip("}"))
    state_doc = protocol.device_state_to_ha({}, {})
    assert referenced <= set(state_doc), referenced - set(state_doc)
