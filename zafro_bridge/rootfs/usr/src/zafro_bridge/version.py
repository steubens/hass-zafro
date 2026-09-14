"""Single source of truth for the bridge version.

This value must match the ``version`` field in ``zafro_bridge/config.yaml``
and have a corresponding ``## <version>`` heading in ``zafro_bridge/CHANGELOG.md``.
``tests/test_addon_config.py`` enforces all three stay in sync — update it
alongside this file and the CHANGELOG on every version bump (see the release
checklist in CLAUDE.md).
"""

VERSION = "0.1.0"
