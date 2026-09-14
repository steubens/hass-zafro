"""One connected appliance: we are its MQTT broker (over WebSocket).

Two operating modes, chosen per session at CONNECT time:

* **relay** — the vendor cloud is reachable: every frame from the device is
  forwarded upstream verbatim and every upstream frame is forwarded down. The
  cloud answers CONNACK/SUBACK/PUBACK/PINGRESP; we just watch and, while a
  cloud answer is outstanding, remember it in a small pending-ack ledger.
* **local** — no cloud (disabled, unreachable, or the cloud refused us): we
  answer as the broker ourselves. Control keeps working with no internet at
  all, and anything the ledger was still waiting on is answered immediately
  so the device is never left hanging mid-handshake.

In both modes we *observe* the device's publishes to update Home Assistant,
and we can *inject* our own PUBLISHes (commands, state requests). Injected
packets use a private packet-id range so the device's PUBACKs for them are
recognized and never leaked upstream.

Before a device is trusted at all it must present a client id of the form
``dev_<serial>`` (see protocol.serial_from_client_id) and, if the add-on's
``allowed_serials`` option is non-empty, that serial must be on the list.
Everything else about a session — the connect timeout, the MQTT keepalive
watchdog, and the receive-buffer/decode-error guards — exists to make sure a
single misbehaving or hostile peer on this port can only ever cost us one
bounded session, never the process.

If the upstream link drops mid-session we degrade to local mode transparently
and, if relaying is still enabled, keep periodically probing the cloud in the
background so the appliance can be bounced back onto the real relay once it
recovers.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp
from aiohttp import web

from . import mqtt_codec, protocol
from .cloud_relay import UpstreamConnection
from .config import Settings

_LOGGER = logging.getLogger(__name__)

# Packet ids we use for injected PUBLISHes; well clear of what the cloud/app use.
_INJECTED_PACKET_ID_START = 60_000
_INJECTED_PACKET_ID_END = 65_535
_INJECTED_PACKET_ID_RANGE_SIZE = _INJECTED_PACKET_ID_END - _INJECTED_PACKET_ID_START

# Upper bound on PUBACKs we'll wait for from injected PUBLISHes at once. Caps
# memory if the device stops acknowledging entirely; the oldest outstanding
# id is evicted to make room rather than letting the set grow forever.
_MAX_PENDING_INJECTED_PUBLISHES = 256

# Longest the connection watchdog will ever sleep before re-checking its
# deadline; see the comment in _connection_watchdog for why this must be
# short rather than sleeping the full remaining time in one shot.
_WATCHDOG_POLL_SECONDS = 1.0

OnConnectedCallback = Callable[["DeviceSession", str], Awaitable[None]]  # (session, serial)
OnPublishCallback = Callable[[str, str, bytes], Awaitable[None]]  # (serial, topic, payload)
OnDisconnectedCallback = Callable[["DeviceSession", str], Awaitable[None]]  # (session, serial)


class _RelayAbandoned(Exception):
    """Internal signal that we deliberately stopped relaying mid-frame.

    Raised from within the upstream receive loop (a cloud CONNACK refusal) so
    it unwinds cleanly without being mistaken for a network failure by the
    generic exception handler around it.
    """


class DeviceSession:
    """Lifecycle of a single device WebSocket, from upgrade to close."""

    def __init__(
        self,
        websocket: web.WebSocketResponse,
        settings: Settings,
        on_connected: OnConnectedCallback,
        on_publish: OnPublishCallback,
        on_disconnected: OnDisconnectedCallback,
    ) -> None:
        self._websocket = websocket
        self._settings = settings
        self._on_connected = on_connected
        self._on_publish = on_publish
        self._on_disconnected = on_disconnected

        self.serial_number: str | None = None
        self._upstream: UpstreamConnection | None = None
        self._upstream_task: asyncio.Task[None] | None = None
        self._receive_buffer = b""
        # Cloud->device bytes not yet forming a complete MQTT packet. Buffering
        # (rather than forwarding a split frame verbatim) keeps the pending-ack
        # ledger in step with what the device has actually been sent.
        self._upstream_receive_buffer = b""
        self._send_lock = asyncio.Lock()
        self._injected_packet_ids: dict[int, None] = {}  # insertion-ordered set
        self._packet_id_counter = itertools.cycle(range(_INJECTED_PACKET_ID_START, _INJECTED_PACKET_ID_END))
        self._closed = False

        # Protocol-state / lifecycle bookkeeping.
        self._connect_seen = False
        self._keepalive_seconds = 0
        self._accepted = False  # True once on_connected has fired
        self._disconnected_fired = False
        self._watchdog_task: asyncio.Task[None] | None = None
        # The pre-CONNECT deadline is measured from the upgrade, not from the
        # last frame: otherwise a peer could hold a session slot forever by
        # trickling frames that never form a CONNECT.
        self._session_started_monotonic = time.monotonic()
        self._last_activity_monotonic = self._session_started_monotonic

        # Relay auto-recovery.
        self._retry_task: asyncio.Task[None] | None = None
        self._relay_refused = False  # a CONNACK refusal ends relay for good this session

        # Pending-ack ledger: device->cloud frames forwarded upstream that are
        # still awaiting a cloud answer. If the link drops before the answer
        # arrives, _answer_pending_locally() synthesizes it.
        self._pending_connect = False
        self._pending_subscribes: dict[int, list[int]] = {}
        self._pending_unsubscribes: set[int] = set()
        self._pending_device_publishes: set[int] = set()
        self._pending_pingreq = False

    # ------------------------------------------------------------------ #
    # Public API used by the bridge
    # ------------------------------------------------------------------ #
    @property
    def relaying(self) -> bool:
        return self._upstream is not None and self._upstream.is_open

    async def inject_publish(self, topic: str, payload: bytes) -> None:
        """Send our own QoS-1 PUBLISH to the device (a command or a request)."""
        packet_id = self._allocate_injected_packet_id()
        frame = mqtt_codec.encode_publish(topic, payload, qos=1, packet_id=packet_id)
        _LOGGER.debug("[%s] inject -> %s %s", self.serial_number, topic, protocol.redact_for_log(payload))
        await self._send_to_device(frame)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Never cancel the task that is currently running this close() call:
        # both the keepalive watchdog and the relay-retry task call close()
        # on themselves, and asyncio.Task.cancel() on the running task would
        # interrupt the very cleanup below at its next await point.
        current_task = asyncio.current_task()
        for task in (self._watchdog_task, self._retry_task, self._upstream_task):
            if task is not None and task is not current_task:
                task.cancel()
        if self._upstream is not None:
            upstream, self._upstream = self._upstream, None
            await upstream.close()
        if not self._websocket.closed:
            await self._websocket.close()

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Pump device frames until the socket closes."""
        self._watchdog_task = asyncio.create_task(self._connection_watchdog(), name=f"watchdog-{id(self)}")
        end_reason = "unknown"
        try:
            async for message in self._websocket:
                if message.type == aiohttp.WSMsgType.BINARY:
                    # MQTT keepalive is about MQTT traffic, which the appliance
                    # always sends as binary frames; other frame types don't count.
                    self._last_activity_monotonic = time.monotonic()
                    await self._handle_device_bytes(message.data)
                elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    end_reason = f"device closed WS ({message.type.name}, code={self._websocket.close_code})"
                    break
                elif message.type == aiohttp.WSMsgType.ERROR:
                    end_reason = f"device WS error: {self._websocket.exception()}"
                    _LOGGER.warning("[%s] %s", self.serial_number, end_reason)
                    break
                elif message.type == aiohttp.WSMsgType.PING:
                    _LOGGER.debug("[%s] <- device WS PING", self.serial_number)
                elif message.type == aiohttp.WSMsgType.PONG:
                    _LOGGER.debug("[%s] <- device WS PONG", self.serial_number)
            else:
                end_reason = "device WS iterator exhausted"
        finally:
            _LOGGER.info("[%s] session ending: %s", self.serial_number, end_reason)
            try:
                await self.close()
            finally:
                # Even if tearing down the upstream fails, the bridge must still
                # hear about the disconnect or HA would show the device online.
                await self._safe_on_disconnected()

    async def _handle_device_bytes(self, data: bytes) -> None:
        self._receive_buffer += data
        if len(self._receive_buffer) > 2 * mqtt_codec.MAX_PACKET_BYTES:
            _LOGGER.warning(
                "[%s] receive buffer exceeded %d bytes without a complete packet; closing "
                "(a desynchronised or hostile stream cannot be trusted)",
                self.serial_number,
                2 * mqtt_codec.MAX_PACKET_BYTES,
            )
            await self.close()
            return
        try:
            packets, self._receive_buffer = mqtt_codec.decode_packets(self._receive_buffer)
        except mqtt_codec.MqttDecodeError as error:
            _LOGGER.warning(
                "[%s] undecodable MQTT from device (%s); closing rather than guessing at a resync point",
                self.serial_number,
                error,
            )
            await self.close()
            return
        for packet in packets:
            await self._handle_device_packet(packet)

    async def _handle_device_packet(self, packet: mqtt_codec.Packet) -> None:
        if isinstance(packet, mqtt_codec.ConnectPacket):
            if self._connect_seen:
                _LOGGER.warning("[%s] second CONNECT on an established session; closing", self.serial_number)
                await self.close()
                return
            self._connect_seen = True
            await self._handle_connect(packet)
            return

        if not self._connect_seen:
            _LOGGER.warning("dropping %s received before CONNECT; closing session", type(packet).__name__)
            await self.close()
            return

        _LOGGER.debug("[%s] <- device %s (relaying=%s)", self.serial_number, type(packet).__name__, self.relaying)

        # Swallow PUBACKs that answer our own injected PUBLISHes.
        if isinstance(packet, mqtt_codec.PubAckPacket) and packet.packet_id in self._injected_packet_ids:
            del self._injected_packet_ids[packet.packet_id]
            return

        if isinstance(packet, mqtt_codec.PublishPacket):
            if not await self._observe_device_publish(packet):
                return  # topic serial spoofed a different device: drop entirely

        if self.relaying:
            await self._forward_upstream(packet)
        else:
            await self._answer_locally(packet)

    # ------------------------------------------------------------------ #
    # CONNECT: identity, allow-list, then decide relay vs local
    # ------------------------------------------------------------------ #
    async def _handle_connect(self, packet: mqtt_codec.ConnectPacket) -> None:
        serial = protocol.serial_from_client_id(packet.client_id)
        if serial is None:
            _LOGGER.warning("rejecting CONNECT with an unrecognized client id: %r", packet.client_id[:80])
            await self._send_to_device(mqtt_codec.encode_connack(mqtt_codec.CONNACK_IDENTIFIER_REJECTED))
            await self.close()
            return

        allowed = self._settings.allowed_serials
        if allowed and serial not in allowed:
            _LOGGER.warning(
                "[%s] CONNECT refused: not in allowed_serials; add it to the add-on's "
                "allowed_serials option if this is your appliance",
                serial,
            )
            await self._send_to_device(mqtt_codec.encode_connack(mqtt_codec.CONNACK_NOT_AUTHORIZED))
            await self.close()
            return

        self.serial_number = serial
        self._keepalive_seconds = packet.keepalive_seconds
        _LOGGER.info(
            "[%s] device CONNECT (proto %s v%s, keepalive %ss, will=%s)",
            self.serial_number,
            packet.protocol_name,
            packet.protocol_level,
            packet.keepalive_seconds,
            packet.will_topic,
        )

        if self._settings.cloud_relay_enabled:
            await self._try_open_upstream()

        if self.relaying:
            await self._forward_upstream(packet)  # the cloud will CONNACK (or we fall back below)
        else:
            await self._send_to_device(mqtt_codec.encode_connack(mqtt_codec.CONNACK_ACCEPTED))

        await self._safe_on_connected()

    async def _try_open_upstream(self) -> None:
        upstream = UpstreamConnection(self._settings.cloud_host, self._settings.cloud_connect_timeout_seconds)
        try:
            await upstream.connect()
        except Exception as error:  # noqa: BLE001 - any failure means "run local"
            _LOGGER.warning("[%s] cloud unreachable (%s); serving device locally", self.serial_number, error)
            self._maybe_start_relay_retry()
            return
        self._upstream = upstream
        self._upstream_task = asyncio.create_task(self._pump_upstream(), name=f"upstream-{self.serial_number}")
        _LOGGER.info("[%s] relaying to %s (app stays functional)", self.serial_number, self._settings.cloud_host)

    async def _pump_upstream(self) -> None:
        upstream = self._upstream
        assert upstream is not None
        try:
            await upstream.receive_loop(self._handle_upstream_bytes)
        except _RelayAbandoned:
            pass  # already reconciled by _fallback_to_local before this was raised
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            _LOGGER.warning("[%s] upstream pump ended: %s", self.serial_number, error)
        finally:
            self._upstream_task = None
            if not self._closed and self._upstream is upstream:
                # The cloud link dropped on its own rather than through one of
                # the explicit fallback paths above; reconcile the same way.
                await self._fallback_to_local("cloud link lost; upstream pump ended")

    async def _handle_upstream_bytes(self, data: bytes) -> None:
        self._upstream_receive_buffer += data
        try:
            packets, self._upstream_receive_buffer = mqtt_codec.decode_packets(self._upstream_receive_buffer)
        except mqtt_codec.MqttDecodeError as error:
            # Preserve the byte-for-byte relay for anything we can't parse
            # rather than risk mis-handling a frame we don't understand.
            _LOGGER.debug("[%s] upstream bytes did not decode (%s); forwarding verbatim", self.serial_number, error)
            undecodable, self._upstream_receive_buffer = self._upstream_receive_buffer, b""
            await self._send_to_device(undecodable)
            return
        # A packet split across WebSocket frames stays buffered until complete;
        # every complete packet is forwarded on its own, in order.
        for packet in packets:
            await self._handle_upstream_packet(packet)

    async def _handle_upstream_packet(self, packet: mqtt_codec.Packet) -> None:
        """Clear the pending-ack ledger for one cloud->device answer, then forward it."""
        if isinstance(packet, mqtt_codec.ConnAckPacket):
            if packet.return_code != mqtt_codec.CONNACK_ACCEPTED:
                # Leave _pending_connect set: _fallback_to_local's call to
                # _answer_pending_locally() is what owes the device its
                # (synthesized, accepted) CONNACK, and clearing it here first
                # would make that a silent no-op.
                _LOGGER.warning(
                    "[%s] cloud refused CONNECT (code %s); falling back to local mode",
                    self.serial_number,
                    packet.return_code,
                )
                await self._fallback_to_local("cloud CONNACK refusal", after_connack_refusal=True)
                raise _RelayAbandoned  # do not forward the refusal; the fallback already answered locally
            self._pending_connect = False
        elif isinstance(packet, mqtt_codec.SubAckPacket):
            self._pending_subscribes.pop(packet.packet_id, None)
        elif isinstance(packet, mqtt_codec.PubAckPacket):
            self._pending_device_publishes.discard(packet.packet_id)
        elif isinstance(packet, mqtt_codec.PingRespPacket):
            self._pending_pingreq = False
        elif isinstance(packet, mqtt_codec.UnknownPacket) and packet.packet_type == mqtt_codec.UNSUBACK:
            packet_id = _read_unsuback_packet_id(packet.body)
            if packet_id is not None:
                self._pending_unsubscribes.discard(packet_id)
        elif isinstance(packet, mqtt_codec.PublishPacket):
            if packet.packet_id is not None and packet.packet_id in self._injected_packet_ids:
                _LOGGER.debug(
                    "[%s] cloud publish id %s collides with a pending injected publish",
                    self.serial_number,
                    packet.packet_id,
                )
            _LOGGER.debug(
                "[%s] cloud -> device %s %s", self.serial_number, packet.topic, protocol.redact_for_log(packet.payload)
            )
        await self._send_to_device(packet.raw)

    # ------------------------------------------------------------------ #
    # Pending-ack ledger and relay fallback
    # ------------------------------------------------------------------ #
    def _record_pending_ack(self, packet: mqtt_codec.Packet) -> None:
        """Remember a device->cloud frame that now awaits a cloud answer."""
        if isinstance(packet, mqtt_codec.ConnectPacket):
            self._pending_connect = True
        elif isinstance(packet, mqtt_codec.SubscribePacket):
            self._pending_subscribes[packet.packet_id] = [qos for _, qos in packet.subscriptions]
        elif isinstance(packet, mqtt_codec.UnsubscribePacket):
            self._pending_unsubscribes.add(packet.packet_id)
        elif isinstance(packet, mqtt_codec.PublishPacket) and packet.qos == 1 and packet.packet_id is not None:
            self._pending_device_publishes.add(packet.packet_id)
        elif isinstance(packet, mqtt_codec.PingReqPacket):
            self._pending_pingreq = True

    async def _answer_pending_locally(self) -> None:
        """Synthesize a broker answer for every relay frame still awaiting a cloud reply.

        The ledger is converted to answer frames and cleared before the first
        await, so traffic handled concurrently while these are being sent can
        neither mutate what we iterate nor be answered twice.
        """
        answers: list[bytes] = []
        if self._pending_connect:
            answers.append(mqtt_codec.encode_connack(mqtt_codec.CONNACK_ACCEPTED))
        for packet_id, requested_qos in self._pending_subscribes.items():
            answers.append(mqtt_codec.encode_suback(packet_id, [min(qos, 1) for qos in requested_qos]))
        answers.extend(mqtt_codec.encode_unsuback(packet_id) for packet_id in self._pending_unsubscribes)
        answers.extend(mqtt_codec.encode_puback(packet_id) for packet_id in self._pending_device_publishes)
        if self._pending_pingreq:
            answers.append(mqtt_codec.encode_pingresp())

        self._pending_connect = False
        self._pending_subscribes.clear()
        self._pending_unsubscribes.clear()
        self._pending_device_publishes.clear()
        self._pending_pingreq = False

        for frame in answers:
            await self._send_to_device(frame)

    async def _fallback_to_local(self, reason: str, *, after_connack_refusal: bool = False) -> None:
        """Stop relaying and answer every frame the cloud still owed the device.

        Called whenever the upstream link is abandoned mid-session: a forward
        failed, the upstream pump ended on its own, or the cloud refused our
        CONNECT. The device must never be left waiting for an answer that
        will now never arrive.
        """
        upstream = self._upstream
        if upstream is None:
            return  # already local; nothing to reconcile (e.g. a second failure racing in)
        # Detach synchronously, before any await: device frames arriving from
        # here on are answered locally instead of forwarded, and a concurrent
        # second fallback sees the guard above and returns.
        self._upstream = None
        self._upstream_receive_buffer = b""
        _LOGGER.warning("[%s] %s; continuing in local mode", self.serial_number, reason)
        await self._answer_pending_locally()
        await upstream.close()
        if after_connack_refusal:
            # The cloud actively rejected this appliance; don't keep probing
            # for the rest of this session; a fresh CONNECT may try again.
            self._relay_refused = True
        else:
            self._maybe_start_relay_retry()

    # ------------------------------------------------------------------ #
    # Relay auto-recovery
    # ------------------------------------------------------------------ #
    def _maybe_start_relay_retry(self) -> None:
        if not self._settings.cloud_relay_enabled or self._relay_refused or self._closed:
            return
        if self._retry_task is not None and not self._retry_task.done():
            return  # already retrying
        self._retry_task = asyncio.create_task(self._retry_relay_recovery(), name=f"relay-retry-{self.serial_number}")

    async def _retry_relay_recovery(self) -> None:
        """While stuck in local mode, periodically probe the cloud and bounce
        the device connection once it is reachable again so the appliance
        reconnects fresh through the relay path (fresh login + CONNECT).
        """
        try:
            while not self._closed:
                await asyncio.sleep(self._settings.relay_retry_seconds)
                if self._closed:
                    return
                probe = UpstreamConnection(self._settings.cloud_host, self._settings.cloud_connect_timeout_seconds)
                try:
                    await probe.connect()
                except Exception:  # noqa: BLE001 - still unreachable; try again next cycle
                    continue
                finally:
                    await probe.close()
                _LOGGER.info(
                    "[%s] vendor cloud reachable again; reconnecting the appliance through the relay",
                    self.serial_number,
                )
                await self.close()
                return
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    # Keepalive / connect-timeout watchdog
    # ------------------------------------------------------------------ #
    async def _connection_watchdog(self) -> None:
        """Enforce the pre-CONNECT timeout, then the post-CONNECT MQTT keepalive.

        One recurring timer covers both phases (only one is ever active):
        before CONNECT, ``settings.device_connect_timeout_seconds``;
        afterwards, 1.5x the keepalive the device requested (the MQTT spec's
        own tolerance), unless it asked for keepalive 0 (disabled), in which
        case nothing is enforced.
        """
        try:
            while not self._closed:
                timeout = self._current_watchdog_timeout()
                if timeout is None:
                    return
                reference = self._last_activity_monotonic if self._connect_seen else self._session_started_monotonic
                remaining = timeout - (time.monotonic() - reference)
                if remaining <= 0:
                    _LOGGER.warning("[%s] %s; closing", self.serial_number, self._watchdog_timeout_description())
                    await self.close()
                    return
                # Cap each sleep rather than sleeping the full `remaining`: the
                # CONNECT that ends the pre-CONNECT phase can (and usually
                # does) arrive well before `device_connect_timeout_seconds`
                # elapses, switching `_current_watchdog_timeout()` from that
                # timeout to the much shorter 1.5x-keepalive one. Sleeping the
                # stale `remaining` computed for the *old* phase would leave
                # the loop blind to that switch until the original, longer
                # sleep finally finished -- badly delaying keepalive
                # enforcement for any device whose CONNECT arrives promptly.
                # Polling at this granularity keeps detection latency small
                # without spinning.
                await asyncio.sleep(min(remaining, _WATCHDOG_POLL_SECONDS))
        except asyncio.CancelledError:
            pass

    def _current_watchdog_timeout(self) -> float | None:
        if not self._connect_seen:
            return self._settings.device_connect_timeout_seconds
        if self._keepalive_seconds > 0:
            return self._keepalive_seconds * 1.5
        return None

    def _watchdog_timeout_description(self) -> str:
        if not self._connect_seen:
            return f"no CONNECT within {self._settings.device_connect_timeout_seconds:g}s of the WebSocket upgrade"
        return f"no traffic for {self._keepalive_seconds * 1.5:g}s (1.5x the {self._keepalive_seconds}s keepalive)"

    # ------------------------------------------------------------------ #
    # Observation and local broker behaviour
    # ------------------------------------------------------------------ #
    async def _observe_device_publish(self, packet: mqtt_codec.PublishPacket) -> bool:
        """Report a device publish to the bridge. False if it was dropped (spoofed topic serial)."""
        topic_serial = protocol.serial_from_topic(packet.topic)
        if topic_serial is not None and topic_serial != self.serial_number:
            _LOGGER.warning(
                "[%s] dropping publish on a topic for a different serial (%s): %s",
                self.serial_number,
                topic_serial,
                packet.topic,
            )
            return False
        _LOGGER.debug(
            "[%s] device -> %s %s", self.serial_number, packet.topic, protocol.redact_for_log(packet.payload)
        )
        await self._safe_on_publish(packet.topic, packet.payload)
        return True

    async def _answer_locally(self, packet: mqtt_codec.Packet) -> None:
        """Play broker: acknowledge exactly what a real broker would."""
        if isinstance(packet, mqtt_codec.SubscribePacket):
            granted = [min(qos, 1) for _, qos in packet.subscriptions]
            await self._send_to_device(mqtt_codec.encode_suback(packet.packet_id, granted))
        elif isinstance(packet, mqtt_codec.UnsubscribePacket):
            await self._send_to_device(mqtt_codec.encode_unsuback(packet.packet_id))
        elif isinstance(packet, mqtt_codec.PublishPacket):
            if packet.qos == 2:
                # QoS 2 is unsupported by this broker role; the appliance has
                # never been observed using it, so refuse rather than fake it.
                _LOGGER.warning(
                    "[%s] device published at QoS 2 (unsupported by this broker); not acknowledging", self.serial_number
                )
            elif packet.qos == 1 and packet.packet_id is not None:
                await self._send_to_device(mqtt_codec.encode_puback(packet.packet_id))
        elif isinstance(packet, mqtt_codec.PingReqPacket):
            await self._send_to_device(mqtt_codec.encode_pingresp())
        elif isinstance(packet, mqtt_codec.DisconnectPacket):
            await self.close()
        # PUBACK from the device (for cloud-originated ids) and unknown packets need no answer.

    # ------------------------------------------------------------------ #
    # Transport helpers
    # ------------------------------------------------------------------ #
    async def _forward_upstream(self, packet: mqtt_codec.Packet) -> None:
        if self._upstream is None:
            return
        self._record_pending_ack(packet)
        try:
            await self._upstream.send(packet.raw)
        except Exception as error:  # noqa: BLE001
            # The pending-ack ledger already has this frame recorded, so the
            # fallback below answers it locally along with anything else
            # still outstanding.
            await self._fallback_to_local(f"upstream send failed ({error})")

    async def _send_to_device(self, raw_frame: bytes) -> None:
        if self._websocket.closed:
            return
        async with self._send_lock:
            try:
                await self._websocket.send_bytes(raw_frame)
            except (ConnectionError, RuntimeError) as error:
                # A transient write failure must never tear down the session loop;
                # the socket close will be handled by run()'s iterator ending.
                # (asyncio.CancelledError is deliberately not caught here: it is a
                # BaseException carrying task-cancellation, not a send failure.)
                _LOGGER.debug("[%s] send to device failed: %s", self.serial_number, error)

    # ------------------------------------------------------------------ #
    # Injected packet ids
    # ------------------------------------------------------------------ #
    def _allocate_injected_packet_id(self) -> int:
        """Pick a packet id for our own PUBLISH, distinct from any still pending.

        The pending set is capped so a device that stops PUBACKing entirely
        can't grow it without bound: the oldest outstanding id is evicted
        first to guarantee a free id always exists within one pass of the
        (much larger) id range.
        """
        if len(self._injected_packet_ids) >= _MAX_PENDING_INJECTED_PUBLISHES:
            oldest_id = next(iter(self._injected_packet_ids))
            del self._injected_packet_ids[oldest_id]
        candidate = next(self._packet_id_counter)
        for _ in range(_INJECTED_PACKET_ID_RANGE_SIZE):
            if candidate not in self._injected_packet_ids:
                break
            candidate = next(self._packet_id_counter)
        self._injected_packet_ids[candidate] = None
        return candidate

    # ------------------------------------------------------------------ #
    # Callback isolation: a bug in the bridge must never end this session.
    # ------------------------------------------------------------------ #
    async def _safe_on_connected(self) -> None:
        self._accepted = True
        try:
            await self._on_connected(self, self.serial_number)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - isolation boundary, see class docstring
            _LOGGER.exception("[%s] on_connected callback raised", self.serial_number)

    async def _safe_on_publish(self, topic: str, payload: bytes) -> None:
        try:
            await self._on_publish(self.serial_number, topic, payload)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - isolation boundary, see class docstring
            _LOGGER.exception("[%s] on_publish callback raised", self.serial_number)

    async def _safe_on_disconnected(self) -> None:
        if not self._accepted or self._disconnected_fired:
            return
        self._disconnected_fired = True
        try:
            await self._on_disconnected(self, self.serial_number)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - isolation boundary, see class docstring
            _LOGGER.exception("[%s] on_disconnected callback raised", self.serial_number)


def _read_unsuback_packet_id(body: bytes) -> int | None:
    """Best-effort UNSUBACK packet-id parse.

    mqtt_codec has no dedicated UnsubAckPacket (the bridge, as the device's
    broker, never needs to decode one from a client); a genuine UNSUBACK
    arriving from the cloud during a relay therefore surfaces as an
    UnknownPacket whose body is just the 2-byte packet id.
    """
    if len(body) < 2:
        return None
    return int.from_bytes(body[:2], "big")
