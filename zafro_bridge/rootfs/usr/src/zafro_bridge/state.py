"""In-memory model of one appliance's state, merged from device messages.

The device never pushes a full snapshot unprompted; it sends deltas (cmd 4)
and answers explicit requests with snapshots (cmd 3) and info (cmd 5). This
class merges all of them into one coherent view and reports whether anything
actually changed, so the bridge only republishes to Home Assistant on change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from . import protocol

# Types a device message is allowed to contribute to the merged state/info: plain
# JSON scalars only. A hostile or buggy device sending a list/dict for, say,
# `templevel` must not get stored where later code assumes a number - it would
# then poison every read of that key (device_state_to_ha, HA templates, ...).
_SCALAR_TYPES: Final = (bool, int, float, str, type(None))

# timeron/timeroff are the one legitimate exception: the device reports them as
# small objects ({"ts": <int>, "du": <int>} per PROTOCOL.md; semantics
# unconfirmed), not scalars.
_DICT_ALLOWED_STATE_KEYS: Final = (protocol.KEY_TIMER_ON, protocol.KEY_TIMER_OFF)


@dataclass
class DeviceState:
    """Merged state and identity for a single serial number."""

    serial_number: str
    state: dict[str, Any] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    online: bool = False

    def _merge_scalars(self, target: dict[str, Any], values: dict[str, Any], *, dict_allowed_keys: tuple[str, ...] = ()) -> bool:
        """Merge JSON-scalar values from ``values`` into ``target``. True if changed.

        Shared by merge_state and merge_info: both only ever want to store
        plain scalars (bool/int/float/str/None) plus, for state, the handful
        of keys that are legitimately small dicts (timeron/timeroff). Anything
        else from the device is dropped rather than stored, so a malformed
        message can't corrupt state that later code assumes is a scalar.
        """
        changed = False
        for key, value in values.items():
            if key == "origin":  # bookkeeping marker, not state
                continue
            is_acceptable = isinstance(value, _SCALAR_TYPES) or (
                isinstance(value, dict) and key in dict_allowed_keys
            )
            if not is_acceptable:
                continue
            if target.get(key) != value:
                target[key] = value
                changed = True
        return changed

    def merge_state(self, values: dict[str, Any]) -> bool:
        """Merge a state dict (from cmd 3 or the 'result' of cmd 4). True if changed."""
        return self._merge_scalars(self.state, values, dict_allowed_keys=_DICT_ALLOWED_STATE_KEYS)

    def merge_info(self, values: dict[str, Any]) -> bool:
        """Merge a device-info dict (from cmd 5). True if changed."""
        return self._merge_scalars(self.info, values)

    def set_online(self, online: bool) -> bool:
        """Update availability. True if it flipped."""
        if self.online == online:
            return False
        self.online = online
        return True

    def apply_device_message(self, message: dict[str, Any]) -> tuple[bool, bool]:
        """Route a decoded device JSON message into the model.

        Returns (state_changed, info_changed).
        """
        command_code = message.get("cmd")
        result = message.get("result")
        if command_code in (protocol.CMD_STATE_SNAPSHOT, protocol.CMD_REPLY) and isinstance(result, dict):
            return self.merge_state(result), False
        if command_code == protocol.CMD_DEVICE_INFO and isinstance(result, dict):
            return False, self.merge_info(result)
        if command_code == protocol.CMD_ONLINE:
            return self.set_online(bool(message.get("status"))), False
        return False, False

    def to_ha_state(self) -> dict[str, Any]:
        """The flat JSON published to the HA state topic."""
        return protocol.device_state_to_ha(self.state, self.info)

    @property
    def has_core_state(self) -> bool:
        """True once the device has told us both power and mode at least once.

        Discovery/state should not be published before this: a fresh
        DeviceState with no data yet would otherwise render as a spurious
        "off" in Home Assistant after every bridge restart.
        """
        return protocol.KEY_POWER in self.state and protocol.KEY_MODE in self.state

    @property
    def display_name(self) -> str:
        """A friendly default device name: product code + serial tail."""
        product = self.info.get(protocol.INFO_PRODUCT)
        product_name = product if isinstance(product, str) and product else "Air Conditioner"
        return f"Zafro {product_name} {self.serial_number[-6:]}"
