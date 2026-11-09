"""Daemon-only credentials, scoped to a Quest and an exact destination/model."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets

from meta_research.companion_writing import validate_content_endpoint


class WritingCredentials:
    def __init__(self, data_root: Path):
        self.root = data_root / "writing-credentials"

    def save(self, *, quest_ref: str, endpoint: str, model: str, api_key: str, api_key_ref: str | None = None) -> str:
        validate_content_endpoint(endpoint)
        if (not isinstance(api_key, str) or not 1 <= len(api_key) <= 8192
                or any(ord(c) < 32 or ord(c) > 126 for c in api_key)
                or not isinstance(model, str) or not model.strip() or len(model) > 256):
            raise ValueError("content_model_credential_invalid")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        ref = "writing-" + secrets.token_hex(16)
        if api_key_ref is not None:
            # Replacing only key bytes keeps the original task binding usable.
            if self.resolve(api_key_ref, quest_ref=quest_ref, endpoint=endpoint, model=model) is None:
                raise ValueError("content_model_credential_unavailable")
        with os.fdopen(os.open(self.root / (ref + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
            json.dump({"quest_ref": quest_ref, "endpoint": endpoint, "model": model, "api_key": api_key}, file)
        if api_key_ref is not None:
            os.replace(self.root / (ref + ".json"), self.root / (api_key_ref + ".json"))
            ref = api_key_ref
        return ref

    def resolve(self, ref: str, *, quest_ref: str, endpoint: str, model: str) -> str | None:
        if not re.fullmatch(r"writing-[a-f0-9]{32}", ref):
            return None
        try:
            value = json.loads((self.root / (ref + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("content_model_credential_unavailable") from None
        if (value.get("quest_ref"), value.get("endpoint"), value.get("model")) != (quest_ref, endpoint, model):
            raise ValueError("content_model_credential_scope_denied")
        return value["api_key"]
