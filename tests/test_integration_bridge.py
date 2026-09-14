"""End-to-end bridge tests: a real mosquitto broker, a real ``Bridge``, and a
fake appliance.

Unlike ``tests/test_integration_device.py`` (which drives ``DeviceServer`` in
isolation), these tests run the whole orchestrator -- ``Bridge`` wired to a
genuine ``DeviceServer`` on one side and a genuine MQTT broker on the other --
so the interaction between the two (discovery timing, availability, command
translation, broker-reconnect republish) is exercised the way it would be in
a real Home Assistant install.

Skipped entirely unless aiohttp, aiomqtt, and a ``mosquitto`` binary are all
available (checked at import time), so a plain ``pytest -q tests`` with only
``pytest`` installed still collects (and skips) this file cleanly. Every
scenario is wrapped in ``asyncio.wait_for`` via ``run()`` so a genuine hang
fails fast rather than stalling the suite; no pytest plugins (no
pytest-asyncio) -- everything is a plain coroutine driven by ``asyncio.run``,
matching the rest of this test suite's convention.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import shutil
import socket
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import pytest

aiohttp = pytest.importorskip("aiohttp")
aiomqtt = pytest.importorskip("aiomqtt")

from fake_appliance import FakeAppliance  # noqa: E402
from zafro_bridge.bridge import Bridge  # noqa: E402
from zafro_bridge.config import Settings  # noqa: E402

from zafro_bridge import ha_discovery, mqtt_codec, protocol  # noqa: E402


def _find_mosquitto_binary() -> str | None:
    """Locate a mosquitto broker binary, checking common install locations
    beyond PATH (Homebrew and most Linux distros don't always put it there)."""
    for candidate in (shutil.which("mosquitto"), "/opt/homebrew/sbin/mosquitto", "/usr/sbin/mosquitto"):
        if candidate and Path(candidate).exists():
            return candidate
    return None


_MOSQUITTO_BINARY = _find_mosquitto_binary()
if _MOSQUITTO_BINARY is None:
    pytest.skip("no mosquitto binary found; skipping end-to-end bridge tests", allow_module_level=True)


# A clearly-fake serial and product name -- never anything from a real unit.
SERIAL: Final = "TESTSERIAL000042"

# Overall wall-clock budget for one test scenario. The mosquitto-restart
# scenario in particular needs room for the bridge's fixed MQTT reconnect
# delay (5s, see ha_mqtt._RECONNECT_DELAY_SECONDS) on top of everything else.
_SCENARIO_TIMEOUT_SECONDS = 30.0


def run(coroutine: Awaitable[None]) -> None:
    """Drive one async test scenario to completion, bounded by a hard timeout."""
    asyncio.run(asyncio.wait_for(coroutine, timeout=_SCENARIO_TIMEOUT_SECONDS))


def _free_tcp_port() -> int:
    """An ephemeral TCP port that is free at the moment of the call.

    A second, independent instance of the same technique used by the shared
    ``free_tcp_port`` fixture in conftest.py: these tests need two unrelated
    free ports per scenario (one for mosquitto, one for the device server),
    and a fixture can only be requested once per test under one name.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --------------------------------------------------------------------------- #
# A disposable, real mosquitto broker
# --------------------------------------------------------------------------- #
class MosquittoBroker:
    """Runs a real, throwaway mosquitto broker for the bridge to talk to.

    Started with a minimal temp config (anonymous access, no persistence) on
    a free localhost port; ``stop()`` terminates it (escalating to SIGKILL if
    it doesn't exit promptly) so a failed test never leaves a stray broker
    running. ``restart()`` stops and starts again on the *same* port, for the
    broker-reconnect republish scenario.
    """

    def __init__(self, binary: str, work_dir: Path) -> None:
        self._binary = binary
        self._work_dir = work_dir
        self.port = _free_tcp_port()
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def _config_path(self) -> Path:
        return self._work_dir / "mosquitto.conf"

    def _write_config(self) -> None:
        self._config_path.write_text(f"listener {self.port} 127.0.0.1\nallow_anonymous true\npersistence false\n")

    async def start(self) -> None:
        self._write_config()
        self._process = subprocess.Popen(
            [self._binary, "-c", str(self._config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await self._wait_until_accepting_connections()

    async def _wait_until_accepting_connections(self, timeout: float = 5.0) -> None:
        assert self._process is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(f"mosquitto exited early with code {self._process.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                await asyncio.sleep(0.05)
        raise TimeoutError(f"mosquitto did not start accepting connections on port {self.port} in time")

    async def stop(self) -> None:
        if self._process is None:
            return
        process, self._process = self._process, None
        process.terminate()
        try:
            await asyncio.wait_for(self._wait_exit(process), timeout=5.0)
        except TimeoutError:
            process.kill()
            await self._wait_exit(process)

    @staticmethod
    async def _wait_exit(process: subprocess.Popen[bytes]) -> None:
        while process.poll() is None:
            await asyncio.sleep(0.02)

    async def restart(self) -> None:
        """Stop and start again on the same port (persistence is off, so the
        new process starts with an empty retained-message store -- exactly
        what makes this a meaningful test of the bridge's own republish)."""
        await self.stop()
        await self.start()

    async def __aenter__(self) -> MosquittoBroker:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()


# --------------------------------------------------------------------------- #
# MQTT observer: records every message seen on the broker from the outside
# --------------------------------------------------------------------------- #
class Observer:
    """Background subscriber to ``#`` that records every message it sees.

    Independent of anything the bridge does internally -- this is exactly
    what a real Home Assistant MQTT integration would observe.
    """

    def __init__(self, client: aiomqtt.Client) -> None:
        self._client = client
        self.messages: list[tuple[str, bytes]] = []
        self._task: asyncio.Task[None] | None = None
        self._next_unread_index = 0

    async def start(self) -> None:
        await self._client.subscribe("#")
        self._task = asyncio.create_task(self._read_loop(), name="mqtt-observer")

    async def _read_loop(self) -> None:
        try:
            async for message in self._client.messages:
                payload = message.payload
                if not isinstance(payload, (bytes, bytearray)):
                    payload = str(payload).encode()
                self.messages.append((str(message.topic), bytes(payload)))
        except aiomqtt.MqttError:
            # The broker went away (e.g. the restart scenario deliberately
            # kills it); nothing more to observe on this connection.
            pass

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def all_on_topic(self, topic: str) -> list[bytes]:
        return [payload for seen_topic, payload in self.messages if seen_topic == topic]

    async def wait_for(
        self, topic: str, predicate: Callable[[bytes], bool] = lambda _payload: True, timeout: float = 5.0
    ) -> bytes:
        """Wait for the next (already-seen or future) message on ``topic`` matching ``predicate``."""
        deadline = time.monotonic() + timeout
        index = 0
        while True:
            while index < len(self.messages):
                seen_topic, payload = self.messages[index]
                index += 1
                if seen_topic == topic and predicate(payload):
                    return payload
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for a message on {topic!r} matching the predicate")
            await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- #
# Bridge settings and a connect-with-retry helper for the device endpoint
# --------------------------------------------------------------------------- #
def make_bridge_settings(*, device_port: int, cert_path: Path, key_path: Path, mqtt_port: int, **overrides: Any) -> Settings:
    base = Settings(
        listen_host="127.0.0.1",
        listen_port=device_port,
        tls_certificate_path=cert_path,
        tls_private_key_path=key_path,
        cloud_relay_enabled=False,
        cloud_host=protocol.DEFAULT_CLOUD_HOST,
        cloud_connect_timeout_seconds=1.0,
        mqtt_host="127.0.0.1",
        mqtt_port=mqtt_port,
        mqtt_username=None,
        mqtt_password=None,
        mqtt_use_tls=False,
        discovery_prefix="homeassistant",
        state_topic_prefix="zafro",
        state_refresh_seconds=300,
        info_refresh_seconds=900,
        log_level="debug",
        bridge_version="test",
        allowed_serials=frozenset(),
        max_device_sessions=16,
        relay_retry_seconds=300.0,
        device_connect_timeout_seconds=10.0,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


async def _connect_appliance(url: str, bridge_task: asyncio.Task[None], timeout: float = 5.0) -> FakeAppliance:
    """Connect a FakeAppliance, retrying while the device listener comes up.

    If the bridge task itself has already failed, that failure is surfaced
    immediately instead of masking it behind a generic connect timeout.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if bridge_task.done():
            error = bridge_task.exception()
            if error is not None:
                raise error
            raise RuntimeError("bridge task ended before the device endpoint ever came up")
        appliance = FakeAppliance(url)
        try:
            await appliance.connect()
            return appliance
        except Exception as error:  # noqa: BLE001 - retry until the listener is up
            last_error = error
            await asyncio.sleep(0.05)
    raise TimeoutError(f"device endpoint at {url} never became reachable: {last_error}")


# A fake but internally-consistent state/info pair: enough for
# DeviceState.has_core_state (poweron + mode) plus a distinctive product name
# to prove discovery picked up real device info, not a placeholder.
_FAKE_STATE: Final[dict[str, Any]] = {
    protocol.KEY_POWER: True,
    protocol.KEY_MODE: protocol.HVAC_MODE_TO_DEVICE["cool"],
    protocol.KEY_TARGET_TEMP: 70,
    protocol.KEY_CURRENT_TEMP: 68,
    protocol.KEY_FAN_LEVEL: protocol.FAN_MODE_TO_DEVICE["medium"],
    protocol.KEY_TEMP_UNIT: protocol.TEMP_UNIT_FAHRENHEIT,
}
_FAKE_INFO: Final[dict[str, Any]] = {
    protocol.INFO_PRODUCT: "FAKE-MODEL-X1",
    protocol.INFO_MODULE_FIRMWARE: "9.9.9",
    protocol.INFO_MCU_FIRMWARE: "1.1.1",
    protocol.INFO_WIFI_SSID: "fake-network",
    protocol.INFO_WIFI_RSSI: -42,
}


async def _answer_initial_bridge_requests(
    appliance: FakeAppliance, serial: str, state: dict[str, Any], info: dict[str, Any], timeout: float = 6.0
) -> None:
    """Wait for the bridge's post-connect cmd-2 (state) and cmd-5 (info)
    requests and answer both, PUBACKing each injected request as we go --
    exactly what a real appliance does on its boot sequence.

    The bridge sends both requests back-to-back without waiting for a reply,
    so their relative arrival order versus the PUBACKs the bridge (as our
    broker) sends back for *our* QoS-1 replies is not guaranteed -- this
    drains and ignores PUBACKs as they show up rather than assuming a fixed
    request/ack interleaving.
    """
    seen_state_request = False
    seen_info_request = False
    deadline = time.monotonic() + timeout
    while not (seen_state_request and seen_info_request):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("bridge never sent both its initial cmd-2 and cmd-5 requests")
        packet = await appliance.recv_packet(timeout=remaining)
        if isinstance(packet, mqtt_codec.PubAckPacket):
            continue  # acknowledges one of our own replies below; nothing to do
        if not isinstance(packet, mqtt_codec.PublishPacket):
            raise AssertionError(f"expected an injected PUBLISH or a PUBACK, got {packet!r}")
        if packet.qos == 1 and packet.packet_id is not None:
            await appliance.send_puback(packet.packet_id)
        message = protocol.parse_device_message(packet.payload) or {}
        if message.get("cmd") == protocol.CMD_REQUEST_STATE and not seen_state_request:
            seen_state_request = True
            await appliance.publish_state_snapshot(serial, state)
        elif message.get("cmd") == protocol.CMD_DEVICE_INFO and not seen_info_request:
            seen_info_request = True
            await appliance.publish_device_info(serial, info)

    # At least one PUBACK for our two QoS-1 replies above may still be in
    # flight; drain it (and it alone) so a caller's own assert_silent() right
    # after this doesn't trip over leftover traffic from our own handshake.
    with contextlib.suppress(asyncio.TimeoutError):
        while True:
            trailing = await appliance.recv_packet(timeout=0.3)
            if not isinstance(trailing, mqtt_codec.PubAckPacket):
                raise AssertionError(f"unexpected packet while draining trailing PUBACKs: {trailing!r}")


# --------------------------------------------------------------------------- #
# The harness: a real mosquitto broker + a real Bridge + an observer client
# --------------------------------------------------------------------------- #
class BridgeHarness:
    """Bundles the moving pieces every scenario below needs: a live mosquitto
    broker, a ``Bridge`` running against it with a real ``DeviceServer``, and
    an MQTT client observing (and, when a test needs it, publishing HA
    commands on) every topic from the outside.

    Usage::

        async with BridgeHarness(mosquitto_binary=..., tmp_path=..., cert_path=..., key_path=...) as harness:
            appliance = await harness.connect_appliance()
            await _answer_initial_bridge_requests(appliance, harness.serial, harness.state, harness.info)
            ... assertions using harness.observer / harness.mqtt_client ...
    """

    def __init__(
        self,
        *,
        mosquitto_binary: str,
        tmp_path: Path,
        cert_path: Path,
        key_path: Path,
        serial: str = SERIAL,
        state: dict[str, Any] | None = None,
        info: dict[str, Any] | None = None,
        settings_overrides: dict[str, Any] | None = None,
    ) -> None:
        self._mosquitto_binary = mosquitto_binary
        self._tmp_path = tmp_path
        self._cert_path = cert_path
        self._key_path = key_path
        self.serial = serial
        self.state = dict(_FAKE_STATE) if state is None else state
        self.info = dict(_FAKE_INFO) if info is None else info
        self._settings_overrides = settings_overrides or {}

        self.mosquitto: MosquittoBroker | None = None
        self.settings: Settings | None = None
        self.device_url: str = ""
        self.bridge: Bridge | None = None
        self.bridge_task: asyncio.Task[None] | None = None
        self.mqtt_client: aiomqtt.Client | None = None
        self.observer: Observer | None = None

    async def __aenter__(self) -> BridgeHarness:
        self.mosquitto = MosquittoBroker(self._mosquitto_binary, self._tmp_path)
        await self.mosquitto.start()

        device_port = _free_tcp_port()
        self.device_url = f"wss://127.0.0.1:{device_port}{protocol.WEBSOCKET_PATH}"
        self.settings = make_bridge_settings(
            device_port=device_port,
            cert_path=self._cert_path,
            key_path=self._key_path,
            mqtt_port=self.mosquitto.port,
            **self._settings_overrides,
        )
        self.bridge = Bridge(self.settings)
        self.bridge_task = asyncio.create_task(self.bridge.run(), name="bridge-under-test")

        self.mqtt_client = aiomqtt.Client(hostname="127.0.0.1", port=self.mosquitto.port, identifier="bridge-test-observer")
        await self.mqtt_client.__aenter__()
        self.observer = Observer(self.mqtt_client)
        await self.observer.start()

        # Confirm the bridge itself is live (connected to the broker and
        # publishing its own availability) before touching anything else.
        await self.observer.wait_for(
            self.bridge_availability_topic, predicate=lambda payload: payload == b"online", timeout=10.0
        )
        return self

    async def connect_appliance(self) -> FakeAppliance:
        assert self.bridge_task is not None
        return await _connect_appliance(self.device_url, self.bridge_task)

    async def connect_and_answer(self, *, keepalive_seconds: int = 60) -> FakeAppliance:
        """Connect a fake appliance, complete the CONNECT handshake, and
        answer the bridge's initial cmd-2/cmd-5 requests. A generous keepalive
        is used so a scenario that idles for a few seconds mid-test (e.g.
        waiting out the broker reconnect delay) is never dropped by the
        session's own keepalive watchdog."""
        appliance = await self.connect_appliance()
        connack = await appliance.handshake(self.serial, keepalive_seconds=keepalive_seconds)
        if connack.return_code != mqtt_codec.CONNACK_ACCEPTED:
            await appliance.close()
            raise AssertionError(f"handshake was refused: CONNACK return_code={connack.return_code}")
        await _answer_initial_bridge_requests(appliance, self.serial, self.state, self.info)
        return appliance

    @property
    def base_topic(self) -> str:
        assert self.settings is not None
        return ha_discovery.device_base_topic(self.settings.state_topic_prefix, self.serial)

    @property
    def discovery_topic(self) -> str:
        assert self.settings is not None
        return ha_discovery.discovery_topic(self.settings.discovery_prefix, self.serial)

    @property
    def bridge_availability_topic(self) -> str:
        assert self.settings is not None
        return ha_discovery.bridge_availability_topic(self.settings.state_topic_prefix)

    async def __aexit__(self, *exc_info: object) -> None:
        if self.bridge_task is not None and not self.bridge_task.done():
            self.bridge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.bridge_task
        if self.observer is not None:
            await self.observer.stop()
        if self.mqtt_client is not None:
            with contextlib.suppress(Exception):
                await self.mqtt_client.__aexit__(None, None, None)
        if self.mosquitto is not None:
            await self.mosquitto.stop()


def _harness(tmp_path: Path, tls_cert_pair: tuple[Path, Path], **kwargs: Any) -> BridgeHarness:
    cert_path, key_path = tls_cert_pair
    return BridgeHarness(mosquitto_binary=_MOSQUITTO_BINARY, tmp_path=tmp_path, cert_path=cert_path, key_path=key_path, **kwargs)


# =============================================================================
# Scenarios
# =============================================================================
async def _scenario_discovery_and_state_after_initial_handshake(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    async with _harness(tmp_path, tls_cert_pair) as harness:
        appliance = await harness.connect_and_answer()
        try:
            discovery_payload = await harness.observer.wait_for(harness.discovery_topic, timeout=5.0)
            discovery = json.loads(discovery_payload)
            assert discovery["dev"]["mdl"] == _FAKE_INFO[protocol.INFO_PRODUCT]
            assert discovery["availability_mode"] == "all"
            availability_topics = {entry["topic"] for entry in discovery["availability"]}
            assert availability_topics == {f"{harness.base_topic}/availability", harness.bridge_availability_topic}

            assert await harness.observer.wait_for(
                harness.bridge_availability_topic, predicate=lambda p: p == b"online", timeout=5.0
            )
            assert await harness.observer.wait_for(
                f"{harness.base_topic}/availability", predicate=lambda p: p == b"online", timeout=5.0
            )

            state_payload = await harness.observer.wait_for(f"{harness.base_topic}/state", timeout=5.0)
            state = json.loads(state_payload)
            assert state["hvac_mode"] == "cool"
            assert state["target_temp"] == 70

            # No state document was ever published showing "off" before the
            # device's real snapshot arrived (the spurious-restart-off bug).
            for payload in harness.observer.all_on_topic(f"{harness.base_topic}/state"):
                assert json.loads(payload)["hvac_mode"] != "off"
        finally:
            await appliance.close()


def test_discovery_and_state_after_initial_handshake(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    run(_scenario_discovery_and_state_after_initial_handshake(tmp_path, tls_cert_pair))


async def _scenario_temperature_and_hvac_mode_commands(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    async with _harness(tmp_path, tls_cert_pair) as harness:
        appliance = await harness.connect_and_answer()
        try:
            temperature_topic = f"{harness.base_topic}/set/temperature"
            hvac_mode_topic = f"{harness.base_topic}/set/hvac_mode"

            # Garbage temperature payloads must be ignored, not crash the bridge.
            for bad_payload in ("abc", "nan", "inf"):
                await harness.mqtt_client.publish(temperature_topic, payload=bad_payload)
                await asyncio.sleep(0.2)
                assert harness.bridge_task is not None and not harness.bridge_task.done()
            await appliance.assert_silent(timeout=0.3)

            # A valid payload afterwards must still work normally.
            await harness.mqtt_client.publish(temperature_topic, payload="72")
            command = await appliance.expect(mqtt_codec.PublishPacket, timeout=3.0)
            if command.qos == 1 and command.packet_id is not None:
                await appliance.send_puback(command.packet_id)
            decoded = protocol.parse_device_message(command.payload)
            assert decoded["cmd"] == protocol.CMD_SET
            assert decoded["data"]["state"][protocol.KEY_TARGET_TEMP] == 72

            # A malformed hvac_mode payload is ignored (no injected command, no crash).
            await harness.mqtt_client.publish(hvac_mode_topic, payload="not-a-real-mode")
            await asyncio.sleep(0.2)
            await appliance.assert_silent(timeout=0.3)
            assert harness.bridge_task is not None and not harness.bridge_task.done()
        finally:
            await appliance.close()


def test_malformed_and_valid_commands(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    run(_scenario_temperature_and_hvac_mode_commands(tmp_path, tls_cert_pair))


async def _scenario_device_disconnect_sets_availability_offline(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    async with _harness(tmp_path, tls_cert_pair) as harness:
        appliance = await harness.connect_and_answer()
        availability_topic = f"{harness.base_topic}/availability"
        await harness.observer.wait_for(availability_topic, predicate=lambda p: p == b"online", timeout=5.0)

        await appliance.close()

        await harness.observer.wait_for(availability_topic, predicate=lambda p: p == b"offline", timeout=5.0)


def test_device_disconnect_sets_availability_offline(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    run(_scenario_device_disconnect_sets_availability_offline(tmp_path, tls_cert_pair))


async def _scenario_broker_restart_republishes_everything(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    async with _harness(tmp_path, tls_cert_pair) as harness:
        appliance = await harness.connect_and_answer()
        try:
            await harness.observer.wait_for(harness.discovery_topic, timeout=5.0)
            await harness.observer.wait_for(f"{harness.base_topic}/state", timeout=5.0)

            assert harness.mosquitto is not None
            await harness.mosquitto.restart()

            # persistence=false means the new broker process starts with an
            # empty retained-message store, so only seeing these again proves
            # the bridge itself noticed the reconnect and republished them --
            # not that mosquitto remembered them across the restart.
            async with aiomqtt.Client(hostname="127.0.0.1", port=harness.mosquitto.port, identifier="post-restart-observer") as post_client:
                post_observer = Observer(post_client)
                await post_observer.start()
                try:
                    discovery_payload = await post_observer.wait_for(harness.discovery_topic, timeout=20.0)
                    assert json.loads(discovery_payload)["dev"]["mdl"] == _FAKE_INFO[protocol.INFO_PRODUCT]
                    await post_observer.wait_for(
                        harness.bridge_availability_topic, predicate=lambda p: p == b"online", timeout=5.0
                    )
                    await post_observer.wait_for(
                        f"{harness.base_topic}/availability", predicate=lambda p: p == b"online", timeout=5.0
                    )
                    state_payload = await post_observer.wait_for(f"{harness.base_topic}/state", timeout=5.0)
                    assert json.loads(state_payload)["hvac_mode"] == "cool"
                finally:
                    await post_observer.stop()
        finally:
            await appliance.close()


def test_broker_restart_republishes_discovery_and_state(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    run(_scenario_broker_restart_republishes_everything(tmp_path, tls_cert_pair))


async def _scenario_cancel_bridge_publishes_offline(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    async with _harness(tmp_path, tls_cert_pair) as harness:
        appliance = await harness.connect_and_answer()
        try:
            await harness.observer.wait_for(
                harness.bridge_availability_topic, predicate=lambda p: p == b"online", timeout=5.0
            )

            assert harness.bridge_task is not None
            harness.bridge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await harness.bridge_task

            await harness.observer.wait_for(
                harness.bridge_availability_topic, predicate=lambda p: p == b"offline", timeout=5.0
            )
        finally:
            await appliance.close()


def test_cancelling_bridge_publishes_bridge_offline(tmp_path: Path, tls_cert_pair: tuple[Path, Path]) -> None:
    run(_scenario_cancel_bridge_publishes_offline(tmp_path, tls_cert_pair))


# --------------------------------------------------------------------------- #
# Cheap simulation of an abrupt-disconnect Last Will delivery.
#
# This does not run the full Bridge: it proves, directly against a real
# mosquitto broker, that a client which registers a Will (exactly the way
# HomeAssistantMqtt._make_client does) and then vanishes without sending an
# MQTT DISCONNECT gets that Will delivered -- the mechanism the bridge relies
# on for "the add-on died uncleanly" detection, exercised without needing to
# reach into aiomqtt/paho internals to kill the real bridge's own socket.
# --------------------------------------------------------------------------- #
async def _simulate_will_delivery(mosquitto_port: int, topic: str, payload: bytes) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", mosquitto_port)
    try:
        connect_frame = mqtt_codec.encode_connect(
            "will-test-client", will_topic=topic, will_message=payload, will_qos=1, will_retain=True
        )
        writer.write(connect_frame)
        await writer.drain()
        connack_bytes = await asyncio.wait_for(reader.read(4), timeout=2.0)
        packets, _ = mqtt_codec.decode_packets(connack_bytes)
        assert packets and isinstance(packets[0], mqtt_codec.ConnAckPacket)
        assert packets[0].return_code == mqtt_codec.CONNACK_ACCEPTED
    finally:
        # Abort the TCP connection with no MQTT DISCONNECT packet -- the
        # broker sees an ungraceful loss of a client and publishes its Will,
        # regardless of whether the underlying TCP close is graceful or not.
        writer.close()


async def _scenario_abrupt_disconnect_delivers_last_will(tmp_path: Path) -> None:
    mosquitto = MosquittoBroker(_MOSQUITTO_BINARY, tmp_path)
    await mosquitto.start()
    try:
        will_topic = "zafro/_bridge/availability"
        async with aiomqtt.Client(hostname="127.0.0.1", port=mosquitto.port, identifier="will-observer") as client:
            observer = Observer(client)
            await observer.start()
            try:
                await _simulate_will_delivery(mosquitto.port, will_topic, b"offline")
                await observer.wait_for(will_topic, predicate=lambda p: p == b"offline", timeout=5.0)
            finally:
                await observer.stop()
    finally:
        await mosquitto.stop()


def test_abrupt_disconnect_delivers_last_will(tmp_path: Path) -> None:
    run(_scenario_abrupt_disconnect_delivers_last_will(tmp_path))
