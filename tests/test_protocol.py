"""Protocol builders, parsers and the HA <-> device translation."""

import json

import pytest

from zafro_bridge import protocol

SERIAL = "6ISEComboWF140ASJ0000000000"


def test_topics_and_serial_extraction():
    assert protocol.command_request_topic(SERIAL) == f"dev/I4SEASON/{SERIAL}/command/request"
    assert protocol.command_reply_topic(SERIAL) == f"dev/I4SEASON/{SERIAL}/command/reply"
    assert protocol.lwt_topic(SERIAL) == f"lwt/I4SEASON/{SERIAL}"
    assert protocol.serial_from_client_id(f"dev_{SERIAL}") == SERIAL
    assert protocol.serial_from_client_id("something-else") is None
    assert protocol.serial_from_topic(f"dev/I4SEASON/{SERIAL}/command/reply") == SERIAL
    assert protocol.serial_from_topic(f"lwt/I4SEASON/{SERIAL}") == SERIAL
    assert protocol.serial_from_topic("dev/time/sync") is None


@pytest.mark.parametrize(
    ("serial", "expected"),
    [
        (SERIAL, True),
        ("a", True),
        ("a.b-c_d", True),
        ("a/b", False),  # topic separator
        ("+", False),  # MQTT single-level wildcard
        ("#", False),  # MQTT multi-level wildcard
        ("", False),  # empty
        ("_x", False),  # leading underscore is reserved
        ("a" * 64, True),  # exactly at the limit
        ("a" * 65, False),  # one over the limit
        ("has space", False),
    ],
)
def test_is_valid_serial(serial, expected):
    assert protocol.is_valid_serial(serial) is expected


@pytest.mark.parametrize("bad_serial", ["a/b", "+", "#", "", "_x", "a" * 65])
def test_serial_from_client_id_rejects_invalid_serials(bad_serial):
    assert protocol.serial_from_client_id(f"dev_{bad_serial}") is None


@pytest.mark.parametrize("bad_serial", ["+", "#", "", "_x", "a" * 65])
def test_serial_from_topic_rejects_invalid_serials(bad_serial):
    # "a/b" is deliberately not parametrized here: it can't appear as a single
    # topic segment, so is_valid_serial's "/" rejection is covered instead by
    # test_is_valid_serial and test_serial_from_client_id_rejects_invalid_serials.
    assert protocol.serial_from_topic(f"dev/I4SEASON/{bad_serial}/command/reply") is None


def test_local_response_builders():
    login = protocol.build_local_login_response(1_700_000_000)
    assert login == {
        "code": 0,
        "msg": "ok",
        "data": {
            "access_token": "local-1700000000",
            "expires_in": 604800,
            "scope": "all",
            "token_type": "Bearer",
        },
    }
    assert protocol.build_local_time_response(1_700_000_000) == {"code": 0, "msg": "ok", "data": 1_700_000_000}


def test_redact_for_log_scrubs_sensitive_keys_at_any_depth():
    payload = json.dumps(
        {
            "cmd": 5,
            "data": {"ssid": "MyHomeWifi", "clientId": "abc123", "nested": {"access_token": "secret-token"}},
        }
    ).encode()
    rendered = protocol.redact_for_log(payload)
    assert "MyHomeWifi" not in rendered
    assert "abc123" not in rendered
    assert "secret-token" not in rendered
    assert "***" in rendered
    assert '"cmd":5' in rendered  # non-sensitive keys are untouched


def test_redact_for_log_truncates():
    long_payload = json.dumps({"data": "x" * 500}).encode()
    rendered = protocol.redact_for_log(long_payload, limit=50)
    assert len(rendered) == 53  # limit + "..."
    assert rendered.endswith("...")


def test_redact_for_log_handles_non_json_payload():
    rendered = protocol.redact_for_log(b"\xff\xfenot json at all")
    assert rendered  # some truncated repr, never raises


def test_set_command_matches_captured_shape():
    payload = json.loads(protocol.build_set_command({"poweron": True}))
    assert payload == {"cmd": 6, "sn": None, "user": protocol.BRIDGE_USER_TAG, "data": {"state": {"poweron": True}}}


def test_state_and_info_requests():
    assert json.loads(protocol.build_state_request()) == {"cmd": 2, "user": protocol.BRIDGE_USER_TAG, "data": None}
    assert json.loads(protocol.build_device_info_request()) == {"cmd": 5, "user": protocol.BRIDGE_USER_TAG}


