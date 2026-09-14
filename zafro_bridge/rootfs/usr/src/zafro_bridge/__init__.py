"""Zafro Bridge: local cloud-replacement for Zafro / Rowan smart appliances.

Acts as the appliance's broker on your LAN (fully local control), optionally
relays to the vendor cloud so the official app keeps working, and exposes
everything to Home Assistant through MQTT Discovery.
"""

from .version import VERSION

__all__ = ["VERSION"]
