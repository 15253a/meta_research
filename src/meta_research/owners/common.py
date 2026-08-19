from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


OwnerFact: TypeAlias = int | str | bool | None


@dataclass(frozen=True)
class OwnerSnapshot:
    owner: str
    revision: int
    facts: dict[str, OwnerFact]
    status: str = "ready"

    def as_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "revision": self.revision,
            "facts": self.facts,
        }
