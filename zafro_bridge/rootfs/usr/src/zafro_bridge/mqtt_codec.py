"""Minimal MQTT 3.1 / 3.1.1 packet codec for the device-facing broker role.

The appliance tunnels MQTT (protocol name "MQIsdp", level 3) inside WebSocket
binary frames. We act as its broker locally, so we must *decode* what it sends
(CONNECT, SUBSCRIBE, UNSUBSCRIBE, PUBLISH, PUBACK, PINGREQ, DISCONNECT) and
*encode* what a broker answers (CONNACK, SUBACK, UNSUBACK, PUBACK, PINGRESP)
plus our own PUBLISHes. Client-side encoders (CONNECT, SUBSCRIBE, UNSUBSCRIBE,
PINGREQ, DISCONNECT) are also provided; the device never receives these from
us, but tests and a fake-appliance simulator use them to drive the decoder
from the other direction.

Only the subset the device actually uses is implemented; anything else is
surfaced as an ``UnknownPacket`` and passed through untouched by the relay.
Each decoded packet keeps its exact ``raw`` bytes so the cloud relay can
forward frames byte-for-byte without re-encoding.

UNSUBSCRIBE is decoded and answered with UNSUBACK, but QoS 2 is intentionally
unimplemented anywhere in this codec: as the device's broker we only ever
grant QoS 0/1 in SUBACK, and the appliance has never been observed publishing
or subscribing at QoS 2. A QoS-2 PUBLISH (flags value 3, which MQTT reserves
as invalid) is rejected as malformed rather than silently misparsed.

This module never trusts the network: every decode path -- including every
fixed-width field read (protocol level, connect flags, keepalive, packet
ids, per-topic QoS bytes) -- is bounds-checked and raises ``MqttDecodeError``
on truncated or malformed input, never a bare ``IndexError``/``ValueError``.
``decode_packets`` also caps how large a single packet's declared remaining
length may be, so a peer cannot make us buffer unbounded memory waiting for
the rest of a packet that claims to be gigabytes long.

Pure Python, no I/O, fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Packet type identifiers (high nibble of the fixed header's first byte)
CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
SUBSCRIBE = 8
SUBACK = 9
UNSUBSCRIBE = 10
UNSUBACK = 11
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14

CONNACK_ACCEPTED = 0
CONNACK_UNACCEPTABLE_PROTOCOL = 1
CONNACK_IDENTIFIER_REJECTED = 2
CONNACK_NOT_AUTHORIZED = 5

# Upper bound on a single packet's declared "remaining length". Real traffic
# from the appliance never approaches this (its largest observed CONNECT is a
# few hundred bytes); it exists purely to stop a malformed or hostile peer
# from making decode_packets() wait forever to buffer a multi-hundred-megabyte
# "packet" the fixed header merely claims to be that long.
MAX_PACKET_BYTES = 65_536


class MqttDecodeError(ValueError):
    """Raised when bytes cannot be interpreted as an MQTT packet."""


@dataclass(slots=True)
class Packet:
    """Base for decoded packets; ``raw`` is the exact wire bytes."""

    packet_type: int
    raw: bytes = field(repr=False, default=b"")


@dataclass(slots=True)
class ConnectPacket(Packet):
    protocol_name: str = ""
    protocol_level: int = 0
    keepalive_seconds: int = 0
    client_id: str = ""
    clean_session: bool = True
    will_topic: str | None = None
    will_message: bytes | None = None
    will_qos: int = 0
    will_retain: bool = False
    username: str | None = None
    password: bytes | None = None


@dataclass(slots=True)
class ConnAckPacket(Packet):
    session_present: bool = False
    return_code: int = 0


@dataclass(slots=True)
class PublishPacket(Packet):
    topic: str = ""
    payload: bytes = b""
    qos: int = 0
    retain: bool = False
    dup: bool = False
    packet_id: int | None = None


@dataclass(slots=True)
class PubAckPacket(Packet):
    packet_id: int = 0


@dataclass(slots=True)
class SubscribePacket(Packet):
    packet_id: int = 0
    subscriptions: list[tuple[str, int]] = field(default_factory=list)


@dataclass(slots=True)
class SubAckPacket(Packet):
    packet_id: int = 0
    return_codes: list[int] = field(default_factory=list)


@dataclass(slots=True)
class UnsubscribePacket(Packet):
    packet_id: int = 0
    topics: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PingReqPacket(Packet):
    pass


@dataclass(slots=True)
class PingRespPacket(Packet):
    pass


@dataclass(slots=True)
class DisconnectPacket(Packet):
    pass


@dataclass(slots=True)
class UnknownPacket(Packet):
    """Any packet type we do not interpret; still forwardable via ``raw``."""

    body: bytes = b""


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def encode_remaining_length(length: int) -> bytes:
    """Encode the MQTT variable-length "remaining length" field."""
    if length < 0 or length > 268_435_455:
        raise ValueError("remaining length out of range")
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 0x80
        encoded.append(digit)
        if length == 0:
            return bytes(encoded)


def _decode_remaining_length(buffer: bytes, offset: int) -> tuple[int, int] | None:
    """Return (remaining_length, bytes_consumed) or None if more data is needed.

    Raises MqttDecodeError as soon as a 4th continuation-flagged byte is seen:
    MQTT limits this field to 4 bytes, so a 4th byte that still has its
    continuation bit set can never be valid and must not be treated as "need
    more data" (which would otherwise happily consume a 5th, 6th, ... byte).
    """
    multiplier = 1
    value = 0
    consumed = 0
    while True:
        if offset + consumed >= len(buffer):
            return None
        digit = buffer[offset + consumed]
        consumed += 1
        value += (digit & 0x7F) * multiplier
        if digit & 0x80 == 0:
            return value, consumed
        if consumed >= 4:
            raise MqttDecodeError("remaining length field longer than 4 bytes")
        multiplier *= 128


def _encode_string(text: str) -> bytes:
    data = text.encode("utf-8")
    return len(data).to_bytes(2, "big") + data


def _encode_bytes(data: bytes) -> bytes:
    return len(data).to_bytes(2, "big") + data


def _read_bytes(body: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(body):
        raise MqttDecodeError("truncated length prefix")
    length = int.from_bytes(body[offset : offset + 2], "big")
    end = offset + 2 + length
    if end > len(body):
        raise MqttDecodeError("truncated field")
    return body[offset + 2 : end], end


def _read_string(body: bytes, offset: int) -> tuple[str, int]:
    data, end = _read_bytes(body, offset)
    return data.decode("utf-8", errors="replace"), end


def _read_u8(body: bytes, offset: int, field_name: str) -> tuple[int, int]:
    """Bounds-checked single-byte read; raises MqttDecodeError, never IndexError."""
    if offset >= len(body):
        raise MqttDecodeError(f"truncated {field_name}")
    return body[offset], offset + 1


def _read_u16(body: bytes, offset: int, field_name: str) -> tuple[int, int]:
    """Bounds-checked big-endian 16-bit read; raises MqttDecodeError, never a silent short read.

    Plain slicing (``body[offset:offset+2]``) never raises even when fewer
    than 2 bytes remain -- it just returns a shorter ``bytes`` that
    ``int.from_bytes`` happily turns into a wrong, too-small integer. Every
    fixed-width field in this codec goes through this helper instead so
    truncation is always caught.
    """
    if offset + 2 > len(body):
        raise MqttDecodeError(f"truncated {field_name}")
    return int.from_bytes(body[offset : offset + 2], "big"), offset + 2


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def decode_packets(buffer: bytes, max_remaining_length: int = MAX_PACKET_BYTES) -> tuple[list[Packet], bytes]:
    """Decode every complete packet in ``buffer``.

    Returns the packets and whatever trailing bytes belong to an incomplete
    packet (to be prepended to the next read). WebSocket framing normally
    delivers whole packets, but the codec stays stream-safe regardless.

    Raises MqttDecodeError if a fixed header declares a remaining length
    greater than ``max_remaining_length`` -- checked as soon as the header is
    parsed, before waiting for that many bytes to arrive, so a peer cannot
    stall us into buffering an unbounded "packet" it merely claims is huge.
    """
    packets: list[Packet] = []
    offset = 0
    while offset < len(buffer):
        header = _decode_remaining_length(buffer, offset + 1)
        if header is None:
            break
        remaining_length, length_bytes = header
        if remaining_length > max_remaining_length:
            raise MqttDecodeError(
                f"remaining length {remaining_length} exceeds the "
                f"{max_remaining_length}-byte limit"
            )
        body_start = offset + 1 + length_bytes
        body_end = body_start + remaining_length
        if body_end > len(buffer):
            break
        first_byte = buffer[offset]
        raw = bytes(buffer[offset:body_end])
        body = raw[1 + length_bytes :]
        packets.append(_decode_one(first_byte, body, raw))
        offset = body_end
    return packets, bytes(buffer[offset:])


def _decode_one(first_byte: int, body: bytes, raw: bytes) -> Packet:
    packet_type = first_byte >> 4
    flags = first_byte & 0x0F

    if packet_type == CONNECT:
        return _decode_connect(body, raw)
    if packet_type == CONNACK:
        flags_byte, offset = _read_u8(body, 0, "CONNACK flags")
        return_code, offset = _read_u8(body, offset, "CONNACK return code")
        return ConnAckPacket(CONNACK, raw, session_present=bool(flags_byte & 1), return_code=return_code)
    if packet_type == PUBLISH:
        return _decode_publish(flags, body, raw)
    if packet_type == PUBACK:
        packet_id, _offset = _read_u16(body, 0, "PUBACK packet id")
        return PubAckPacket(PUBACK, raw, packet_id=packet_id)
    if packet_type == SUBSCRIBE:
        return _decode_subscribe(body, raw)
    if packet_type == SUBACK:
        packet_id, offset = _read_u16(body, 0, "SUBACK packet id")
        return SubAckPacket(SUBACK, raw, packet_id=packet_id, return_codes=list(body[offset:]))
    if packet_type == UNSUBSCRIBE:
        return _decode_unsubscribe(body, raw)
    if packet_type == PINGREQ:
        return PingReqPacket(PINGREQ, raw)
    if packet_type == PINGRESP:
        return PingRespPacket(PINGRESP, raw)
    if packet_type == DISCONNECT:
        return DisconnectPacket(DISCONNECT, raw)
    return UnknownPacket(packet_type, raw, body=body)


def _decode_connect(body: bytes, raw: bytes) -> ConnectPacket:
    offset = 0
    protocol_name, offset = _read_string(body, offset)
    protocol_level, offset = _read_u8(body, offset, "CONNECT protocol level")
    connect_flags, offset = _read_u8(body, offset, "CONNECT flags")
    keepalive, offset = _read_u16(body, offset, "CONNECT keepalive")

    has_username = bool(connect_flags & 0x80)
    has_password = bool(connect_flags & 0x40)
    will_retain = bool(connect_flags & 0x20)
    will_qos = (connect_flags >> 3) & 0x03
    has_will = bool(connect_flags & 0x04)
    clean_session = bool(connect_flags & 0x02)

    client_id, offset = _read_string(body, offset)
    will_topic = will_message = None
    if has_will:
        will_topic, offset = _read_string(body, offset)
        will_message, offset = _read_bytes(body, offset)
    username = password = None
    if has_username:
        username, offset = _read_string(body, offset)
    if has_password:
        password, offset = _read_bytes(body, offset)

    return ConnectPacket(
        CONNECT,
        raw,
        protocol_name=protocol_name,
        protocol_level=protocol_level,
        keepalive_seconds=keepalive,
        client_id=client_id,
        clean_session=clean_session,
        will_topic=will_topic,
        will_message=will_message,
        will_qos=will_qos,
        will_retain=will_retain,
        username=username,
        password=password,
    )


def _decode_publish(flags: int, body: bytes, raw: bytes) -> PublishPacket:
    qos = (flags >> 1) & 0x03
    if qos == 3:
        # QoS 3 does not exist in MQTT; the two QoS bits being both set is
        # explicitly reserved/invalid, not "QoS 2 that we don't support".
        raise MqttDecodeError("PUBLISH declares invalid QoS 3")
    topic, offset = _read_string(body, 0)
    packet_id = None
    if qos > 0:
        packet_id, offset = _read_u16(body, offset, "PUBLISH packet id")
    return PublishPacket(
        PUBLISH,
        raw,
        topic=topic,
        payload=body[offset:],
        qos=qos,
        retain=bool(flags & 0x01),
        dup=bool(flags & 0x08),
        packet_id=packet_id,
    )


def _decode_subscribe(body: bytes, raw: bytes) -> SubscribePacket:
    packet_id, offset = _read_u16(body, 0, "SUBSCRIBE packet id")
    subscriptions: list[tuple[str, int]] = []
    while offset < len(body):
        topic, offset = _read_string(body, offset)
        requested_qos, offset = _read_u8(body, offset, "SUBSCRIBE requested QoS")
        subscriptions.append((topic, requested_qos & 0x03))
    return SubscribePacket(SUBSCRIBE, raw, packet_id=packet_id, subscriptions=subscriptions)


def _decode_unsubscribe(body: bytes, raw: bytes) -> UnsubscribePacket:
    packet_id, offset = _read_u16(body, 0, "UNSUBSCRIBE packet id")
    topics: list[str] = []
    while offset < len(body):
        topic, offset = _read_string(body, offset)
        topics.append(topic)
    return UnsubscribePacket(UNSUBSCRIBE, raw, packet_id=packet_id, topics=topics)


# --------------------------------------------------------------------------- #
# Encoding -- broker-role responses (what we send the appliance)
# --------------------------------------------------------------------------- #
def _frame(first_byte: int, body: bytes) -> bytes:
    return bytes([first_byte]) + encode_remaining_length(len(body)) + body


def encode_connack(return_code: int = CONNACK_ACCEPTED, session_present: bool = False) -> bytes:
    return _frame(CONNACK << 4, bytes([1 if session_present else 0, return_code]))


def encode_suback(packet_id: int, granted_qos: list[int]) -> bytes:
    return _frame(SUBACK << 4, packet_id.to_bytes(2, "big") + bytes(granted_qos))


def encode_unsuback(packet_id: int) -> bytes:
    return _frame(UNSUBACK << 4, packet_id.to_bytes(2, "big"))


def encode_puback(packet_id: int) -> bytes:
    return _frame(PUBACK << 4, packet_id.to_bytes(2, "big"))


def encode_pingresp() -> bytes:
    return _frame(PINGRESP << 4, b"")


def encode_publish(topic: str, payload: bytes, qos: int = 1, packet_id: int | None = None, retain: bool = False) -> bytes:
    """Encode a PUBLISH; QoS>0 requires a packet id."""
    if qos > 0 and packet_id is None:
        raise ValueError("QoS>0 PUBLISH needs a packet_id")
    flags = (qos << 1) | (1 if retain else 0)
    body = _encode_string(topic)
    if qos > 0:
        body += packet_id.to_bytes(2, "big")  # type: ignore[union-attr]
    body += payload
    return _frame((PUBLISH << 4) | flags, body)


# --------------------------------------------------------------------------- #
# Encoding -- client-role requests (what the appliance sends us)
#
# The bridge never needs to originate these against a real appliance, but a
# fake-appliance test harness and the decoder round-trip tests below use them
# to build well-formed traffic from the other direction.
# --------------------------------------------------------------------------- #
def encode_connect(
    client_id: str,
    *,
    keepalive_seconds: int = 30,
    protocol_name: str = "MQIsdp",
    protocol_level: int = 3,
    clean_session: bool = True,
    username: str | None = None,
    password: bytes | None = None,
    will_topic: str | None = None,
    will_message: bytes | None = None,
    will_qos: int = 0,
    will_retain: bool = False,
) -> bytes:
    has_will = will_topic is not None
    connect_flags = 0
    if username is not None:
        connect_flags |= 0x80
    if password is not None:
        connect_flags |= 0x40
    if has_will:
        if will_retain:
            connect_flags |= 0x20
        connect_flags |= (will_qos & 0x03) << 3
        connect_flags |= 0x04
    if clean_session:
        connect_flags |= 0x02

    body = _encode_string(protocol_name)
    body += bytes([protocol_level, connect_flags])
    body += keepalive_seconds.to_bytes(2, "big")
    body += _encode_string(client_id)
    if has_will:
        body += _encode_string(will_topic)  # type: ignore[arg-type]
        body += _encode_bytes(will_message or b"")
    if username is not None:
        body += _encode_string(username)
    if password is not None:
        body += _encode_bytes(password)
    return _frame(CONNECT << 4, body)


def encode_subscribe(packet_id: int, subscriptions: list[tuple[str, int]]) -> bytes:
    body = packet_id.to_bytes(2, "big")
    for topic, requested_qos in subscriptions:
        body += _encode_string(topic) + bytes([requested_qos & 0x03])
    # The SUBSCRIBE fixed header's lower nibble is reserved and fixed at 0010.
    return _frame((SUBSCRIBE << 4) | 0x02, body)


def encode_unsubscribe(packet_id: int, topics: list[str]) -> bytes:
    body = packet_id.to_bytes(2, "big")
    for topic in topics:
        body += _encode_string(topic)
    # The UNSUBSCRIBE fixed header's lower nibble is reserved and fixed at 0010.
    return _frame((UNSUBSCRIBE << 4) | 0x02, body)


def encode_pingreq() -> bytes:
    return _frame(PINGREQ << 4, b"")


def encode_disconnect() -> bytes:
    return _frame(DISCONNECT << 4, b"")
