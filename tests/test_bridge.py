"""Bridge orchestration tests with in-memory fakes (no broker, no sockets).

These pin down ordering-sensitive behavior in bridge.py that the end-to-end
tests only reach by chance: session replacement, the broker-reconnect
republish loop, and when discovery is (re)published.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("aiomqtt")

from zafro_bridge.bridge import Bridge  # noqa: E402
from zafro_bridge.config import Settings  # noqa: E402

from zafro_bridge import protocol  # noqa: E402

SERIAL = "TESTSERIAL000001"


def _settings() -> Settings:
    return Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        tls_certificate_path=Path("unused-cert.pem"),
        tls_private_key_path=Path("unused-key.pem"),
        cloud_relay_enabled=False,
        cloud_host=protocol.DEFAULT_CLOUD_HOST,
        cloud_connect_timeout_seconds=1.0,
        mqtt_host="127.0.0.1",
        mqtt_port=1883,
        mqtt_username=None,
        mqtt_password=None,
        mqtt_use_tls=False,
        discovery_prefix="homeassistant",
        state_topic_prefix="zafro",
        state_refresh_seconds=300,
        info_refresh_seconds=900,
        log_level="debug",
        bridge_version="test",
        allowed_serials=frozenset({SERIAL}),
    )


class FakeHomeAssistant:
    """Records what the bridge would publish; every publish yields to the loop."""

    def __init__(self) -> None:
        self.discovery_serials: list[str] = []
        self.availability: list[tuple[str, bool]] = []
        self.states: list[str] = []
        self.on_availability_publish = None

    async def publish_discovery(self, device: Any) -> None:
        self.discovery_serials.append(device.serial_number)
        await asyncio.sleep(0)

    async def publish_availability(self, serial: str, online: bool) -> None:
        self.availability.append((serial, online))
        if self.on_availability_publish is not None:
            self.on_availability_publish(serial)
        await asyncio.sleep(0)

    async def publish_state(self, serial: str, ha_state: dict[str, Any]) -> None:
        self.states.append(serial)
        await asyncio.sleep(0)


class FakeSession:
    def __init__(self, bridge: Bridge) -> None:
        self._bridge = bridge
        self.closed = False
        self.injected: list[bytes] = []

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # A real session reports its own disconnect as it closes.
        await self._bridge._on_device_disconnected(self, SERIAL)  # type: ignore[arg-type]

    async def inject_publish(self, topic: str, payload: bytes) -> None:
        self.injected.append(payload)


def _bridge_with_fake_ha() -> tuple[Bridge, FakeHomeAssistant]:
    bridge = Bridge(_settings())
    fake_ha = FakeHomeAssistant()
    bridge._ha = fake_ha  # type: ignore[assignment]
    return bridge, fake_ha


async def _cancel_background_tasks(bridge: Bridge) -> None:
    for task in list(bridge._background_tasks):
        task.cancel()
    await asyncio.gather(*bridge._background_tasks, return_exceptions=True)


def test_replacing_a_session_never_publishes_offline() -> None:
    async def scenario() -> None:
        bridge, fake_ha = _bridge_with_fake_ha()
        old_session, new_session = FakeSession(bridge), FakeSession(bridge)
        try:
            await bridge._on_device_connected(old_session, SERIAL)  # type: ignore[arg-type]
            await bridge._on_device_connected(new_session, SERIAL)  # type: ignore[arg-type]
            assert old_session.closed
            assert bridge._sessions[SERIAL] is new_session
            assert (SERIAL, False) not in fake_ha.availability
        finally:
            await _cancel_background_tasks(bridge)

    asyncio.run(scenario())


def test_broker_republish_tolerates_a_new_device_connecting_mid_loop() -> None:
    async def scenario() -> None:
        bridge, fake_ha = _bridge_with_fake_ha()
        bridge._state_for("TESTSERIAL00000A")
        bridge._state_for("TESTSERIAL00000B")
        # A brand-new appliance connects while the republish loop is suspended.
        fake_ha.on_availability_publish = lambda serial: bridge._state_for("TESTSERIAL00000C")
        await bridge._on_broker_connected()
        assert "TESTSERIAL00000C" in bridge._states

    asyncio.run(scenario())


def _info_message(**info: Any) -> bytes:
    return json.dumps({"cmd": protocol.CMD_DEVICE_INFO, "sn": SERIAL, "result": info}).encode()


def test_discovery_is_republished_only_when_device_card_details_change() -> None:
    async def scenario() -> None:
        bridge, fake_ha = _bridge_with_fake_ha()
        reply_topic = protocol.command_reply_topic(SERIAL)
        base_info = {protocol.INFO_PRODUCT: "FAKE-MODEL", protocol.INFO_MODULE_FIRMWARE: "1.0.0", protocol.INFO_MCU_PART: "PART"}

        await bridge._on_device_publish(SERIAL, reply_topic, _info_message(**base_info, rssi=-40))
        assert fake_ha.discovery_serials == [SERIAL]

        # A routine Wi-Fi signal refresh only changes state-document data.
        await bridge._on_device_publish(SERIAL, reply_topic, _info_message(**base_info, rssi=-55))
        assert fake_ha.discovery_serials == [SERIAL]

        # A firmware update changes the device card.
        updated_info = {**base_info, protocol.INFO_MODULE_FIRMWARE: "1.0.1"}
        await bridge._on_device_publish(SERIAL, reply_topic, _info_message(**updated_info, rssi=-55))
        assert fake_ha.discovery_serials == [SERIAL, SERIAL]

    asyncio.run(scenario())
