"""Device-level integration tests: a fake appliance against a real DeviceServer.

These drive ``DeviceServer``/``DeviceSession`` end-to-end over real TLS
sockets and WebSocket framing (via ``tests/fake_appliance.py``), rather than
unit-testing the pure protocol/codec modules. Two families:

* **Local-mode** tests run a real ``DeviceServer`` with relaying disabled and
  a genuine fake-appliance client: handshake, publish routing, the allow-list
  and identity checks, the connect/keepalive watchdogs, frame-size and
  malformed-input hardening, the session cap, injected-publish packet ids,
  and callback isolation/exactly-once semantics.
* **Relay-mode** tests run with relaying enabled but replace
  ``device_session.UpstreamConnection`` with an in-memory fake (no network),
  covering verbatim forwarding, mid-handshake upstream drop, a cloud CONNACK
  refusal, and the auto-recovery probe.

Every test is wrapped in ``asyncio.wait_for`` (via ``_run``) so a hang fails
fast instead of stalling the suite; per-recv timeouts inside each scenario are
kept short for the same reason. No pytest plugins (no pytest-asyncio) --
async scenarios are plain coroutines driven by ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

aiohttp = pytest.importorskip("aiohttp")

from fake_appliance import FakeAppliance  # noqa: E402
from zafro_bridge.config import Settings  # noqa: E402
from zafro_bridge.device_server import DeviceServer  # noqa: E402
from zafro_bridge.device_session import DeviceSession  # noqa: E402

from zafro_bridge import device_session as device_session_module  # noqa: E402
from zafro_bridge import mqtt_codec, protocol  # noqa: E402

SERIAL = "TESTSERIAL000001"
OTHER_SERIAL = "TESTSERIAL000002"

# Overall wall-clock budget for one test scenario. Generous relative to the
# short per-step timeouts used inside each scenario, so a genuine hang (not
# just one slow step) still fails in well under pytest's own default.
_SCENARIO_TIMEOUT_SECONDS = 10.0


def run(coroutine: Awaitable[None]) -> None:
    """Drive one async test scenario to completion, bounded by a hard timeout."""
    asyncio.run(asyncio.wait_for(coroutine, timeout=_SCENARIO_TIMEOUT_SECONDS))


def make_settings(*, port: int, cert_path: Path, key_path: Path, **overrides: Any) -> Settings:
    base = Settings(
        listen_host="127.0.0.1",
        listen_port=port,
        tls_certificate_path=cert_path,
        tls_private_key_path=key_path,
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
        allowed_serials=frozenset(),
        max_device_sessions=16,
        relay_retry_seconds=300.0,
        device_connect_timeout_seconds=5.0,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


class Recorder:
    """Records every on_connected/on_publish/on_disconnected callback invocation."""

    def __init__(self, *, on_connected_hook: Callable[[DeviceSession, str], Awaitable[None]] | None = None) -> None:
        self.connected: list[tuple[DeviceSession, str]] = []
        self.published: list[tuple[str, str, bytes]] = []
        self.disconnected: list[tuple[DeviceSession, str]] = []
        self._connected_event = asyncio.Event()
        self._on_connected_hook = on_connected_hook

    async def on_connected(self, session: DeviceSession, serial: str) -> None:
        # Record before invoking the (possibly-raising) hook: a test proving
        # that a buggy on_connected callback doesn't end the session still
        # needs to observe that the callback *ran* despite raising.
        self.connected.append((session, serial))
        self._connected_event.set()
        if self._on_connected_hook is not None:
            await self._on_connected_hook(session, serial)

    async def on_publish(self, serial: str, topic: str, payload: bytes) -> None:
        self.published.append((serial, topic, payload))

    async def on_disconnected(self, session: DeviceSession, serial: str) -> None:
        self.disconnected.append((session, serial))

    async def wait_connected(self, timeout: float = 2.0) -> tuple[DeviceSession, str]:
        await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        return self.connected[-1]


class _running_server:
    """Async context manager: start a DeviceServer, guarantee it stops."""

    def __init__(self, settings: Settings, recorder: Recorder) -> None:
        self._settings = settings
        self._recorder = recorder
        self.server: DeviceServer | None = None

    async def __aenter__(self) -> DeviceServer:
        self.server = DeviceServer(self._settings, self._recorder.on_connected, self._recorder.on_publish, self._recorder.on_disconnected)
        await self.server.start()
        return self.server

    async def __aexit__(self, *exc_info: object) -> None:
        assert self.server is not None
        await self.server.stop()


def server_url(settings: Settings) -> str:
    return f"wss://{settings.listen_host}:{settings.listen_port}{protocol.WEBSOCKET_PATH}"


async def _connected_appliance(settings: Settings) -> FakeAppliance:
    appliance = FakeAppliance(server_url(settings))
    await appliance.connect()
    return appliance


# =============================================================================
# Local-mode tests (relay disabled; DeviceSession answers as its own broker)
# =============================================================================
async def _scenario_local_handshake(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            connack = await appliance.handshake(SERIAL)
            assert connack.return_code == mqtt_codec.CONNACK_ACCEPTED
            session, serial = await recorder.wait_connected()
            assert serial == SERIAL
            assert isinstance(session, DeviceSession)

            await appliance.send_pingreq()
            await appliance.expect(mqtt_codec.PingRespPacket)

            unsubscribe_id = await appliance.unsubscribe([protocol.TOPIC_TIME_SYNC])
            acked_id = await appliance.expect_unsuback()
            assert acked_id == unsubscribe_id
        finally:
            await appliance.close()


def test_local_handshake(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_local_handshake(*tls_cert_pair, free_tcp_port))


async def _scenario_publish_reaches_on_publish(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.handshake(SERIAL)
            await recorder.wait_connected()

            topic = protocol.command_reply_topic(SERIAL)
            payload = b'{"cmd":3,"data":{"state":{"poweron":true}}}'
            packet_id = await appliance.publish(topic, payload, qos=1)

            puback = await appliance.expect(mqtt_codec.PubAckPacket)
            assert puback.packet_id == packet_id

            assert recorder.published == [(SERIAL, topic, payload)]
        finally:
            await appliance.close()


def test_publish_reaches_on_publish_with_session_serial(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_publish_reaches_on_publish(*tls_cert_pair, free_tcp_port))


async def _scenario_spoofed_topic_publish_dropped(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.handshake(SERIAL)
            await recorder.wait_connected()

            # Connected as SERIAL but publishing on a topic naming a different
            # device's serial: must be dropped entirely (no on_publish, no PUBACK).
            spoofed_topic = protocol.command_reply_topic(OTHER_SERIAL)
            await appliance.publish(spoofed_topic, b'{"cmd":3}', qos=1)
            await appliance.assert_silent()

            assert recorder.published == []
        finally:
            await appliance.close()


def test_spoofed_topic_publish_is_dropped(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_spoofed_topic_publish_dropped(*tls_cert_pair, free_tcp_port))


async def _scenario_allowlist_rejection(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path, allowed_serials=frozenset({OTHER_SERIAL}))
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.send_connect(SERIAL)
            connack = await appliance.expect(mqtt_codec.ConnAckPacket)
            assert connack.return_code == mqtt_codec.CONNACK_NOT_AUTHORIZED
            await appliance.wait_closed()
            assert recorder.connected == []
        finally:
            await appliance.close()


def test_allowlist_rejection_closes_with_connack_5(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_allowlist_rejection(*tls_cert_pair, free_tcp_port))


async def _scenario_invalid_client_id_rejected(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.send_connect_raw("not-a-device-client-id")
            connack = await appliance.expect(mqtt_codec.ConnAckPacket)
            assert connack.return_code == mqtt_codec.CONNACK_IDENTIFIER_REJECTED
            await appliance.wait_closed()
            assert recorder.connected == []
        finally:
            await appliance.close()


def test_invalid_client_id_closes_with_connack_2(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_invalid_client_id_rejected(*tls_cert_pair, free_tcp_port))


async def _scenario_packet_before_connect_closes(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.subscribe([(protocol.TOPIC_TIME_SYNC, 1)])
            await appliance.wait_closed()
            assert recorder.connected == []
        finally:
            await appliance.close()


def test_packet_before_connect_closes_session(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_packet_before_connect_closes(*tls_cert_pair, free_tcp_port))


async def _scenario_second_connect_closes(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.handshake(SERIAL)
            await recorder.wait_connected()

            await appliance.send_connect(SERIAL)
            await appliance.wait_closed()
        finally:
            await appliance.close()


def test_second_connect_closes_session(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_second_connect_closes(*tls_cert_pair, free_tcp_port))


async def _scenario_connect_timeout_closes(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path, device_connect_timeout_seconds=0.3)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            # Deliberately send nothing; the watchdog should close us for
            # never presenting a CONNECT within device_connect_timeout_seconds.
            await appliance.wait_closed(timeout=2.0)
            assert recorder.connected == []
        finally:
            await appliance.close()


def test_connect_timeout_closes_session(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_connect_timeout_closes(*tls_cert_pair, free_tcp_port))


async def _scenario_keepalive_watchdog_closes_silent_session(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.handshake(SERIAL, keepalive_seconds=1)
            await recorder.wait_connected()

            # 1.5x a 1s keepalive is 1.5s; go quiet and wait comfortably past it.
            await appliance.wait_closed(timeout=3.0)
        finally:
            await appliance.close()


def test_keepalive_watchdog_closes_silent_session(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_keepalive_watchdog_closes_silent_session(*tls_cert_pair, free_tcp_port))


async def _scenario_oversize_and_malformed_frames_close_session(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        # First peer: a PUBLISH-shaped fixed header declaring a remaining
        # length far above mqtt_codec.MAX_PACKET_BYTES. decode_packets() must
        # reject this the instant the header is parsed, so no body is needed.
        oversize_appliance = await _connected_appliance(settings)
        try:
            await oversize_appliance.handshake(SERIAL)
            await recorder.wait_connected()
            oversize_header = bytes([0x30]) + mqtt_codec.encode_remaining_length(mqtt_codec.MAX_PACKET_BYTES + 1)
            await oversize_appliance.send_raw(oversize_header)
            await oversize_appliance.wait_closed()
        finally:
            await oversize_appliance.close()

        # Second peer: a remaining-length field with a 5th continuation byte,
        # which is unconditionally invalid MQTT regardless of declared size.
        malformed_appliance = await _connected_appliance(settings)
        try:
            await malformed_appliance.handshake(OTHER_SERIAL)
            malformed_header = bytes([0x10, 0xFF, 0xFF, 0xFF, 0xFF, 0x01])
            await malformed_appliance.send_raw(malformed_header)
            await malformed_appliance.wait_closed()
        finally:
            await malformed_appliance.close()

        # The server must still be healthy: a fresh, well-behaved connection
        # completes a normal handshake afterwards.
        recovered_appliance = await _connected_appliance(settings)
        try:
            connack = await recovered_appliance.handshake("TESTSERIAL000003")
            assert connack.return_code == mqtt_codec.CONNACK_ACCEPTED
        finally:
            await recovered_appliance.close()

        assert len(recorder.connected) == 3


def test_oversize_and_malformed_frames_close_session_and_server_recovers(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_oversize_and_malformed_frames_close_session(*tls_cert_pair, free_tcp_port))


async def _scenario_session_cap_returns_503(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path, max_device_sessions=1)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        first_appliance = await _connected_appliance(settings)
        try:
            overflow_session = aiohttp.ClientSession()
            try:
                with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
                    await overflow_session.ws_connect(
                        server_url(settings), protocols=(protocol.WEBSOCKET_SUBPROTOCOL,), compress=0, ssl=False
                    )
                assert excinfo.value.status == 503
            finally:
                await overflow_session.close()
        finally:
            await first_appliance.close()


def test_session_cap_returns_503(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_session_cap_returns_503(*tls_cert_pair, free_tcp_port))


async def _scenario_inject_publish_uses_high_packet_ids(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.handshake(SERIAL)
            session, _serial = await recorder.wait_connected()

            command_topic = protocol.command_request_topic(SERIAL)
            await session.inject_publish(command_topic, protocol.build_state_request())

            injected = await appliance.expect(mqtt_codec.PublishPacket)
            assert injected.topic == command_topic
            assert injected.packet_id is not None
            assert injected.packet_id >= 60_000

            # The device's PUBACK for an injected id is swallowed internally:
            # it must not surface as a device publish nor produce any reply.
            await appliance.send_puback(injected.packet_id)
            await appliance.assert_silent()
            assert recorder.published == []

            # The session must still be fully functional afterwards.
            reply_topic = protocol.command_reply_topic(SERIAL)
            await appliance.publish(reply_topic, b'{"cmd":4}', qos=1)
            await appliance.expect(mqtt_codec.PubAckPacket)
            assert recorder.published == [(SERIAL, reply_topic, b'{"cmd":4}')]
        finally:
            await appliance.close()


def test_inject_publish_uses_high_packet_ids_and_puback_not_leaked(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_inject_publish_uses_high_packet_ids(*tls_cert_pair, free_tcp_port))


async def _scenario_on_connected_exception_does_not_end_session(cert_path: Path, key_path: Path, port: int) -> None:
    async def _raising_hook(_session: DeviceSession, _serial: str) -> None:
        raise RuntimeError("boom: simulated bug in the bridge's on_connected handler")

    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder(on_connected_hook=_raising_hook)
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            connack = await appliance.handshake(SERIAL)
            assert connack.return_code == mqtt_codec.CONNACK_ACCEPTED
            await recorder.wait_connected()  # the callback ran (and raised) but was recorded

            # The session must still be alive and answering normally.
            topic = protocol.command_reply_topic(SERIAL)
            await appliance.publish(topic, b"{}", qos=1)
            await appliance.expect(mqtt_codec.PubAckPacket)
            assert recorder.published == [(SERIAL, topic, b"{}")]
        finally:
            await appliance.close()
        await asyncio.sleep(0.05)
        assert len(recorder.disconnected) == 1


def test_on_connected_exception_does_not_end_session(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_on_connected_exception_does_not_end_session(*tls_cert_pair, free_tcp_port))


async def _scenario_on_disconnected_fires_exactly_once(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        await appliance.handshake(SERIAL)
        await recorder.wait_connected()

        await appliance.send_disconnect()
        await appliance.wait_closed()
        await appliance.close()

        await asyncio.sleep(0.05)
        assert len(recorder.disconnected) == 1


def test_on_disconnected_fires_exactly_once(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_on_disconnected_fires_exactly_once(*tls_cert_pair, free_tcp_port))


# =============================================================================
# Relay-mode tests: UpstreamConnection replaced with an in-memory fake so
# these need no network access at all.
# =============================================================================
class FakeUpstream:
    """Stand-in for cloud_relay.UpstreamConnection, driven by the test directly."""

    def __init__(self, cloud_host: str, connect_timeout_seconds: float, *, connect_error: Exception | None = None) -> None:
        self.cloud_host = cloud_host
        self.connect_timeout_seconds = connect_timeout_seconds
        self._connect_error = connect_error
        self._open = False
        self._closed = False
        self.sent_frames: list[bytes] = []
        # Frames "from the cloud" queue here rather than being dispatched
        # directly to the on_frame callback: receive_loop() below dequeues
        # and calls on_frame from *inside* its own coroutine, exactly like the
        # real UpstreamConnection does, so a callback exception (in
        # particular device_session._RelayAbandoned, deliberately raised to
        # unwind a CONNACK refusal) propagates through receive_loop() and is
        # caught by DeviceSession._pump_upstream()'s own except clause --
        # calling the callback directly from push_raw() would instead let it
        # escape into whichever task called push_raw (the test).
        self._incoming_frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.receiving_started = asyncio.Event()

    @property
    def is_open(self) -> bool:
        return self._open and not self._closed

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self._open = True

    async def send(self, raw_frame: bytes) -> None:
        if not self.is_open:
            raise ConnectionError("fake upstream is not open")
        self.sent_frames.append(raw_frame)

    async def receive_loop(self, on_frame: Callable[[bytes], Awaitable[None]]) -> None:
        self.receiving_started.set()
        while True:
            frame = await self._incoming_frames.get()
            if frame is None:  # closed/dropped sentinel
                return
            await on_frame(frame)

    async def close(self) -> None:
        self._closed = True
        self._open = False
        self._incoming_frames.put_nowait(None)

    async def push_raw(self, frame: bytes) -> None:
        """Simulate a frame arriving from the cloud."""
        await self._incoming_frames.put(frame)

    def simulate_drop(self) -> None:
        """Simulate the upstream link vanishing without a proper close()."""
        self._closed = True
        self._incoming_frames.put_nowait(None)


async def _wait_for_upstream_instance(harness: UpstreamHarness, index: int = 0, timeout: float = 2.0) -> FakeUpstream:
    """Poll until the harness has created its (index+1)-th fake upstream.

    Creating a DeviceSession's upstream happens on a task the test does not
    control directly (inside the server's own connection-handling coroutine),
    so this polls rather than assuming any fixed delay is enough.
    """

    async def _poll() -> FakeUpstream:
        while len(harness.instances) <= index:
            await asyncio.sleep(0.01)
        return harness.instances[index]

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Poll ``predicate`` until it is true, bounded by ``timeout``."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)


class UpstreamHarness:
    """Monkeypatched in place of device_session.UpstreamConnection.

    Each call consumes one queued connect outcome (None = succeeds); once the
    queue is empty, every further instance connects successfully. Every
    FakeUpstream created is kept so the test can reach into it.
    """

    def __init__(self, connect_outcomes: list[Exception | None] | None = None) -> None:
        self._connect_outcomes = list(connect_outcomes or [])
        self.instances: list[FakeUpstream] = []

    def __call__(self, cloud_host: str, connect_timeout_seconds: float) -> FakeUpstream:
        outcome = self._connect_outcomes.pop(0) if self._connect_outcomes else None
        fake = FakeUpstream(cloud_host, connect_timeout_seconds, connect_error=outcome)
        self.instances.append(fake)
        return fake


async def _scenario_relay_forwards_verbatim(cert_path: Path, key_path: Path, port: int, harness: UpstreamHarness) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path, cloud_relay_enabled=True)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.send_connect(SERIAL)
            fake_upstream = await _wait_for_upstream_instance(harness)
            await fake_upstream.receiving_started.wait()

            def _sent_connect() -> bool:
                return any(isinstance(mqtt_codec.decode_packets(f)[0][0], mqtt_codec.ConnectPacket) for f in fake_upstream.sent_frames)

            await _wait_until(_sent_connect)

            await fake_upstream.push_raw(mqtt_codec.encode_connack(mqtt_codec.CONNACK_ACCEPTED))
            connack = await appliance.expect(mqtt_codec.ConnAckPacket)
            assert connack.return_code == mqtt_codec.CONNACK_ACCEPTED
            await recorder.wait_connected()

            subscribe_id = await appliance.subscribe([(protocol.command_request_topic(SERIAL), 1)])

            def _sent_subscribe() -> bool:
                return any(
                    isinstance((decoded := mqtt_codec.decode_packets(f)[0][0]), mqtt_codec.SubscribePacket)
                    and decoded.packet_id == subscribe_id
                    for f in fake_upstream.sent_frames
                )

            await _wait_until(_sent_subscribe)

            await fake_upstream.push_raw(mqtt_codec.encode_suback(subscribe_id, [1]))
            suback = await appliance.expect(mqtt_codec.SubAckPacket)
            assert suback.packet_id == subscribe_id

            # A device publish is both observed locally (for HA) and forwarded
            # upstream verbatim so the vendor app keeps seeing live state.
            topic = protocol.command_reply_topic(SERIAL)
            payload = b'{"cmd":3,"data":{}}'
            await appliance.publish(topic, payload, qos=0)

            def _forwarded_publish() -> bool:
                return any(
                    f[0] >> 4 == mqtt_codec.PUBLISH and mqtt_codec.decode_packets(f)[0][0].topic == topic
                    for f in fake_upstream.sent_frames
                )

            await _wait_until(_forwarded_publish)
            assert recorder.published == [(SERIAL, topic, payload)]
        finally:
            await appliance.close()


def test_relay_forwards_frames_verbatim(tls_cert_pair: tuple[Path, Path], free_tcp_port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = UpstreamHarness()
    monkeypatch.setattr(device_session_module, "UpstreamConnection", harness)
    run(_scenario_relay_forwards_verbatim(*tls_cert_pair, free_tcp_port, harness))


async def _scenario_relay_upstream_drop_answers_pending_locally(
    cert_path: Path, key_path: Path, port: int, harness: UpstreamHarness
) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path, cloud_relay_enabled=True)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.send_connect(SERIAL)
            fake_upstream = await _wait_for_upstream_instance(harness)
            await fake_upstream.receiving_started.wait()
            await fake_upstream.push_raw(mqtt_codec.encode_connack(mqtt_codec.CONNACK_ACCEPTED))
            await appliance.expect(mqtt_codec.ConnAckPacket)

            subscribe_id = await appliance.subscribe([(protocol.command_request_topic(SERIAL), 1)])
            # Let the SUBSCRIBE actually reach the (fake) upstream and be
            # recorded as pending before the link vanishes.
            await _wait_until(lambda: any(f[0] >> 4 == mqtt_codec.SUBSCRIBE for f in fake_upstream.sent_frames))

            # The cloud link vanishes before answering the SUBSCRIBE.
            fake_upstream.simulate_drop()

            # The bridge must fall back to local mode and answer the
            # still-pending SUBSCRIBE itself rather than leaving the device hanging.
            suback = await appliance.expect(mqtt_codec.SubAckPacket, timeout=3.0)
            assert suback.packet_id == subscribe_id
        finally:
            await appliance.close()


def test_relay_upstream_drop_mid_handshake_answers_pending_locally(
    tls_cert_pair: tuple[Path, Path], free_tcp_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = UpstreamHarness()
    monkeypatch.setattr(device_session_module, "UpstreamConnection", harness)
    run(_scenario_relay_upstream_drop_answers_pending_locally(*tls_cert_pair, free_tcp_port, harness))


async def _scenario_relay_connack_refusal_falls_back_locally(
    cert_path: Path, key_path: Path, port: int, harness: UpstreamHarness
) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path, cloud_relay_enabled=True)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.send_connect(SERIAL)
            fake_upstream = await _wait_for_upstream_instance(harness)
            await fake_upstream.receiving_started.wait()

            # The cloud actively refuses this appliance.
            await fake_upstream.push_raw(mqtt_codec.encode_connack(mqtt_codec.CONNACK_NOT_AUTHORIZED))

            # The device must see a locally-synthesized *accepted* CONNACK,
            # never the cloud's refusal.
            connack = await appliance.expect(mqtt_codec.ConnAckPacket, timeout=3.0)
            assert connack.return_code == mqtt_codec.CONNACK_ACCEPTED
        finally:
            await appliance.close()


def test_relay_connack_refusal_is_not_forwarded(
    tls_cert_pair: tuple[Path, Path], free_tcp_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = UpstreamHarness()
    monkeypatch.setattr(device_session_module, "UpstreamConnection", harness)
    run(_scenario_relay_connack_refusal_falls_back_locally(*tls_cert_pair, free_tcp_port, harness))


async def _scenario_relay_auto_recovery_closes_device_socket(
    cert_path: Path, key_path: Path, port: int, harness: UpstreamHarness
) -> None:
    settings = make_settings(
        port=port,
        cert_path=cert_path,
        key_path=key_path,
        cloud_relay_enabled=True,
        relay_retry_seconds=0.05,
    )
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            # The initial connect fails, so the session runs entirely locally...
            connack = await appliance.handshake(SERIAL)
            assert connack.return_code == mqtt_codec.CONNACK_ACCEPTED
            await recorder.wait_connected()
            assert len(harness.instances) == 1  # the failed initial attempt

            # ...until the retry probe succeeds, at which point the bridge
            # closes the appliance connection so it reconnects fresh through
            # the relay (a brief offline/online blip in Home Assistant).
            await appliance.wait_closed(timeout=3.0)
            assert len(harness.instances) >= 2  # the initial attempt plus at least one probe
        finally:
            await appliance.close()


def test_relay_auto_recovery_probe_success_closes_device_socket(
    tls_cert_pair: tuple[Path, Path], free_tcp_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = UpstreamHarness(connect_outcomes=[ConnectionRefusedError("cloud unreachable")])
    monkeypatch.setattr(device_session_module, "UpstreamConnection", harness)
    run(_scenario_relay_auto_recovery_closes_device_socket(*tls_cert_pair, free_tcp_port, harness))


# =============================================================================
# Regression tests for defects found in review
# =============================================================================
async def _scenario_connect_deadline_not_extended_by_trickled_frames(cert_path: Path, key_path: Path, port: int) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path, device_connect_timeout_seconds=0.8)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            # Keep the socket busy without ever completing a CONNECT: a lone
            # CONNECT header byte, then a steady trickle of text frames. None
            # of it may push the pre-CONNECT deadline back.
            await appliance.send_raw(bytes([mqtt_codec.CONNECT << 4]))
            while not appliance.closed and loop.time() - started < 4.0:
                try:
                    await appliance._ws.send_str("still here")  # type: ignore[union-attr]
                except (ConnectionError, RuntimeError):
                    break
                await asyncio.sleep(0.2)
            await appliance.wait_closed(timeout=1.0)
            assert loop.time() - started < 3.0
            assert recorder.connected == []
        finally:
            await appliance.close()


def test_connect_deadline_is_not_extended_by_trickled_frames(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_connect_deadline_not_extended_by_trickled_frames(*tls_cert_pair, free_tcp_port))


async def _scenario_relay_split_upstream_packet_is_reassembled(
    cert_path: Path, key_path: Path, port: int, harness: UpstreamHarness
) -> None:
    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path, cloud_relay_enabled=True)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        appliance = await _connected_appliance(settings)
        try:
            await appliance.send_connect(SERIAL)
            fake_upstream = await _wait_for_upstream_instance(harness)
            await fake_upstream.receiving_started.wait()
            await _wait_until(lambda: len(fake_upstream.sent_frames) >= 1)  # CONNECT recorded as pending

            # The cloud's CONNACK arrives split across two WebSocket frames.
            connack = mqtt_codec.encode_connack(mqtt_codec.CONNACK_ACCEPTED)
            await fake_upstream.push_raw(connack[:1])
            await fake_upstream.push_raw(connack[1:])
            received = await appliance.expect(mqtt_codec.ConnAckPacket)
            assert received.return_code == mqtt_codec.CONNACK_ACCEPTED

            # The CONNECT is answered, so losing the link must not produce a
            # second, locally synthesized CONNACK.
            fake_upstream.simulate_drop()
            await appliance.assert_silent(timeout=0.5)
        finally:
            await appliance.close()


def test_relay_split_upstream_packet_is_reassembled_and_not_answered_twice(
    tls_cert_pair: tuple[Path, Path], free_tcp_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = UpstreamHarness()
    monkeypatch.setattr(device_session_module, "UpstreamConnection", harness)
    run(_scenario_relay_split_upstream_packet_is_reassembled(*tls_cert_pair, free_tcp_port, harness))


class _YieldingWebSocket:
    """Minimal WebSocketResponse stand-in whose sends yield to the event loop."""

    def __init__(self) -> None:
        self.sent_frames: list[bytes] = []
        self.closed = False
        self.before_first_send_returns: Callable[[], Awaitable[None]] | None = None

    async def send_bytes(self, frame: bytes) -> None:
        self.sent_frames.append(frame)
        if self.before_first_send_returns is not None:
            hook, self.before_first_send_returns = self.before_first_send_returns, None
            await hook()
        await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True


async def _scenario_concurrent_fallbacks_answer_each_frame_once(cert_path: Path, key_path: Path) -> None:
    settings = make_settings(port=0, cert_path=cert_path, key_path=key_path, cloud_relay_enabled=True)
    recorder = Recorder()
    websocket = _YieldingWebSocket()
    session = DeviceSession(websocket, settings, recorder.on_connected, recorder.on_publish, recorder.on_disconnected)  # type: ignore[arg-type]
    session.serial_number = SERIAL
    session._connect_seen = True
    upstream = FakeUpstream(protocol.DEFAULT_CLOUD_HOST, 1.0)
    await upstream.connect()
    session._upstream = upstream  # type: ignore[assignment]

    def decoded(frame: bytes) -> mqtt_codec.Packet:
        return mqtt_codec.decode_packets(frame)[0][0]

    session._record_pending_ack(decoded(mqtt_codec.encode_connect(f"dev_{SERIAL}")))
    session._record_pending_ack(decoded(mqtt_codec.encode_subscribe(1, [("a", 1)])))
    session._record_pending_ack(decoded(mqtt_codec.encode_subscribe(2, [("b", 1)])))
    late_subscribe = decoded(mqtt_codec.encode_subscribe(3, [("c", 1)]))

    race_tasks: list[asyncio.Task[None]] = []

    async def race_in_while_answering() -> None:
        # A second upstream failure and fresh device traffic arrive (on their
        # own tasks, as they would for real) while the first fallback is still
        # sending its synthesized answers.
        async def second_failure_and_late_traffic() -> None:
            await session._fallback_to_local("second failure")
            await session._handle_device_packet(late_subscribe)

        race_tasks.append(asyncio.create_task(second_failure_and_late_traffic()))

    websocket.before_first_send_returns = race_in_while_answering
    try:
        await session._fallback_to_local("first failure")
        await asyncio.gather(*race_tasks)
        packets = [decoded(frame) for frame in websocket.sent_frames]
        assert sum(isinstance(packet, mqtt_codec.ConnAckPacket) for packet in packets) == 1
        assert sorted(packet.packet_id for packet in packets if isinstance(packet, mqtt_codec.SubAckPacket)) == [1, 2, 3]
        assert upstream.sent_frames == []  # nothing leaked upstream once detached
    finally:
        await session.close()


def test_concurrent_relay_fallbacks_answer_each_pending_frame_exactly_once(tls_cert_pair: tuple[Path, Path]) -> None:
    run(_scenario_concurrent_fallbacks_answer_each_frame_once(*tls_cert_pair))


# =============================================================================
# Container health check
# =============================================================================
async def _scenario_health_check_endpoint(cert_path: Path, key_path: Path, port: int) -> None:
    import shutil

    settings = make_settings(port=port, cert_path=cert_path, key_path=key_path)
    recorder = Recorder()
    async with _running_server(settings, recorder):
        base_url = f"https://{settings.listen_host}:{settings.listen_port}"
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{base_url}/healthz", ssl=False) as response:
                assert response.status == 200
                assert await response.text() == "ok"
            # Everything else still mimics the vendor cloud's 404.
            async with client.get(f"{base_url}/anything-else", ssl=False) as response:
                assert response.status == 404
                assert response.reason == protocol.CLOUD_NOT_FOUND_REASON

        # The exact probe the Dockerfile HEALTHCHECK runs, when curl is available.
        curl_path = shutil.which("curl")
        if curl_path is not None:
            process = await asyncio.create_subprocess_exec(
                curl_path, "--silent", "--insecure", "--fail", "--max-time", "5",
                "--output", "/dev/null", f"{base_url}/healthz",
            )
            assert await process.wait() == 0


def test_health_check_endpoint_answers_ok(tls_cert_pair: tuple[Path, Path], free_tcp_port: int) -> None:
    run(_scenario_health_check_endpoint(*tls_cert_pair, free_tcp_port))