@pytest.mark.parametrize(
    ("command", "payload", "expected"),
    [
        ("hvac_mode", "off", {"poweron": False}),
        ("hvac_mode", "cool", {"poweron": True, "mode": 1}),
        ("hvac_mode", "dry", {"poweron": True, "mode": 2}),
        ("hvac_mode", "fan_only", {"poweron": True, "mode": 3}),
        ("hvac_mode", "heat", {"poweron": True, "mode": 4}),
        ("power", "ON", {"poweron": True}),
        ("power", "OFF", {"poweron": False}),
        ("temperature", "72.0", {"templevel": 72}),
        ("temperature", "40", {"templevel": 61}),  # clamped to the unit's minimum
        ("temperature", "99", {"templevel": 86}),  # clamped to the unit's maximum
        ("fan_mode", "auto", {"eco": False, "extra": False, "sleep": False, "windlevel": 4}),
        ("fan_mode", "low", {"eco": False, "extra": False, "sleep": False, "windlevel": 1}),
        ("fan_mode", "high", {"eco": False, "extra": False, "sleep": False, "windlevel": 3}),
        ("fan_mode", "extra", {"extra": True}),
        ("swing", "on", {"oscset1": True}),
        ("swing", "off", {"oscset1": False}),
        ("swing_horizontal", "on", {"oscset2": True}),
        ("preset", "eco", {"eco": True, "sleep": False}),
        ("preset", "sleep", {"sleep": True, "eco": False}),
        ("preset", "none", {"eco": False, "sleep": False}),
        ("mute", "ON", {"muteon": True}),
        ("light", "OFF", {"lighton": False}),
        ("child_lock", "ON", {"childlockon": True}),
    ],
)
def test_ha_command_to_device(command, payload, expected):
    assert protocol.ha_command_to_device(command, payload) == expected


def test_unknown_commands_are_rejected():
    with pytest.raises(protocol.UnknownCommandError):
        protocol.ha_command_to_device("hvac_mode", "auto")  # no auto HVAC mode on this product
    with pytest.raises(protocol.UnknownCommandError):
        protocol.ha_command_to_device("bogus", "1")
    with pytest.raises(protocol.UnknownCommandError):
        protocol.ha_command_to_device("mute", "maybe")


@pytest.mark.parametrize("bad_temperature", ["abc", "", "nan", "inf", "-inf", "1e999"])
def test_malformed_temperature_payloads_raise_unknown_command_error(bad_temperature):
    """Regression: these used to raise bare ValueError/OverflowError and crash the bridge."""
    with pytest.raises(protocol.UnknownCommandError):
        protocol.ha_command_to_device("temperature", bad_temperature)


@pytest.mark.parametrize(
    ("payload", "expected_templevel"),
    [
        ("70.4", 70),  # rounds down, well within range
        ("70.6", 71),  # rounds up, well within range
        ("86.6", 86),  # rounds up but still clamps to the maximum
    ],
)
def test_temperature_rounding_and_clamping(payload, expected_templevel):
    assert protocol.ha_command_to_device("temperature", payload) == {"templevel": expected_templevel}


@pytest.mark.parametrize("command", ["swing", "swing_horizontal"])
@pytest.mark.parametrize("bad_value", ["onn", "of", "1", "true", "", "maybe"])
def test_swing_typos_are_rejected_not_silently_off(command, bad_value):
    """A typo in a swing payload must be rejected, not silently treated as "off"."""
    with pytest.raises(protocol.UnknownCommandError):
        protocol.ha_command_to_device(command, bad_value)


@pytest.mark.parametrize("command", ["swing", "swing_horizontal"])
@pytest.mark.parametrize(("payload", "expected"), [("ON", True), ("  off  ", False), ("On", True)])
def test_swing_accepts_only_case_insensitive_on_off(command, payload, expected):
    key = protocol.KEY_SWING_VERTICAL if command == "swing" else protocol.KEY_SWING_HORIZONTAL
    assert protocol.ha_command_to_device(command, payload) == {key: expected}


def test_device_state_to_ha_from_captured_snapshot():
    snapshot = {
        "poweron": True, "mode": 1, "templevel": 68, "windlevel": 4, "oscset1": False, "oscset2": True,
        "muteon": False, "temperature": 73, "lighton": True, "childlockon": False, "wrong": 0,
        "tempunit": 1, "sleep": False, "extra": False, "eco": False, "filterthr": 250, "worktime": 146,
    }
    info = {"v": "I4SEASON", "p": "W15491-8K", "ver": "1.0.4", "ssid": "TestNet", "rssi": 45, "mcu_ver": "0.0.1", "mp": "HC32F030K8"}
    ha = protocol.device_state_to_ha(snapshot, info)
    assert ha["hvac_mode"] == "cool" and ha["power"] == "ON"
    assert ha["target_temp"] == 68 and ha["current_temp"] == 73
    assert ha["fan_mode"] == "auto"
    assert ha["swing_v"] == "off" and ha["swing_h"] == "on"
    assert ha["preset"] == "none"
    assert ha["light"] == "ON" and ha["mute"] == "OFF" and ha["child_lock"] == "OFF"
    assert ha["problem"] == "OFF" and ha["fault"] == 0
    assert ha["rssi"] == 45 and ha["ssid"] == "TestNet" and ha["fw"] == "1.0.4" and ha["mcu_fw"] == "0.0.1"
    assert ha["runtime"] == 146 and ha["filter"] == 250 and ha["product"] == "W15491-8K"


