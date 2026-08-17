#!/usr/bin/env python3
"""Run the downloader with Node's environment-proxy support enabled."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def main() -> None:
    node = shutil.which("node")
    if node is None:
        raise SystemExit("node is required")
    environment = os.environ.copy()
    bypass = []
    for name in ("NO_PROXY", "no_proxy"):
        bypass.extend(item.strip() for item in environment.get(name, "").split(",") if item.strip())
    for local in ("127.0.0.1", "localhost"):
        if local not in bypass:
            bypass.append(local)
    environment["NO_PROXY"] = environment["no_proxy"] = ",".join(dict.fromkeys(bypass))
    batch = Path(__file__).with_name("batch_download.mjs")
    os.execvpe(node, [node, "--use-env-proxy", str(batch), *sys.argv[1:]], environment)


if __name__ == "__main__":
    main()
