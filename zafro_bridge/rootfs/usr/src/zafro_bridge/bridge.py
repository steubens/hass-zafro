"""Orchestrator: wires device sessions to Home Assistant and keeps state fresh.

Responsibilities:
* track one DeviceState + (at most one) DeviceSession per serial number,
* on device connect: announce it to HA (once we have something worth
  announcing) and ask it for a full snapshot + info,
* on device publish: merge into state and republish to HA only on change,
* on HA command: translate and inject into the device's session, without
  ever letting a bad command or a device-side failure end the MQTT loop,
* periodically re-request snapshot/info (the device won't volunteer them),
* republish everything Home Assistant needs to see after the MQTT broker
  connection itself is (re)established.
"""

from __future__ import annotations

import asyncio
import logging

from . import protocol
from .config import Settings
from .device_server import DeviceServer
from .device_session import DeviceSession
from .ha_mqtt import HomeAssistantMqtt
from .state import DeviceState

_LOGGER = logging.getLogger(__name__)

# Give the device a moment after CONNECT/SUBSCRIBE before we start asking it things.
_INITIAL_REQUEST_DELAY_SECONDS = 1.5

# How long to wait for a cmd-5 device-info reply after CONNECT before publishing
# discovery anyway with placeholder device details. Without this fallback, a
# device whose info reply is lost or slow would never appear in Home Assistant.
_DISCOVERY_FALLBACK_SECONDS = 15.0


def _discovery_identity(state: DeviceState) -> tuple[object, ...]:
    """The device-info fields that appear in the discovery payload's device card.

    Frequently changing info such as Wi-Fi signal lives in the state document,
    so it is deliberately excluded here.
    """
    return tuple(
        state.info.get(key) for key in (protocol.INFO_PRODUCT, protocol.INFO_MODULE_FIRMWARE, protocol.INFO_MCU_PART)
    )


