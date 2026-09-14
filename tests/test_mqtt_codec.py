"""Codec tests using frames shaped exactly like a real appliance session.

The layouts, flags and lengths mirror bytes captured from a real unit; only
the identifying fields (serial, MQTT credentials) are replaced with
placeholders of identical length, so every remaining-length value is the same
as on the wire.

Also covers the hardening added for pre-publish review: bounds-checked
fixed-width reads (every truncation point of a real frame must raise
MqttDecodeError, never IndexError/ValueError), a cap on declared remaining
length, rejection of an over-long remaining-length field, the new
UNSUBSCRIBE/UNSUBACK support, the new client-side encoders, and a seeded
fuzz sweep asserting only MqttDecodeError ever escapes decode_packets().
"""

import random

import pytest

from zafro_bridge import mqtt_codec

# Placeholders with the same lengths as the real values (27-char serial, 40-hex creds).
SERIAL = "6ISEComboWF140ASJ0000000000"
USERNAME = "0123456789abcdef0123456789abcdef01234567"
PASSWORD = b"fedcba9876543210fedcba9876543210fedcba98"

CLIENT_ID = f"dev_{SERIAL}"
WILL_TOPIC = f"lwt/I4SEASON/{SERIAL}"
WILL_MESSAGE = f'{{\n\t"cmd":\t1,\n\t"sn":\t"{SERIAL}",\n\t"status":\tfalse\n}}'.encode()
ONLINE_MESSAGE = f'{{\n\t"cmd":\t1,\n\t"sn":\t"{SERIAL}",\n\t"status":\ttrue\n}}'.encode()
COMMAND_REQUEST_TOPIC = f"dev/I4SEASON/{SERIAL}/command/request"

CONNACK_HEX = "20020000"
SUBACK_HEX = "9003000201"

# PINGREQ's fixed header first byte (type 12, no flags); its body, if any, is
# ignored by the decoder, which makes it a convenient no-op packet type for
# tests that only care about framing (remaining-length limits, etc).
PINGREQ_FIRST_BYTE = mqtt_codec.PINGREQ << 4


def _s(text: str) -> bytes:
    data = text.encode()
    return len(data).to_bytes(2, "big") + data


def _b(data: bytes) -> bytes:
    return len(data).to_bytes(2, "big") + data


def build_device_connect() -> bytes:
    """MQTT 3.1 CONNECT exactly as the appliance sends it: MQIsdp v3, flags 0xce
    (username, password, will QoS 1, clean session), keepalive 30s."""
    body = _s("MQIsdp") + bytes([0x03, 0xCE]) + (30).to_bytes(2, "big")
    body += _s(CLIENT_ID) + _s(WILL_TOPIC) + _b(WILL_MESSAGE) + _s(USERNAME) + _b(PASSWORD)
    return bytes([0x10]) + mqtt_codec.encode_remaining_length(len(body)) + body


def build_device_subscribe() -> bytes:
    body = (2).to_bytes(2, "big") + _s(COMMAND_REQUEST_TOPIC) + bytes([0x01])
    return bytes([0x82]) + mqtt_codec.encode_remaining_length(len(body)) + body


def build_device_online_publish() -> bytes:
    body = _s(WILL_TOPIC) + (4).to_bytes(2, "big") + ONLINE_MESSAGE
    return bytes([0x32]) + mqtt_codec.encode_remaining_length(len(body)) + body


def _decode_one(frame: bytes):
    packets, remainder = mqtt_codec.decode_packets(frame)
    assert remainder == b""
    assert len(packets) == 1
    return packets[0]


def test_decode_device_shaped_connect():
    frame = build_device_connect()
    assert len(frame) == 245 and frame[1:3] == b"\xf2\x01"  # remaining length 242, as captured
    packet = _decode_one(frame)
    assert isinstance(packet, mqtt_codec.ConnectPacket)
    assert packet.protocol_name == "MQIsdp"
    assert packet.protocol_level == 3
    assert packet.keepalive_seconds == 30
    assert packet.client_id == CLIENT_ID
    assert packet.clean_session is True
    assert packet.will_topic == WILL_TOPIC
    assert packet.will_qos == 1 and packet.will_retain is False
    assert packet.will_message == WILL_MESSAGE
    assert packet.username == USERNAME
    assert packet.password == PASSWORD
    assert packet.raw == frame


def test_decode_device_shaped_subscribe():
    frame = build_device_subscribe()
    assert frame[1] == 0x3D  # remaining length 61, as captured
    packet = _decode_one(frame)
    assert isinstance(packet, mqtt_codec.SubscribePacket)
    assert packet.packet_id == 2
    assert packet.subscriptions == [(COMMAND_REQUEST_TOPIC, 1)]