def test_device_state_to_ha_off_extra_and_presets():
    assert protocol.device_state_to_ha({"poweron": False, "mode": 4}, {})["hvac_mode"] == "off"
    assert protocol.device_state_to_ha({"poweron": True, "mode": 4, "extra": True, "windlevel": 2}, {})["fan_mode"] == "extra"
    assert protocol.device_state_to_ha({"poweron": True, "eco": True}, {})["preset"] == "eco"
    assert protocol.device_state_to_ha({"poweron": True, "sleep": True}, {})["preset"] == "sleep"
    assert protocol.device_state_to_ha({"poweron": True, "wrong": 3}, {})["problem"] == "ON"


def test_device_state_to_ha_on_completely_empty_input_never_raises():
    ha = protocol.device_state_to_ha({}, {})
    assert ha["hvac_mode"] is None  # poweron never reported
    assert ha["power"] is None
    assert ha["fan_mode"] is None
    assert ha["swing_v"] == "off" and ha["swing_h"] == "off"  # louver-less default
    assert ha["preset"] == "none"
    assert ha["mute"] is None and ha["light"] is None and ha["child_lock"] is None
    assert ha["target_temp"] is None and ha["current_temp"] is None
    assert ha["runtime"] is None and ha["filter"] is None and ha["rssi"] is None
    assert ha["fault"] is None and ha["problem"] is None
    assert ha["ssid"] is None and ha["fw"] is None and ha["mcu_fw"] is None and ha["product"] is None


def test_device_state_to_ha_hvac_mode_semantics():
    # poweron missing entirely -> unknown, not "off"
    assert protocol.device_state_to_ha({}, {})["hvac_mode"] is None
    # poweron present and false -> "off" regardless of mode
    assert protocol.device_state_to_ha({"poweron": False}, {})["hvac_mode"] == "off"
    # poweron true but mode unrecognized -> None, not a fabricated fallback mode
    assert protocol.device_state_to_ha({"poweron": True, "mode": 99}, {})["hvac_mode"] is None
    # poweron true and mode missing -> None
    assert protocol.device_state_to_ha({"poweron": True}, {})["hvac_mode"] is None
    # poweron true and mode recognized -> the mapped mode
    assert protocol.device_state_to_ha({"poweron": True, "mode": 2}, {})["hvac_mode"] == "dry"


def test_device_state_to_ha_tolerates_unhashable_values():
    """A buggy/hostile device sending a list/dict where a scalar is expected must not raise."""
    hostile_state = {
        "poweron": True,
        "mode": ["not", "hashable"],
        "windlevel": {"nested": "dict"},
        "extra": {"truthy": "dict is truthy"},
        "wrong": [1, 2],
        "templevel": [68],
        "temperature": {"x": 1},
        "worktime": [1],
        "filterthr": {"a": 1},
    }
    hostile_info = {"rssi": [45], "ssid": {"nested": True}, "ver": [1], "mcu_ver": {}, "p": [1, 2]}
    ha = protocol.device_state_to_ha(hostile_state, hostile_info)  # must not raise
    assert ha["hvac_mode"] is None  # mode unhashable -> falls back to None, not a crash
    assert ha["fan_mode"] == "extra"  # extra is a truthy dict -> boost wins regardless
    assert ha["fault"] is None and ha["problem"] is None  # wrong is a list, not an int
    assert ha["target_temp"] is None and ha["current_temp"] is None
    assert ha["runtime"] is None and ha["filter"] is None and ha["rssi"] is None
    assert ha["ssid"] is None and ha["fw"] is None and ha["mcu_fw"] is None and ha["product"] is None


def test_device_state_to_ha_numeric_fields_reject_bool():
    """bool is a subclass of int in Python; it must not leak through as a measurement."""
    ha = protocol.device_state_to_ha({"templevel": True, "worktime": False}, {})
    assert ha["target_temp"] is None
    assert ha["runtime"] is None


def test_device_state_to_ha_text_fields_stringify_scalars():
    ha = protocol.device_state_to_ha({}, {"ssid": "TestNet", "p": 123, "ver": 1.5})
    assert ha["ssid"] == "TestNet"
    assert ha["product"] == "123"
    assert ha["fw"] == "1.5"


def test_device_state_to_ha_fault_code_present_but_not_int():
    ha = protocol.device_state_to_ha({"wrong": "not-an-int"}, {})
    assert ha["fault"] is None
    assert ha["problem"] is None