class Bridge:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._states: dict[str, DeviceState] = {}
        self._sessions: dict[str, DeviceSession] = {}
        # Serial -> the device-card details last published in its (retained)
        # discovery payload. Discovery is re-published only when those details
        # change or the broker reconnects - not on every Wi-Fi signal refresh -
        # and membership doubles as "don't wait for cmd 5 again".
        self._published_discovery_identity: dict[str, tuple[object, ...]] = {}
        # One-time-per-serial guard for the "only Fahrenheit is supported" warning.
        self._tempunit_warned: set[str] = set()
        # Fire-and-forget tasks (initial requests, discovery fallback timers) so
        # they can be cancelled cleanly on shutdown and a bug in one can't
        # disappear silently.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Set once run()'s own shutdown begins. Bug found via the end-to-end
        # DOCS.md log capture: closing every device session here (below) makes
        # each one's on_disconnected fire on its own task, and by then the HA
        # MQTT client is already mid-teardown (or gone) - a per-device
        # publish_availability() attempted against it was observed to hang for
        # several seconds waiting on a PUBACK that will never arrive, needlessly
        # slowing every shutdown that has a device connected. The bridge-level
        # availability topic's own last-will already covers "everything is
        # unavailable" in this case, so this flag just skips the redundant,
        # doomed per-device publish once we're already on our way out.
        self._shutting_down = False

        self._ha = HomeAssistantMqtt(settings, on_command=self._on_ha_command, on_connected=self._on_broker_connected)
        self._server = DeviceServer(
            settings,
            on_connected=self._on_device_connected,
            on_publish=self._on_device_publish,
            on_disconnected=self._on_device_disconnected,
        )

        if settings.allowed_serials:
            _LOGGER.info("Restricting device connections to %d allowed serial(s)", len(settings.allowed_serials))
        else:
            _LOGGER.info("allowed_serials is empty; accepting a connection from any appliance")

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        await self._server.start()
        try:
            await asyncio.gather(self._ha.run(), self._refresh_loop())
        finally:
            self._shutting_down = True
            for task in list(self._background_tasks):
                task.cancel()
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            for session in list(self._sessions.values()):
                await session.close()
            await self._server.stop()

    # ------------------------------------------------------------------ #
    # Background task bookkeeping
    # ------------------------------------------------------------------ #
    def _spawn(self, coro, *, name: str) -> asyncio.Task[None]:  # noqa: ANN001 - coroutine, inferred
        """Create a tracked background task: discarded when done, errors logged.

        Fire-and-forget tasks that silently swallow exceptions (the default
        behaviour of a task nobody awaits) are a common way for a bug to go
        unnoticed forever; this makes sure it at least reaches the log.
        """
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        return task

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.error("Background task %s failed", task.get_name(), exc_info=error)

    # ------------------------------------------------------------------ #
    # Device-side events
    # ------------------------------------------------------------------ #
    def _state_for(self, serial: str) -> DeviceState:
        if serial not in self._states:
            self._states[serial] = DeviceState(serial_number=serial)
        return self._states[serial]

    async def _on_device_connected(self, session: DeviceSession, serial: str) -> None:
        previous = self._sessions.get(serial)
        # Register the new session before closing the old one, so the old
        # session's disconnect is recognized as stale and doesn't flash the
        # device offline in Home Assistant.
        self._sessions[serial] = session
        if previous is not None and previous is not session:
            _LOGGER.info("[%s] replacing an older session", serial)
            await previous.close()

        if not self._settings.allowed_serials:
            _LOGGER.info("[%s] connected; add it to allowed_serials to lock this listener down", serial)

        state = self._state_for(serial)
        state.set_online(True)
        await self._ha.publish_availability(serial, True)

        if serial not in self._published_discovery_identity:
            if state.info.get(protocol.INFO_PRODUCT):
                # Product already known from an earlier session in this process
                # (the appliance reconnected, or this serial ran before) - no
                # need to wait for another cmd-5 reply.
                await self._publish_discovery(serial, state)
            else:
                self._spawn(self._discovery_fallback(serial), name=f"discovery-fallback-{serial}")

        if state.has_core_state:
            # We already have real data for this serial from before this
            # connection (or from an earlier process run's late-arriving
            # message) - safe to publish immediately rather than waiting.
            await self._ha.publish_state(serial, state.to_ha_state())

        self._spawn(self._initial_requests(serial), name=f"initial-{serial}")

    async def _discovery_fallback(self, serial: str) -> None:
        """Publish discovery with placeholder device details if info never arrives.

        Skips the publish if a cmd-5 reply already triggered a real discovery
        publish while we were sleeping.
        """
        await asyncio.sleep(_DISCOVERY_FALLBACK_SECONDS)
        if serial in self._published_discovery_identity:
            return
        state = self._states.get(serial)
        if state is None:
            return
        _LOGGER.info(
            "[%s] no device-info reply within %.0fs; publishing discovery with placeholder details",
            serial,
            _DISCOVERY_FALLBACK_SECONDS,
        )
        await self._publish_discovery(serial, state)

    async def _publish_discovery(self, serial: str, state: DeviceState) -> None:
        await self._ha.publish_discovery(state)
        self._published_discovery_identity[serial] = _discovery_identity(state)

    async def _initial_requests(self, serial: str) -> None:
        await asyncio.sleep(_INITIAL_REQUEST_DELAY_SECONDS)
        await self._request_snapshot(serial)
        await self._request_info(serial)

    async def _on_device_publish(self, serial: str, topic: str, payload: bytes) -> None:
        state = self._state_for(serial)
        message = protocol.parse_device_message(payload)
        if message is None:
            return

        if topic == protocol.lwt_topic(serial):
            if state.set_online(bool(message.get("status"))):
                await self._ha.publish_availability(serial, state.online)
            return

        state_changed, info_changed = state.apply_device_message(message)
        if info_changed and self._published_discovery_identity.get(serial) != _discovery_identity(state):
            # Model / firmware newly known or changed: (re)publish the device card.
            await self._publish_discovery(serial, state)
        if state_changed:
            self._warn_once_if_not_fahrenheit(serial, state)
        # Never publish a state document before the device has told us both
        # power and mode at least once - otherwise a fresh DeviceState would
        # render as a spurious "off" in Home Assistant after every restart.
        if (state_changed or info_changed) and state.has_core_state:
            await self._ha.publish_state(serial, state.to_ha_state())

    def _warn_once_if_not_fahrenheit(self, serial: str, state: DeviceState) -> None:
        unit = state.state.get(protocol.KEY_TEMP_UNIT)
        if unit is None or unit == protocol.TEMP_UNIT_FAHRENHEIT or serial in self._tempunit_warned:
            return
        self._tempunit_warned.add(serial)
        _LOGGER.warning(
            "[%s] device reports tempunit=%r (not Fahrenheit); only Fahrenheit is supported "
            "by this bridge and reported temperatures may be mislabeled",
            serial,
            unit,
        )

    async def _on_device_disconnected(self, session: DeviceSession, serial: str) -> None:
        current = self._sessions.get(serial)
        if current is not session:
            # This session was already replaced by a newer one (see
            # _on_device_connected); its exit must not evict the live
            # replacement or flip HA to "offline" behind its back.
            _LOGGER.info("[%s] stale session disconnected (already replaced)", serial)
            return
        del self._sessions[serial]
        state = self._state_for(serial)
        if state.set_online(False):
            if self._shutting_down:
                # See the _shutting_down comment in __init__: the HA MQTT
                # client is on its own way out (or already gone) during
                # run()'s shutdown, and the bridge-level availability LWT
                # already tells Home Assistant everything is unavailable.
                _LOGGER.debug("[%s] skipping HA availability publish; bridge is shutting down", serial)
            else:
                await self._ha.publish_availability(serial, False)
        _LOGGER.info("[%s] disconnected", serial)

    # ------------------------------------------------------------------ #
    # Home Assistant -> device
    # ------------------------------------------------------------------ #
    async def _on_ha_command(self, serial: str, command: str, payload: str) -> None:
        """Translate and forward one HA command; never propagates an exception.

        A malformed command payload, an unexpected device-session failure, or
        any other bug in this path must degrade to "that one command was
        ignored", never take down the MQTT message loop that every other
        command and device event relies on.
        """
        try:
            await self._handle_ha_command(serial, command, payload)
        except protocol.UnknownCommandError as error:
            _LOGGER.warning("[%s] ignoring command %s=%r: %s", serial, command, payload, error)
        except Exception:  # noqa: BLE001 - last-resort isolation boundary, see docstring
            _LOGGER.exception("[%s] unexpected error handling command %s=%r", serial, command, payload)

    async def _handle_ha_command(self, serial: str, command: str, payload: str) -> None:
        session = self._sessions.get(serial)
        if session is None:
            _LOGGER.warning("[%s] command %s=%r but device is not connected", serial, command, payload)
            return
        changes = protocol.ha_command_to_device(command, payload)
        _LOGGER.info("[%s] set %s", serial, changes)
        await session.inject_publish(protocol.command_request_topic(serial), protocol.build_set_command(changes))

    # ------------------------------------------------------------------ #
    # Home Assistant MQTT broker (re)connection
    # ------------------------------------------------------------------ #
    async def _on_broker_connected(self) -> None:
        """Republish everything HA needs after the broker connection is (re)established.

        Retained messages usually survive a reconnect untouched, but nothing
        here relies on that: after every fresh connection (including the
        very first one) every known serial gets its discovery (if it was
        ever published), current availability, and current state republished,
        so a broker restart, a persistence wipe, or a missed retained message
        can never leave Home Assistant showing stale data.
        """
        # Snapshot: a brand-new appliance can connect (adding to _states)
        # while this loop is suspended on a publish.
        for serial, state in list(self._states.items()):
            if serial in self._published_discovery_identity:
                await self._ha.publish_discovery(state)
            await self._ha.publish_availability(serial, serial in self._sessions)
            if state.has_core_state:
                await self._ha.publish_state(serial, state.to_ha_state())

    # ------------------------------------------------------------------ #
    # Keeping read-only data fresh
    # ------------------------------------------------------------------ #
    async def _request_snapshot(self, serial: str) -> None:
        session = self._sessions.get(serial)
        if session is not None:
            await session.inject_publish(protocol.command_request_topic(serial), protocol.build_state_request())

    async def _request_info(self, serial: str) -> None:
        session = self._sessions.get(serial)
        if session is not None:
            await session.inject_publish(protocol.command_request_topic(serial), protocol.build_device_info_request())

    async def _refresh_loop(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(self._settings.state_refresh_seconds)
            tick += self._settings.state_refresh_seconds
            for serial in list(self._sessions):
                await self._request_snapshot(serial)
                if tick % self._settings.info_refresh_seconds < self._settings.state_refresh_seconds:
                    await self._request_info(serial)