def test_decode_device_shaped_online_publish():
    frame = build_device_online_publish()
    assert frame[1] == 0x70  # remaining length 112, as captured
    packet = _decode_one(frame)
    assert isinstance(packet, mqtt_codec.PublishPacket)
    assert packet.qos == 1 and packet.retain is False and packet.dup is False
    assert packet.packet_id == 4
    assert packet.topic == WILL_TOPIC
    assert packet.payload == ONLINE_MESSAGE


def test_decode_connack_and_suback_from_cloud():
    connack = _decode_one(bytes.fromhex(CONNACK_HEX))
    assert isinstance(connack, mqtt_codec.ConnAckPacket) and connack.return_code == 0
    suback = _decode_one(bytes.fromhex(SUBACK_HEX))
    assert isinstance(suback, mqtt_codec.SubAckPacket)
    assert suback.packet_id == 2 and suback.return_codes == [1]


def test_encoders_match_cloud_bytes():
    assert mqtt_codec.encode_connack(0) == bytes.fromhex(CONNACK_HEX)
    assert mqtt_codec.encode_suback(2, [1]) == bytes.fromhex(SUBACK_HEX)
    assert mqtt_codec.encode_puback(4) == bytes.fromhex("40020004")
    assert mqtt_codec.encode_pingresp() == bytes.fromhex("d000")


def test_pingreq_decodes():
    packet = _decode_one(bytes.fromhex("c000"))
    assert isinstance(packet, mqtt_codec.PingReqPacket)


def test_publish_roundtrip_and_injected_packet_id():
    frame = mqtt_codec.encode_publish(COMMAND_REQUEST_TOPIC, b'{"cmd":6}', qos=1, packet_id=60001)
    packet = _decode_one(frame)
    assert isinstance(packet, mqtt_codec.PublishPacket)
    assert packet.packet_id == 60001
    assert packet.topic == COMMAND_REQUEST_TOPIC
    assert packet.payload == b'{"cmd":6}'


def test_stream_safety_with_partial_and_concatenated_frames():
    stream = bytes.fromhex(CONNACK_HEX + SUBACK_HEX)
    packets, remainder = mqtt_codec.decode_packets(stream[:5])  # CONNACK + 1 byte of SUBACK
    assert len(packets) == 1 and remainder == stream[4:5]
    packets, remainder = mqtt_codec.decode_packets(remainder + stream[5:])
    assert len(packets) == 1 and remainder == b""


def test_remaining_length_encoding():
    assert mqtt_codec.encode_remaining_length(0) == b"\x00"
    assert mqtt_codec.encode_remaining_length(127) == b"\x7f"
    assert mqtt_codec.encode_remaining_length(128) == b"\x80\x01"
    assert mqtt_codec.encode_remaining_length(242) == b"\xf2\x01"


# --------------------------------------------------------------------------- #
# Truncation: every prefix of a well-formed frame must either decode nothing
# yet (still waiting for more bytes) or raise MqttDecodeError -- never any
# other exception (IndexError, ValueError from int.from_bytes, etc).
# --------------------------------------------------------------------------- #
def _assert_every_truncation_is_safe(frame: bytes) -> None:
    for cut in range(len(frame)):
        prefix = frame[:cut]
        try:
            packets, remainder = mqtt_codec.decode_packets(prefix)
        except mqtt_codec.MqttDecodeError:
            continue  # acceptable: truncated input recognized as malformed
        # Not yet an error: must simply report "nothing decoded, all pending".
        assert packets == []
        assert remainder == prefix


def test_truncated_connect_is_always_safe():
    _assert_every_truncation_is_safe(build_device_connect())


def test_truncated_subscribe_is_always_safe():
    _assert_every_truncation_is_safe(build_device_subscribe())


def test_truncated_publish_is_always_safe():
    _assert_every_truncation_is_safe(build_device_online_publish())


def test_truncated_unsubscribe_is_always_safe():
    frame = mqtt_codec.encode_unsubscribe(7, [COMMAND_REQUEST_TOPIC, WILL_TOPIC])
    _assert_every_truncation_is_safe(frame)


# --------------------------------------------------------------------------- #
# Bounds-checking of individual fixed-width reads that truncation alone might
# not exercise cleanly (e.g. a remaining length that promises more body than
# a fixed-width field needs, but the field's own bytes are still missing is
# already covered above; these pin specific documented cases).
# --------------------------------------------------------------------------- #
def test_publish_qos3_is_rejected_as_malformed():
    # flags nibble 0x06 => qos bits 11 (3), which MQTT reserves as invalid.
    body = _s(COMMAND_REQUEST_TOPIC) + b"\x00\x01" + b"{}"
    frame = bytes([0x36]) + mqtt_codec.encode_remaining_length(len(body)) + body
    with pytest.raises(mqtt_codec.MqttDecodeError):
        mqtt_codec.decode_packets(frame)


