"""DeviceState merging of the device's message types."""

from zafro_bridge.state import DeviceState

SERIAL = "TESTMODULEPREFIX00000000000"


def test_snapshot_then_delta_then_info():
    device = DeviceState(SERIAL)

    changed, info_changed = device.apply_device_message(
        {"cmd": 3, "sn": SERIAL, "user": "", "result": {"poweron": True, "mode": 1, "templevel": 68, "origin": 0}}
    )
    assert changed and not info_changed
    assert device.state == {"poweron": True, "mode": 1, "templevel": 68}  # "origin" is not state

    changed, _ = device.apply_device_message({"cmd": 4, "sn": SERIAL, "user": "app_1", "result": {"templevel": 68, "origin": 1}})
    assert changed is False  # identical value -> no republish

    changed, _ = device.apply_device_message({"cmd": 4, "sn": SERIAL, "user": "", "result": {"temperature": 73, "origin": 0}})
    assert changed and device.state["temperature"] == 73

    _, info_changed = device.apply_device_message({"cmd": 5, "sn": SERIAL, "result": {"p": "W15491-8K", "rssi": 45}})
    assert info_changed and device.info["rssi"] == 45
    assert device.display_name == f"Zafro W15491-8K {SERIAL[-6:]}"


def test_online_flag_from_lwt_and_setter():
    device = DeviceState(SERIAL)
    assert device.online is False
    changed, _ = device.apply_device_message({"cmd": 1, "sn": SERIAL, "status": True})
    assert changed and device.online
    assert device.set_online(True) is False
    assert device.set_online(False) is True


def test_ignores_unrelated_messages():
    device = DeviceState(SERIAL)
    assert device.apply_device_message({"cmd": 2, "data": None}) == (False, False)
    assert device.apply_device_message({"cmd": 6, "data": {"state": {"poweron": True}}}) == (False, False)


def test_login_identity_field_is_gone():
    """login_identity was removed; DeviceState no longer tracks REST login fields."""
    device = DeviceState(SERIAL)
    assert not hasattr(device, "login_identity")


def test_has_core_state_requires_both_power_and_mode():
    device = DeviceState(SERIAL)
    assert device.has_core_state is False

    device.merge_state({"poweron": True})
    assert device.has_core_state is False  # mode still unknown

    device.merge_state({"mode": 1})
    assert device.has_core_state is True


def test_merge_state_drops_non_scalar_values_except_timers():
    device = DeviceState(SERIAL)
    changed = device.merge_state(
        {
            "poweron": True,  # scalar: kept
            "templevel": [1, 2],  # list: dropped, not a legitimate state shape
            "extra": {"nested": "dict"},  # dict on a non-timer key: dropped
            "timeron": {"hour": 7, "minute": 30},  # dict on a timer key: kept
            "timeroff": {"hour": 22, "minute": 0},  # dict on a timer key: kept
        }
    )
    assert changed is True
    assert device.state == {
        "poweron": True,
        "timeron": {"hour": 7, "minute": 30},
        "timeroff": {"hour": 22, "minute": 0},
    }


def test_merge_state_with_only_dropped_values_reports_no_change():
    device = DeviceState(SERIAL)
    changed = device.merge_state({"templevel": [1, 2, 3]})
    assert changed is False
    assert device.state == {}


def test_merge_info_drops_non_scalar_values():
    device = DeviceState(SERIAL)
    changed = device.merge_info({"p": "W15491-8K", "rssi": [1, 2], "ver": {"nested": True}})
    assert changed is True
    assert device.info == {"p": "W15491-8K"}


def test_display_name_falls_back_when_product_is_not_a_string():
    device = DeviceState(SERIAL, info={"p": ["not", "a", "string"]})
    assert device.display_name == f"Zafro Air Conditioner {SERIAL[-6:]}"

    device = DeviceState(SERIAL, info={"p": ""})
    assert device.display_name == f"Zafro Air Conditioner {SERIAL[-6:]}"

    device = DeviceState(SERIAL, info={"p": "W15491-8K"})
    assert device.display_name == f"Zafro W15491-8K {SERIAL[-6:]}"
