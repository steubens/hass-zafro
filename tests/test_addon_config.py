"""Cross-checks between the add-on manifest, the changelog and the code.

These guard against the classic "bumped the code but forgot the manifest" (or
vice versa) mistake described in the release checklist in CLAUDE.md. The
manifest is parsed with a small line-oriented reader rather than a YAML
library: `config.yaml` only ever uses a tiny, predictable subset of YAML
(top-level `key: value` scalars and one flat `options:` block), and pulling
in a YAML dependency just to read it here would be more machinery than the
scalars are worth.
"""

import re
from pathlib import Path

from zafro_bridge.config import Settings

from zafro_bridge import version

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_YAML_PATH = REPO_ROOT / "zafro_bridge" / "config.yaml"
CHANGELOG_PATH = REPO_ROOT / "zafro_bridge" / "CHANGELOG.md"

# config.yaml option name -> Settings field name, for every option whose
# default the Settings side is expected to reproduce with no env vars set.
# (tls_hostname, mqtt_*, etc. are intentionally not covered here: they either
# have no direct Settings counterpart or are optional with no default.)
OPTION_TO_SETTINGS_FIELD = {
    "cloud_relay": "cloud_relay_enabled",
    "cloud_host": "cloud_host",
    "discovery_prefix": "discovery_prefix",
    "state_refresh_seconds": "state_refresh_seconds",
    "info_refresh_seconds": "info_refresh_seconds",
    "log_level": "log_level",
}

# Every env var Settings.from_environment consults for the options above,
# cleared so defaults are what's actually compared.
RELEVANT_ENV_VARS = (
    "ZAFRO_CLOUD_RELAY",
    "ZAFRO_CLOUD_HOST",
    "ZAFRO_DISCOVERY_PREFIX",
    "ZAFRO_STATE_REFRESH",
    "ZAFRO_INFO_REFRESH",
    "ZAFRO_LOG_LEVEL",
)


def _read_config_yaml() -> str:
    return CONFIG_YAML_PATH.read_text(encoding="utf-8")


def _top_level_scalar(text: str, key: str) -> str:
    """Return the value of a top-level ``key: value`` line, quotes stripped.

    Only matches a line with no leading whitespace, so it can't be fooled by
    a same-named key nested under ``options:`` or ``schema:``.
    """
    match = re.search(rf'^{re.escape(key)}:\s*"?([^"\n]+?)"?\s*$', text, re.MULTILINE)
    assert match, f"no top-level '{key}:' scalar found in {CONFIG_YAML_PATH}"
    return match.group(1)


def _parse_options_block(text: str) -> dict[str, bool | int | str]:
    """Parse the flat, two-space-indented ``options:`` block into Python values."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "options:")
    values: dict[str, bool | int | str] = {}
    for line in lines[start + 1 :]:
        if line.strip() == "" or line.startswith("  #"):
            continue
        if not line.startswith("  ") or line.startswith("   "):
            break  # dedent (or deeper indent, which this block never uses) ends it
        name, _, raw_value = line.strip().partition(":")
        raw_value = raw_value.strip()
        if raw_value == "true":
            values[name] = True
        elif raw_value == "false":
            values[name] = False
        elif re.fullmatch(r"-?\d+", raw_value):
            values[name] = int(raw_value)
        else:
            values[name] = raw_value.strip('"')
    return values


def _latest_changelog_version() -> str:
    """First ``## <version>`` heading in the changelog, skipping "Unreleased"."""
    heading_pattern = re.compile(r"^##\s+(\S+)", re.MULTILINE)
    version_pattern = re.compile(r"^\d+(\.\d+)*$")
    for heading in heading_pattern.findall(CHANGELOG_PATH.read_text(encoding="utf-8")):
        if version_pattern.match(heading):
            return heading
    raise AssertionError(f"no version heading found in {CHANGELOG_PATH}")


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_version_matches_config_yaml_and_changelog():
    config_version = _top_level_scalar(_read_config_yaml(), "version")
    changelog_version = _latest_changelog_version()
    assert version.VERSION == config_version, (
        f"version.py ({version.VERSION}) and config.yaml ({config_version}) have drifted; "
        "see the release checklist in CLAUDE.md"
    )
    assert version.VERSION == changelog_version, (
        f"version.py ({version.VERSION}) has no matching '## {version.VERSION}' heading "
        f"in CHANGELOG.md (latest heading found: {changelog_version})"
    )


def test_option_defaults_match_settings_defaults(monkeypatch):
    for env_var in RELEVANT_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    settings = Settings.from_environment(version.VERSION)

    option_defaults = _parse_options_block(_read_config_yaml())
    for option_name, settings_field in OPTION_TO_SETTINGS_FIELD.items():
        assert option_name in option_defaults, f"config.yaml options: is missing '{option_name}'"
        expected = option_defaults[option_name]
        actual = getattr(settings, settings_field)
        assert actual == expected, (
            f"config.yaml default for '{option_name}' ({expected!r}) does not match "
            f"Settings.{settings_field} with no env vars set ({actual!r})"
        )


def test_no_legacy_hassio_api_permission():
    # hassio_api is a deprecated Supervisor permission superseded by the
    # `services`/`homeassistant_api` model this add-on already uses; it
    # should never be reintroduced.
    assert "hassio_api" not in _read_config_yaml()


def test_homeassistant_minimum_version_is_recent_enough():
    minimum_required = _version_tuple("2025.3.0")
    declared = _version_tuple(_top_level_scalar(_read_config_yaml(), "homeassistant"))
    assert declared >= minimum_required, (
        f"config.yaml declares homeassistant: {'.'.join(map(str, declared))}, "
        f"below the {'.'.join(map(str, minimum_required))} minimum this add-on relies on "
        "(device-based MQTT discovery, horizontal swing)"
    )


def test_allowed_serials_defaults_to_accepting_any_appliance(monkeypatch):
    """config.yaml ships an empty allow-list, which Settings reads as "no restriction"."""
    assert re.search(r"^  allowed_serials: \[\]$", _read_config_yaml(), re.MULTILINE)
    monkeypatch.delenv("ZAFRO_ALLOWED_SERIALS", raising=False)
    assert Settings.from_environment("test").allowed_serials == frozenset()
    monkeypatch.setenv("ZAFRO_ALLOWED_SERIALS", "SERIALONE, SERIALTWO,,")
    assert Settings.from_environment("test").allowed_serials == frozenset({"SERIALONE", "SERIALTWO"})
