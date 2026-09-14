"""Home Assistant side: talk to the Mosquitto broker (discovery, state, commands).

Runs a reconnecting client. Publishes retained discovery/state/availability so
HA rebuilds instantly after restarts, and subscribes to ``<prefix>/+/set/+`` to
receive commands, which it hands to the bridge as (serial, command, payload).

The client also carries a bridge-wide last will (see ``ha_discovery.
bridge_availability_topic``): if this process dies without a clean shutdown,
the broker publishes "offline" on that topic on our behalf, and every
entity's discovery lists it as one of two availability topics (the other
being the per-device one) so the whole device goes unavailable together with
the add-on itself, not just when the appliance drops its own connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiomqtt

from . import ha_discovery
from .config import Settings
from .state import DeviceState

_LOGGER = logging.getLogger(__name__)

OnCommandCallback = Callable[[str, str, str], Awaitable[None]]  # (serial, command, payload)
OnConnectedCallback = Callable[[], Awaitable[None]]

_RECONNECT_DELAY_SECONDS = 5
# How long to wait for the "offline" last-will publish to land on a graceful
# shutdown before giving up and letting the connection close anyway - a slow
# or wedged broker must never turn a clean shutdown into a hang.
_SHUTDOWN_PUBLISH_TIMEOUT_SECONDS = 2.0
# Bounds every other publish. Bug found via the end-to-end DOCS.md log capture:
# when a device disconnects during the bridge's own shutdown, the disconnect
# callback's publish_availability() call can land on a connection that is
# already mid-teardown; the underlying client then blocks on its own,
# much longer internal timeout (~10s observed) waiting for a PUBACK that will
# never come, needlessly turning a near-instant shutdown into a ten-second
# one. A healthy connection publishes in milliseconds, so this bound is never
# felt in normal operation.
_PUBLISH_TIMEOUT_SECONDS = 3.0

_ONLINE = "online"
_OFFLINE = "offline"


class HomeAssistantMqtt:
    """Publisher/subscriber for everything Home Assistant sees."""

    def __init__(self, settings: Settings, on_command: OnCommandCallback, on_connected: OnConnectedCallback | None = None) -> None:
        self._settings = settings
        self._on_command = on_command
        self._on_connected = on_connected
        self._client: aiomqtt.Client | None = None
        self._connected = asyncio.Event()
        self._bridge_availability_topic = ha_discovery.bridge_availability_topic(settings.state_topic_prefix)

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Maintain the broker connection forever; deliver inbound commands.

        On every fresh connection: publish the bridge "online" (retained),
        subscribe to commands, then (if given) await ``on_connected`` so the
        bridge can republish discovery/availability/state for everything it
        already knows about before the first inbound command could arrive.
        """
        command_filter = f"{self._settings.state_topic_prefix}/+/set/+"
        while True:
            try:
                async with self._make_client() as client:
                    self._client = client
                    _LOGGER.info("Connected to MQTT broker %s:%s", self._settings.mqtt_host, self._settings.mqtt_port)
                    await self._publish(self._bridge_availability_topic, _ONLINE, retain=True)
                    await client.subscribe(command_filter, qos=1)
                    self._connected.set()
                    if self._on_connected is not None:
                        try:
                            await self._on_connected()
                        except Exception:  # noqa: BLE001 - a republish bug must not end the MQTT loop
                            _LOGGER.exception("Republishing after the MQTT broker connected failed")
                    try:
                        async for message in client.messages:
                            await self._dispatch(message)
                    except asyncio.CancelledError:
                        # Graceful shutdown: tell HA we're going away before the
                        # `async with` block disconnects out from under us -
                        # after this point the broker's own last will would
                        # publish the same thing anyway, but doing it ourselves
                        # is instant instead of waiting for the broker's
                        # keepalive-based will delivery.
                        await self._publish_offline_best_effort()
                        raise
            except aiomqtt.MqttError as error:
                _LOGGER.warning("MQTT connection lost (%s); retrying in %ss", error, _RECONNECT_DELAY_SECONDS)
            finally:
                self._connected.clear()
                self._client = None
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)

    def _make_client(self) -> aiomqtt.Client:
        tls_params = aiomqtt.TLSParameters() if self._settings.mqtt_use_tls else None
        will = aiomqtt.Will(topic=self._bridge_availability_topic, payload=_OFFLINE, qos=1, retain=True)
        return aiomqtt.Client(
            hostname=self._settings.mqtt_host,
            port=self._settings.mqtt_port,
            username=self._settings.mqtt_username,
            password=self._settings.mqtt_password,
            tls_params=tls_params,
            identifier=f"zafro-bridge-{id(self) & 0xFFFF:04x}",
            keepalive=60,
            will=will,
        )

    async def _publish_offline_best_effort(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            await asyncio.wait_for(
                client.publish(self._bridge_availability_topic, _OFFLINE, qos=1, retain=True),
                timeout=_SHUTDOWN_PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - shutting down regardless; this is best-effort
            _LOGGER.debug("Could not publish bridge offline before shutdown", exc_info=True)

    async def _dispatch(self, message: aiomqtt.Message) -> None:
        # topic: <prefix>/<serial>/set/<command>
        parts = str(message.topic).split("/")
        if len(parts) != 4 or parts[2] != "set":
            return
        serial, command = parts[1], parts[3]
        payload = message.payload.decode("utf-8", errors="replace") if isinstance(message.payload, (bytes, bytearray)) else str(message.payload)
        _LOGGER.debug("HA command %s %s=%r", serial, command, payload)
        try:
            await self._on_command(serial, command, payload)
        except Exception:  # noqa: BLE001 - a command callback must never end the MQTT loop
            _LOGGER.exception("[%s] on_command callback raised for %s=%r", serial, command, payload)

    # ------------------------------------------------------------------ #
    async def _publish(self, topic: str, payload: str | bytes, *, retain: bool) -> None:
        """Publish if connected; silently skip otherwise (state is republished on change).

        Bounded by ``_PUBLISH_TIMEOUT_SECONDS`` so a publish that lands on a
        connection already going away (see that constant's comment) fails
        fast instead of hanging on the underlying client's own, much longer
        internal timeout.
        """
        client = self._client
        if client is None:
            _LOGGER.debug("MQTT not connected; dropping publish to %s", topic)
            return
        try:
            await asyncio.wait_for(client.publish(topic, payload, qos=1, retain=retain), timeout=_PUBLISH_TIMEOUT_SECONDS)
        except aiomqtt.MqttError as error:
            _LOGGER.warning("MQTT publish to %s failed: %s", topic, error)
        except TimeoutError:
            _LOGGER.warning("MQTT publish to %s timed out after %.0fs", topic, _PUBLISH_TIMEOUT_SECONDS)

    def _base(self, serial: str) -> str:
        return ha_discovery.device_base_topic(self._settings.state_topic_prefix, serial)

    async def publish_discovery(self, device: DeviceState) -> None:
        payload = ha_discovery.build_discovery_payload(device, self._settings.state_topic_prefix, self._settings.bridge_version)
        topic = ha_discovery.discovery_topic(self._settings.discovery_prefix, device.serial_number)
        await self._publish(topic, json.dumps(payload), retain=True)
        _LOGGER.info("Published discovery for %s (%s)", device.serial_number, device.display_name)

    async def publish_availability(self, serial: str, online: bool) -> None:
        await self._publish(f"{self._base(serial)}/availability", _ONLINE if online else _OFFLINE, retain=True)

    async def publish_state(self, serial: str, ha_state: dict[str, Any]) -> None:
        await self._publish(f"{self._base(serial)}/state", json.dumps(ha_state), retain=True)
