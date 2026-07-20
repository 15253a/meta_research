import hashlib
import json
import re
import runpy
from pathlib import Path
from unittest.mock import patch

from orchestrator import web_app


ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = ROOT / "engines" / "wildidea"
MANIFEST = VENDOR_ROOT / "MANIFEST.sha256"


def _manifest_entries():
    entries = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        assert match is not None, f"invalid manifest line: {line!r}"
        digest, relative = match.groups()
        assert relative not in entries, f"duplicate manifest path: {relative}"
        entries[relative] = digest
    return entries


def test_wildidea_upstream_provenance_is_pinned():
    metadata = json.loads((VENDOR_ROOT / "UPSTREAM.json").read_text(encoding="utf-8"))
    assert metadata == {
        "schema": "meta-research-wildidea-upstream/v1",
        "repository": "https://github.com/liwenyu2002/wildidea.git",
        "ref": "main",
        "commit": "6ff66ada15b0047b2e03d229f2e9543c542df598",
        "commit_time": "2026-07-12T15:08:35+08:00",
        "tree": "3bf8299953dd316cd7086cc627dbe71cef66fe23",
        "archive_sha256": (
            "sha256:54de73ce7d8f0d1442fb62b9ff08415dd23fea54519e3b35e257602bf2330453"
        ),
        "vendored_path": "upstream",
        "license": "MIT",
    }
    assert (VENDOR_ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License\n")


def test_wildidea_manifest_is_complete_and_matches_bytes():
    entries = _manifest_entries()
    actual_files = {
        path.relative_to(VENDOR_ROOT).as_posix()
        for path in VENDOR_ROOT.rglob("*")
        if path.is_file() and path != MANIFEST
    }
    assert len(actual_files) == 20
    assert set(entries) == actual_files
    assert list(entries) == sorted(entries)

    for relative, expected_digest in entries.items():
        path = VENDOR_ROOT / relative
        assert not path.is_symlink()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_digest


def test_wildidea_vendor_is_in_install_assets_and_system_markers():
    with patch("setuptools.setup") as mocked_setup:
        runpy.run_path(str(ROOT / "setup.py"))
    packaged_sources = {
        Path(source).as_posix()
        for _, sources in mocked_setup.call_args.kwargs["data_files"]
        for source in sources
    }
    expected_sources = {
        path.relative_to(ROOT).as_posix()
        for path in VENDOR_ROOT.rglob("*")
        if path.is_file()
    }
    assert expected_sources <= packaged_sources
    assert "engines/wildidea/UPSTREAM.json" in web_app._SYSTEM_MARKERS