def test_puback_needs_two_bytes():
    with pytest.raises(mqtt_codec.MqttDecodeError):
        mqtt_codec.decode_packets(bytes([0x40, 0x01, 0x00]))  # PUBACK, 1-byte body


def test_suback_needs_packet_id_bytes():
    with pytest.raises(mqtt_codec.MqttDecodeError):
        mqtt_codec.decode_packets(bytes([0x90, 0x01, 0x00]))  # SUBACK, 1-byte body


def test_connack_needs_two_bytes():
    with pytest.raises(mqtt_codec.MqttDecodeError):
        mqtt_codec.decode_packets(bytes([0x20, 0x01, 0x00]))  # CONNACK, 1-byte body


# --------------------------------------------------------------------------- #
# Remaining-length hardening
# --------------------------------------------------------------------------- #
def test_remaining_length_of_five_bytes_is_rejected():
    # Five continuation-flagged-then-terminated bytes: not a valid MQTT
    # remaining length (max 4 bytes) even though each individual byte is
    # well-formed. Must be rejected as soon as the 4th byte is still
    # continuation-flagged, without reading a 5th.
    malformed_header = bytes([0x10, 0xFF, 0xFF, 0xFF, 0xFF, 0x01])
    with pytest.raises(mqtt_codec.MqttDecodeError):
        mqtt_codec.decode_packets(malformed_header)


def test_oversize_remaining_length_is_rejected():
    # A legitimate 4-byte-max-encodable remaining length (16 MB+) that is
    # nowhere near actually present: must be rejected immediately rather
    # than waiting forever for that many bytes to arrive.
    header = bytes([0x30]) + mqtt_codec.encode_remaining_length(1_000_000) + b"only a few bytes"
    with pytest.raises(mqtt_codec.MqttDecodeError):
        mqtt_codec.decode_packets(header)


def test_remaining_length_limit_is_configurable():
    # A caller-supplied lower cap is honored even when the default would
    # have allowed the packet through. PINGREQ's body (if any) is ignored by
    # the decoder, so padding it is a clean way to test the length gate in
    # isolation from any packet-specific body validation.
    body = b"x" * 100
    frame = bytes([PINGREQ_FIRST_BYTE]) + mqtt_codec.encode_remaining_length(len(body)) + body
    # Under the default cap this decodes fine...
    packets, remainder = mqtt_codec.decode_packets(frame)
    assert len(packets) == 1 and remainder == b""
    # ...but a tighter cap rejects the same frame.
    with pytest.raises(mqtt_codec.MqttDecodeError):
        mqtt_codec.decode_packets(frame, max_remaining_length=50)


# --------------------------------------------------------------------------- #
# UNSUBSCRIBE / UNSUBACK
# --------------------------------------------------------------------------- #
def test_unsubscribe_decodes():
    frame = mqtt_codec.encode_unsubscribe(9, [COMMAND_REQUEST_TOPIC, WILL_TOPIC])
    packet = _decode_one(frame)
    assert isinstance(packet, mqtt_codec.UnsubscribePacket)
    assert packet.packet_id == 9
    assert packet.topics == [COMMAND_REQUEST_TOPIC, WILL_TOPIC]
    # Reserved fixed-header nibble must be 0010 per the MQTT spec.
    assert frame[0] == (mqtt_codec.UNSUBSCRIBE << 4) | 0x02


def test_encode_unsuback():
    assert mqtt_codec.encode_unsuback(9) == bytes.fromhex("b0020009")


# --------------------------------------------------------------------------- #
# New CONNACK constants
# --------------------------------------------------------------------------- #
def test_connack_return_code_constants():
    assert mqtt_codec.CONNACK_ACCEPTED == 0
    assert mqtt_codec.CONNACK_UNACCEPTABLE_PROTOCOL == 1
    assert mqtt_codec.CONNACK_IDENTIFIER_REJECTED == 2
    assert mqtt_codec.CONNACK_NOT_AUTHORIZED == 5


# --------------------------------------------------------------------------- #
# Client-side encoders: round-trip through the real decoders.
# --------------------------------------------------------------------------- #
def test_encode_connect_roundtrip_minimal():
    frame = mqtt_codec.encode_connect("dev_" + SERIAL, keepalive_seconds=45)
    packet = _decode_one(frame)
    assert isinstance(packet, mqtt_codec.ConnectPacket)
    assert packet.protocol_name == "MQIsdp"
    assert packet.protocol_level == 3
    assert packet.client_id == "dev_" + SERIAL
    assert packet.keepalive_seconds == 45
    assert packet.clean_session is True
    assert packet.will_topic is None
    assert packet.username is None
    assert packet.password is None


