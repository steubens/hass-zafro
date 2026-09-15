"""Checks on the s6 run script that the Python tests can't reach otherwise.

The allow-list is read from the add-on options with jq. An earlier version
looped over ``bashio::config`` output with ``while read``, which drops the last
list item because bashio prints it without a trailing newline, so a single
configured serial was silently ignored on real Home Assistant installs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

RUN_SCRIPT = Path(__file__).resolve().parents[1] / "zafro_bridge" / "rootfs" / "etc" / "services.d" / "zafro-bridge" / "run"


def _allowed_serials_jq_filter() -> str:
    match = re.search(r"ZAFRO_ALLOWED_SERIALS=\"\$\(jq --raw-output '([^']+)' /data/options\.json\)\"", RUN_SCRIPT.read_text())
    assert match, "run script no longer builds ZAFRO_ALLOWED_SERIALS with the expected jq filter"
    return match.group(1)


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"allowed_serials": []}, ""),
        ({}, ""),
        ({"allowed_serials": None}, ""),
        ({"allowed_serials": ["SERIALONE"]}, "SERIALONE"),
        ({"allowed_serials": ["SERIALONE", "SERIALTWO"]}, "SERIALONE,SERIALTWO"),
        ({"allowed_serials": ["", "SERIALONE", ""]}, "SERIALONE"),
    ],
)
def test_allowed_serials_filter_keeps_every_configured_serial(options: dict, expected: str) -> None:
    jq_path = shutil.which("jq")
    if jq_path is None:
        pytest.skip("jq is not installed")
    result = subprocess.run(
        [jq_path, "--raw-output", _allowed_serials_jq_filter()],
        input=json.dumps(options),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == expected


def test_run_script_does_not_read_list_options_line_by_line() -> None:
    assert "done < <(bashio::config" not in RUN_SCRIPT.read_text()
