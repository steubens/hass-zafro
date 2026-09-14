"""Reusable asyncio simulator of the Zafro/Rowan appliance.

Speaks the same wire protocol the real unit does -- MQTT 3.1 ("MQIsdp") framed
inside a WebSocket ("mqtt" subprotocol, no compression) -- using aiohttp's
WebSocket client and mqtt_codec's client-side encoders. It is deliberately
*dumb*: it does not know about Home Assistant or the bridge's internals, only
about the bytes a real appliance would send and receive, so it can drive
``DeviceServer``/``DeviceSession`` exactly like hardware would.

Two uses:

1. Imported by ``tests/test_integration_device.py`` for fine-grained,
   packet-at-a-time control of a simulated device session.
2. Run directly as a script against a real (or locally running) add-on for
   manual, human-driven testing:

       python tests/fake_appliance.py --host 192.0.2.10 --port 8443 \\
           --serial TESTSERIAL000001

   Always use an obviously-fake serial for manual runs (never a real
   appliance's) so captured traffic and HA entities are never mistaken for a
   genuine device.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from itertools import count
from typing import Any

import aiohttp

from zafro_bridge import mqtt_codec, protocol

# A clearly-fake default serial for manual runs; never a real unit's.
DEFAULT_FAKE_SERIAL = "TESTSERIAL000001"

# How long the reader loop waits for a WebSocket message before checking
# whether it has been asked to stop; keeps close() from hanging forever if
# the peer never sends another frame.
_READER_POLL_SECONDS = 0.5


class FakeApplianceError(Exception):
    """Raised for protocol-level misuse of the simulator (e.g. not connected)."""


class FakeAppliance:
    """One simulated appliance WebSocket/MQTT client session."""

    def __init__(self, url: str, *, verify_tls: bool = False) -> None:
        self._url = url
        self._verify_tls = verify_tls
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._receive_buffer = b""
        self._incoming: asyncio.Queue[mqtt_codec.Packet] = asyncio.Queue()
        self._packet_id_counter = count(1)
        self._closed_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    @property
    def closed(self) -> bool:
        return self._closed_event.is_set()

    async def connect(self) -> None:
        """Open the WebSocket transport (no MQTT CONNECT sent yet)."""
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                self._url,
                protocols=(protocol.WEBSOCKET_SUBPROTOCOL,),
                compress=0,
                # The real appliance never validates the bridge's certificate;
                # mirror that here rather than requiring a trusted chain in tests.
                ssl=False if not self._verify_tls else None,
            )
        except Exception:
            await self._session.close()
            self._session = None
            raise
        self._reader_task = asyncio.create_task(self._read_loop(), name="fake-appliance-reader")

    async def wait_closed(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._closed_event.wait(), timeout=timeout)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        self._closed_event.set()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.BINARY:
                    self._receive_buffer += message.data
                    try:
                        packets, self._receive_buffer = mqtt_codec.decode_packets(self._receive_buffer)
                    except mqtt_codec.MqttDecodeError:
                        # A test deliberately sending malformed bytes only cares
                        # about the *server's* reaction; give up decoding our
                        # own buffer rather than raising out of the reader task.
                        self._receive_buffer = b""
                        continue
                    for packet in packets:
                        await self._incoming.put(packet)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        finally:
            self._closed_event.set()

    # ------------------------------------------------------------------ #
    # Receiving
    # ------------------------------------------------------------------ #
    async def recv_packet(self, timeout: float = 2.0) -> mqtt_codec.Packet:
        """Wait for and return the next decoded packet from the bridge."""
        return await asyncio.wait_for(self._incoming.get(), timeout=timeout)

    async def expect(self, packet_type: type[mqtt_codec.Packet], timeout: float = 2.0) -> Any:
        """``recv_packet`` plus a type assertion, for readable test bodies."""
        packet = await self.recv_packet(timeout)
        if not isinstance(packet, packet_type):
            raise AssertionError(f"expected {packet_type.__name__}, got {packet!r}")
        return packet

    async def expect_unsuback(self, timeout: float = 2.0) -> int:
        """UNSUBACK has no dedicated dataclass (the codec only needs to decode it
        from the bridge's own broker role, never originate one); it surfaces as
        an UnknownPacket whose 2-byte body is the packet id."""
        packet = await self.recv_packet(timeout)
        if not (isinstance(packet, mqtt_codec.UnknownPacket) and packet.packet_type == mqtt_codec.UNSUBACK):
            raise AssertionError(f"expected UNSUBACK, got {packet!r}")
        if len(packet.body) < 2:
            raise AssertionError("UNSUBACK body shorter than a packet id")
        return int.from_bytes(packet.body[:2], "big")

    async def assert_silent(self, timeout: float = 0.3) -> None:
        """Assert nothing arrives within ``timeout`` (a dropped/ignored frame)."""
        try:
            packet = await self.recv_packet(timeout)
        except TimeoutError:
            return
        raise AssertionError(f"expected silence, but received {packet!r}")

    # ------------------------------------------------------------------ #
    # Sending -- low level
    # ------------------------------------------------------------------ #
    async def send_raw(self, frame: bytes) -> None:
        if self._ws is None:
            raise FakeApplianceError("not connected")
        await self._ws.send_bytes(frame)

    def next_packet_id(self) -> int:
        return next(self._packet_id_counter)

    # ------------------------------------------------------------------ #
    # Sending -- MQTT client requests
    # ------------------------------------------------------------------ #
    async def send_connect_raw(self, client_id: str, **kwargs: Any) -> None:
        """CONNECT with an arbitrary (possibly invalid) client id."""
        await self.send_raw(mqtt_codec.encode_connect(client_id, **kwargs))

    async def send_connect(self, serial: str, *, keepalive_seconds: int = 30, **kwargs: Any) -> None:
        await self.send_connect_raw(f"{protocol.DEVICE_CLIENT_ID_PREFIX}{serial}", keepalive_seconds=keepalive_seconds, **kwargs)

    async def subscribe(self, topics: list[tuple[str, int]]) -> int:
        packet_id = self.next_packet_id()
        await self.send_raw(mqtt_codec.encode_subscribe(packet_id, topics))
        return packet_id

    async def unsubscribe(self, topics: list[str]) -> int:
        packet_id = self.next_packet_id()
        await self.send_raw(mqtt_codec.encode_unsubscribe(packet_id, topics))
        return packet_id

    async def publish(self, topic: str, payload: bytes, *, qos: int = 0, retain: bool = False, packet_id: int | None = None) -> int | None:
        if qos > 0 and packet_id is None:
            packet_id = self.next_packet_id()
        await self.send_raw(mqtt_codec.encode_publish(topic, payload, qos=qos, packet_id=packet_id, retain=retain))
        return packet_id

    async def send_puback(self, packet_id: int) -> None:
        await self.send_raw(mqtt_codec.encode_puback(packet_id))

    async def send_pingreq(self) -> None:
        await self.send_raw(mqtt_codec.encode_pingreq())

    async def send_disconnect(self) -> None:
        await self.send_raw(mqtt_codec.encode_disconnect())

    # ------------------------------------------------------------------ #
    # High-level helpers matching the real appliance's boot sequence
    # ------------------------------------------------------------------ #
    async def handshake(self, serial: str, *, keepalive_seconds: int = 30, subscribe_command: bool = True, **connect_kwargs: Any):
        """CONNECT, wait for CONNACK, and (if accepted) SUBSCRIBE to the
        appliance's command-request and time-sync topics like the real unit."""
        await self.send_connect(serial, keepalive_seconds=keepalive_seconds, **connect_kwargs)
        connack = await self.expect(mqtt_codec.ConnAckPacket)
        if subscribe_command and connack.return_code == mqtt_codec.CONNACK_ACCEPTED:
            await self.subscribe([(protocol.command_request_topic(serial), 1), (protocol.TOPIC_TIME_SYNC, 1)])
            await self.expect(mqtt_codec.SubAckPacket)
        return connack

    async def publish_online(self, serial: str, *, online: bool = True, qos: int = 1) -> int | None:
        payload = json.dumps({"cmd": protocol.CMD_ONLINE, "sn": serial, "status": online}, separators=(",", ":")).encode()
        return await self.publish(protocol.lwt_topic(serial), payload, qos=qos)

    async def publish_state_snapshot(self, serial: str, state: dict[str, Any], *, qos: int = 1) -> int | None:
        # Device -> cloud replies carry their body under "result" (PROTOCOL.md, state.apply_device_message).
        payload = json.dumps({"cmd": protocol.CMD_STATE_SNAPSHOT, "sn": serial, "result": state}, separators=(",", ":")).encode()
        return await self.publish(protocol.command_reply_topic(serial), payload, qos=qos)

    async def publish_reply(self, serial: str, state_delta: dict[str, Any], *, origin: int = protocol.ORIGIN_DEVICE, qos: int = 1) -> int | None:
        payload = json.dumps(
            {"cmd": protocol.CMD_REPLY, "sn": serial, "user": "", "result": {**state_delta, "origin": origin}}, separators=(",", ":")
        ).encode()
        return await self.publish(protocol.command_reply_topic(serial), payload, qos=qos)

    async def publish_device_info(self, serial: str, info: dict[str, Any], *, qos: int = 1) -> int | None:
        payload = json.dumps({"cmd": protocol.CMD_DEVICE_INFO, "sn": serial, "result": info}, separators=(",", ":")).encode()
        return await self.publish(protocol.command_reply_topic(serial), payload, qos=qos)

    async def auto_ack_injected_commands(self, *, idle_timeout: float = 5.0) -> None:
        """Background helper for manual runs: PUBACK every QoS-1 PUBLISH the
        bridge injects (a command or a state/info request), forever, until the
        connection closes or nothing arrives for ``idle_timeout`` seconds."""
        while not self.closed:
            try:
                packet = await self.recv_packet(timeout=idle_timeout)
            except TimeoutError:
                return
            if isinstance(packet, mqtt_codec.PublishPacket) and packet.qos == 1 and packet.packet_id is not None:
                await self.send_puback(packet.packet_id)


# --------------------------------------------------------------------------- #
# Manual CLI entry point
# --------------------------------------------------------------------------- #
# What the CLI's simulated unit reports. Shaped like the tested window AC
# (PROTOCOL.md) but with an obviously fake product code.
_CLI_INITIAL_STATE: dict[str, Any] = {
    protocol.KEY_POWER: False,
    protocol.KEY_MODE: 1,
    protocol.KEY_TARGET_TEMP: 72,
    protocol.KEY_CURRENT_TEMP: 75,
    protocol.KEY_FAN_LEVEL: 4,
    protocol.KEY_EXTRA_FAN: False,
    protocol.KEY_ECO: False,
    protocol.KEY_SLEEP: False,
    protocol.KEY_SWING_VERTICAL: False,
    protocol.KEY_SWING_HORIZONTAL: False,
    protocol.KEY_MUTE: False,
    protocol.KEY_DISPLAY_LIGHT: True,
    protocol.KEY_CHILD_LOCK: False,
    protocol.KEY_RUNTIME: 0,
    protocol.KEY_TEMP_UNIT: protocol.TEMP_UNIT_FAHRENHEIT,
    protocol.KEY_FILTER_COUNTER: 250,
    protocol.KEY_FAULT_CODE: 0,
}
_CLI_DEVICE_INFO: dict[str, Any] = {
    protocol.INFO_VENDOR: "I4SEASON",
    protocol.INFO_PRODUCT: "FAKE-MODEL-X1",
    protocol.INFO_MODULE_FIRMWARE: "0.0.0",
    protocol.INFO_WIFI_SSID: "fake-network",
    protocol.INFO_WIFI_RSSI: -50,
    protocol.INFO_MCU_FIRMWARE: "0.0.0",
    protocol.INFO_MCU_PART: "FAKE-MCU",
}


async def _answer_like_the_real_unit(appliance: FakeAppliance, serial: str, state: dict[str, Any], payload: bytes) -> None:
    """Reply to a bridge request the way the appliance does: cmd 2 -> cmd 3, cmd 5 -> cmd 5, cmd 6 -> cmd 4."""
    message = protocol.parse_device_message(payload) or {}
    command_code = message.get("cmd")
    if command_code == protocol.CMD_REQUEST_STATE:
        await appliance.publish_state_snapshot(serial, state)
    elif command_code == protocol.CMD_DEVICE_INFO:
        await appliance.publish_device_info(serial, _CLI_DEVICE_INFO)
    elif command_code == protocol.CMD_SET:
        changes = (message.get("data") or {}).get("state") or {}
        state.update(changes)
        await appliance.publish_reply(serial, changes, origin=protocol.ORIGIN_COMMANDED)
def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manually drive a Zafro Bridge device endpoint as a fake appliance.")
    parser.add_argument("--host", required=True, help="bridge host/IP the device would be redirected to")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument(
        "--serial",
        default=DEFAULT_FAKE_SERIAL,
        help=f"an obviously-fake serial to identify as (default: {DEFAULT_FAKE_SERIAL})",
    )
    parser.add_argument("--keepalive", type=int, default=30)
    parser.add_argument("--verify-tls", action="store_true", help="validate the bridge's certificate (off by default, like the real unit)")
    return parser


async def _run_cli(args: argparse.Namespace) -> None:
    url = f"wss://{args.host}:{args.port}{protocol.WEBSOCKET_PATH}"
    appliance = FakeAppliance(url, verify_tls=args.verify_tls)
    print(f"connecting to {url} as serial {args.serial!r} ...")
    await appliance.connect()
    try:
        connack = await appliance.handshake(args.serial, keepalive_seconds=args.keepalive)
        print(f"CONNACK return_code={connack.return_code}")
        if connack.return_code != mqtt_codec.CONNACK_ACCEPTED:
            print("connection refused by the bridge; exiting")
            return
        await appliance.publish_online(args.serial, online=True)
        print("published birth message (online)")
        print("answering the bridge's requests and commands; Ctrl+C to stop")
        simulated_state = dict(_CLI_INITIAL_STATE)
        ping_interval = max(args.keepalive / 2, 1)
        last_ping = time.monotonic()
        while True:
            remaining = max(ping_interval - (time.monotonic() - last_ping), 0.1)
            try:
                packet = await appliance.recv_packet(timeout=remaining)
            except TimeoutError:
                await appliance.send_pingreq()
                last_ping = time.monotonic()
                continue
            print(f"<- {packet!r}")
            if isinstance(packet, mqtt_codec.PublishPacket) and packet.qos == 1 and packet.packet_id is not None:
                await appliance.send_puback(packet.packet_id)
                print(f"-> PUBACK {packet.packet_id}")
            if isinstance(packet, mqtt_codec.PublishPacket):
                await _answer_like_the_real_unit(appliance, args.serial, simulated_state, packet.payload)
    finally:
        if not appliance.closed:
            await appliance.publish_online(args.serial, online=False)
        await appliance.close()


def main() -> None:
    args = _build_arg_parser().parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run_cli(args))


if __name__ == "__main__":
    main()
