"""The TLS endpoint the appliance is redirected to: a stand-in for the cloud.

Serves exactly the three things the device asks for:

1. ``POST /iot1/device/login``  — proxied to the cloud when relaying, else stubbed
2. ``GET  /iot1/time/second``   — proxied when relaying, else the local clock
3. ``GET  /ws/iot1/``           — WebSocket upgrade (subprotocol "mqtt") → DeviceSession

Any other path gets the cloud's own quirky ``404 Not Find`` so the device sees
nothing unfamiliar. The certificate is self-signed for the cloud hostname; the
appliance does not verify it (that is the whole reason this works).

The number of simultaneous device sessions is capped (``max_device_sessions``)
so a single misconfigured or hostile peer opening WebSocket after WebSocket
cannot exhaust memory or file descriptors; once at the cap, new upgrades are
refused outright with HTTP 503 before a ``DeviceSession`` is even created.
"""

from __future__ import annotations

import logging
import ssl
import time

from aiohttp import web

from . import mqtt_codec, protocol
from .cloud_relay import proxy_rest_request
from .config import Settings
from .device_session import DeviceSession, OnConnectedCallback, OnDisconnectedCallback, OnPublishCallback

_LOGGER = logging.getLogger(__name__)

# Slack above the largest MQTT packet we accept, to cover WebSocket/HTTP
# framing overhead. Generous, but still far below anything a legitimate
# appliance frame would ever need.
_WEBSOCKET_HEADER_ALLOWANCE = 4096


class DeviceServer:
    """aiohttp application bound to the device-facing TLS port."""

    def __init__(
        self,
        settings: Settings,
        on_connected: OnConnectedCallback,
        on_publish: OnPublishCallback,
        on_disconnected: OnDisconnectedCallback,
    ) -> None:
        self._settings = settings
        self._on_connected = on_connected
        self._on_publish = on_publish
        self._on_disconnected = on_disconnected
        self._runner: web.AppRunner | None = None
        self._active_sessions: set[DeviceSession] = set()

    # ------------------------------------------------------------------ #
    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post(protocol.REST_DEVICE_LOGIN_PATH, self._handle_device_login)
        app.router.add_get(protocol.REST_TIME_PATH, self._handle_time)
        app.router.add_get(protocol.WEBSOCKET_PATH, self._handle_websocket)
        app.router.add_route("*", "/{tail:.*}", self._handle_not_found)
        return app

    async def start(self) -> None:
        ssl_context = self._build_ssl_context()
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._settings.listen_host, self._settings.listen_port, ssl_context=ssl_context)
        await site.start()
        _LOGGER.info(
            "Device endpoint listening on https://%s:%s (relay %s)",
            self._settings.listen_host,
            self._settings.listen_port,
            "enabled" if self._settings.cloud_relay_enabled else "disabled",
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _build_ssl_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            certfile=str(self._settings.tls_certificate_path),
            keyfile=str(self._settings.tls_private_key_path),
        )
        # The appliance's embedded TLS stack is old and inflexible: it will
        # only negotiate TLSv1 with a legacy cipher set, and its handshake
        # simply fails against anything stricter. TLSv1 + SECLEVEL=0 here are
        # therefore a hard requirement of talking to the hardware at all, not
        # an oversight — do NOT "modernize" this to a higher minimum version
        # or a stricter security level, or the appliance will stop connecting.
        # This is safe specifically *because* the device never validates the
        # certificate anyway (it accepts whatever we present); a browser or
        # any other TLS-verifying client must never be pointed at this port.
        context.minimum_version = ssl.TLSVersion.TLSv1
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
        return context

    # ------------------------------------------------------------------ #
    # REST handlers
    # ------------------------------------------------------------------ #
    async def _handle_device_login(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        identity = protocol.parse_device_message(body) or {}
        serial = identity.get("deviceSn")
        _LOGGER.info("Device login from %s (sn=%s)", request.remote, serial)

        if self._settings.cloud_relay_enabled:
            proxied = await self._try_proxy(request, body)
            if proxied is not None:
                return proxied

        return web.json_response(protocol.build_local_login_response(int(time.time())))

    async def _handle_time(self, request: web.Request) -> web.StreamResponse:
        if self._settings.cloud_relay_enabled:
            proxied = await self._try_proxy(request, b"")
            if proxied is not None:
                return proxied
        return web.json_response(protocol.build_local_time_response(int(time.time())))

    async def _try_proxy(self, request: web.Request, body: bytes) -> web.Response | None:
        """Forward to the cloud; None (fall back to the local stub) if it's unreachable or erroring."""
        try:
            status, content_type, payload = await proxy_rest_request(
                self._settings.cloud_host,
                request.method,
                request.path_qs,
                request.headers,
                body,
                self._settings.cloud_connect_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001
            _LOGGER.warning("Cloud REST proxy failed for %s (%s); answering locally", request.path, error)
            return None
        if status >= 400:
            # The cloud is reachable but rejected this request (expired token,
            # maintenance, a transient 5xx, ...); the local stub at least keeps
            # the appliance's own bootstrap sequence moving.
            _LOGGER.warning("Cloud REST proxy for %s returned status %s; answering locally", request.path, status)
            return None
        return web.Response(status=status, body=payload, content_type=content_type.split(";")[0])

    async def _handle_not_found(self, request: web.Request) -> web.StreamResponse:
        _LOGGER.debug("Unexpected request %s %s from %s", request.method, request.path, request.remote)
        return web.Response(status=404, reason=protocol.CLOUD_NOT_FOUND_REASON, text="", content_type="text/plain")

    # ------------------------------------------------------------------ #
    # WebSocket: hand the socket to a DeviceSession
    # ------------------------------------------------------------------ #
    async def _handle_websocket(self, request: web.Request) -> web.StreamResponse:
        if len(self._active_sessions) >= self._settings.max_device_sessions:
            _LOGGER.warning(
                "Refusing device WebSocket from %s: %d session(s) already active (max_device_sessions=%d)",
                request.remote,
                len(self._active_sessions),
                self._settings.max_device_sessions,
            )
            return web.Response(status=503, reason="Service Unavailable", text="too many active device sessions")

        # compress=False: decline permessage-deflate. The appliance offers it, but
        # a compressed shared context desynchronises the raw MQTT relay (and hit an
        # aiohttp writer crash); uncompressed frames are simpler and the device
        # accepts them. Everything on this socket is small binary MQTT anyway.
        # max_msg_size: cap a single WebSocket frame just above the largest MQTT
        # packet we accept, so we can never be made to buffer an unbounded frame
        # before decoding even gets a chance to reject it.
        websocket = web.WebSocketResponse(
            protocols=(protocol.WEBSOCKET_SUBPROTOCOL,),
            heartbeat=None,
            autoping=True,
            compress=False,
            max_msg_size=mqtt_codec.MAX_PACKET_BYTES + _WEBSOCKET_HEADER_ALLOWANCE,
        )
        session = DeviceSession(
            websocket,
            self._settings,
            on_connected=self._on_connected,
            on_publish=self._on_publish,
            on_disconnected=self._on_disconnected,
        )
        # Claim the slot before the first await so simultaneous upgrades can't
        # all pass the cap check above.
        self._active_sessions.add(session)
        try:
            await websocket.prepare(request)
            _LOGGER.info("Device WebSocket from %s", request.remote)
            await session.run()
        finally:
            self._active_sessions.discard(session)
        return websocket
