# https://developers.home-assistant.io/docs/add-ons/configuration#add-on-dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.24
FROM ${BUILD_FROM}

# openssl: generates the self-signed certificate the appliance connects to.
RUN apk add --no-cache openssl

# Python dependencies (pinned ranges; see requirements.txt)
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# Copy root filesystem (s6 service + the bridge package)
COPY rootfs /
RUN chmod a+x /etc/services.d/zafro-bridge/run /etc/services.d/zafro-bridge/finish

ENV PYTHONPATH=/usr/src \
    PYTHONUNBUFFERED=1

# Supervisor watchdog: the device endpoint (container port 8443) must complete
# TLS and answer HTTP, proving the bridge's event loop is responsive.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl --silent --insecure --fail --max-time 5 --output /dev/null https://127.0.0.1:8443/healthz || exit 1

LABEL \
    org.opencontainers.image.title="Home Assistant Add-on: Zafro Bridge" \
    org.opencontainers.image.description="Local cloud-replacement bridge for Zafro / Rowan smart appliances with MQTT Discovery." \
    org.opencontainers.image.source="https://github.com/steubens/hass-zafro" \
    org.opencontainers.image.licenses="MIT"
