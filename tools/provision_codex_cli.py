"""Install one exact stable Codex CLI into an atomic, versioned release directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def provision(install_parent: Path, version: str) -> Path:
    if not re.fullmatch(r"0\.\d+\.\d+", version):
        raise ValueError("an exact stable Codex version is required")
    install_parent.mkdir(parents=True, exist_ok=True)
    destination = install_parent / version
    if destination.exists():
        raise FileExistsError(f"preserving existing install: {destination}")
    with tempfile.TemporaryDirectory(prefix=".provision-", dir=install_parent) as temporary:
        staging = Path(temporary)
        cache = staging / "npm-cache"
        subprocess.run([
            "npm", "install", "--prefix", str(staging), "--cache", str(cache),
            "--save-exact", "--ignore-scripts", "--no-audit", "--no-fund",
            f"@openai/codex@{version}",
        ], check=True)
        executable = staging / "node_modules/.bin/codex"
        actual = subprocess.check_output([str(executable), "--version"], text=True).strip()
        if actual != f"codex-cli {version}":
            raise ValueError(f"installed version mismatch: {actual}")
        lock = json.loads((staging / "package-lock.json").read_text())
        packages = lock["packages"]
        package = packages["node_modules/@openai/codex"]
        if package["version"] != version or not package.get("integrity"):
            raise ValueError("package lock does not identify exact Codex package")
        # Npm has already verified SRI during installation. Keep the lock and hashes
        # for the JS launcher, native executable, and bundled tools; discard its cache.
        shutil.rmtree(cache)
        files = {
            str(path.relative_to(staging)): sha256(path)
            for path in sorted(staging.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        manifest = {
            "schema": "meta-research/codex-cli-install/v1",
            "version": version,
            "cli_version": actual,
            "package_integrity": package["integrity"],
            "files": files,
        }
        (staging / "codex-install-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        os.rename(staging, destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installs-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    print(provision(args.installs_root, args.version))
