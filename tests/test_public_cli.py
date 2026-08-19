from __future__ import annotations

import json
import subprocess


def test_installed_cli_reports_its_release_version() -> None:
    completed = subprocess.run(
        ["meta-research", "version", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "product": "meta-research-vnext",
        "version": "0.1.0",
    }