def test_encode_connect_roundtrip_full():
    frame = mqtt_codec.encode_connect(
        CLIENT_ID,
        keepalive_seconds=30,
        clean_session=False,
        username=USERNAME,
        password=PASSWORD,
        will_topic=WILL_TOPIC,
        will_message=WILL_MESSAGE,
        will_qos=1,
        will_retain=True,
    )
    packet = _decode_one(frame)
    assert isinstance(packet, mqtt_codec.ConnectPacket)
    assert packet.client_id == CLIENT_ID
    assert packet.clean_session is False
    assert packet.will_topic == WILL_TOPIC
    assert packet.will_message == WILL_MESSAGE
    assert packet.will_qos == 1
    assert packet.will_retain is True
    assert packet.username == USERNAME
    assert packet.password == PASSWORD


def test_encode_connect_matches_device_shaped_bytes():
    # Re-encoding the same fields the real appliance sends must reproduce the
    # exact captured-shape frame built by build_device_connect().
    frame = mqtt_codec.encode_connect(
        CLIENT_ID,
        keepalive_seconds=30,
        clean_session=True,
        username=USERNAME,
        password=PASSWORD,
        will_topic=WILL_TOPIC,
        will_message=WILL_MESSAGE,
        will_qos=1,
        will_retain=False,
    )
    assert frame == build_device_connect()


def test_encode_subscribe_roundtrip():
    frame = mqtt_codec.encode_subscribe(5, [(COMMAND_REQUEST_TOPIC, 1)])
    packet = _decode_one(frame)
    assert isinstance(packet, mqtt_codec.SubscribePacket)
    assert packet.packet_id == 5
    assert packet.subscriptions == [(COMMAND_REQUEST_TOPIC, 1)]


def test_encode_subscribe_matches_device_shaped_bytes():
    assert mqtt_codec.encode_subscribe(2, [(COMMAND_REQUEST_TOPIC, 1)]) == build_device_subscribe()


def test_encode_pingreq_and_disconnect():
    assert mqtt_codec.encode_pingreq() == bytes.fromhex("c000")
    ping = _decode_one(mqtt_codec.encode_pingreq())
    assert isinstance(ping, mqtt_codec.PingReqPacket)

    assert mqtt_codec.encode_disconnect() == bytes.fromhex("e000")
    disconnect = _decode_one(mqtt_codec.encode_disconnect())
    assert isinstance(disconnect, mqtt_codec.DisconnectPacket)


# --------------------------------------------------------------------------- #
# Seeded fuzz sweep: thousands of pseudo-random buffers, some built by
# mutating well-formed frames and some pure noise. The only contract is that
# decode_packets() never raises anything other than MqttDecodeError.
# --------------------------------------------------------------------------- #
def _mutate(rng: random.Random, frame: bytes) -> bytes:
    data = bytearray(frame)
    mutation_kind = rng.randrange(4)
    if mutation_kind == 0 and data:
        # Flip random bytes.
        for _ in range(rng.randrange(1, 4)):
            data[rng.randrange(len(data))] = rng.randrange(256)
    elif mutation_kind == 1 and len(data) > 1:
        # Truncate at a random point.
        del data[rng.randrange(1, len(data)) :]
    elif mutation_kind == 2:
        # Append random noise.
        data += bytes(rng.randrange(256) for _ in range(rng.randrange(0, 8)))
    else:
        # Pure random noise of a random length, unrelated to any real frame.
        data = bytearray(rng.randrange(256) for _ in range(rng.randrange(0, 16)))
    return bytes(data)


def test_fuzz_decode_packets_never_raises_unexpected_exceptions():
    rng = random.Random(0xC0FFEE)
    seed_frames = [
        build_device_connect(),
        build_device_subscribe(),
        build_device_online_publish(),
        mqtt_codec.encode_unsubscribe(1, [COMMAND_REQUEST_TOPIC]),
        mqtt_codec.encode_connack(0),
        mqtt_codec.encode_suback(2, [1]),
        mqtt_codec.encode_puback(4),
        mqtt_codec.encode_pingresp(),
        mqtt_codec.encode_pingreq(),
        mqtt_codec.encode_disconnect(),
    ]
    for _ in range(4000):
        base = seed_frames[rng.randrange(len(seed_frames))]
        candidate = _mutate(rng, base)
        try:
            mqtt_codec.decode_packets(candidate)
        except mqtt_codec.MqttDecodeError:
            pass  # the only exception this API is allowed to raise
