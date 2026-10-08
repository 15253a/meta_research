from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Upload:
    relative_path: str
    media_type: str
    content: bytes


@dataclass(frozen=True)
class LinkedLocal:
    locator: str
    description: str


@dataclass(frozen=True)
class ServerReference:
    selection: dict[str, object]


@dataclass(frozen=True)
class ProvidedReply:
    note: str
    facts: dict[str, object]
    materials: tuple[Upload | LinkedLocal | ServerReference, ...] = ()
    decision: Literal["provided"] = "provided"


@dataclass(frozen=True)
class OtherReply:
    decision: Literal["declined", "deferred"]
    note: str
    facts: dict[str, object]
