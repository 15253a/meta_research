from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Protocol, TypeAlias, cast


OwnerFact: TypeAlias = int | str | bool | None
QUESTION_PROPOSAL_SCHEMA = "meta-research/question-proposal/v1"


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


@dataclass(frozen=True)
class AcceptanceReceipt:
    issuer: str
    kind: str
    receipt_ref: str
    subject_ref: str
    payload_hash: str

    def as_public_dict(self) -> dict[str, str]:
        return {
            "status": "accepted",
            "issuer": self.issuer,
            "kind": self.kind,
            "receipt_ref": self.receipt_ref,
            "subject_ref": self.subject_ref,
            "payload_hash": self.payload_hash,
        }


class BundleConfirmationVerifier(Protocol):
    def verify_bundle_confirmation(
        self,
        *,
        initialization_id: str,
        draft_revision: int,
        draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
        preview_ref: str,
        preview_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class QuestReceiptVerifier(Protocol):
    def verify_quest_receipt(
        self,
        *,
        initialization_id: str,
        quest_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        confirmation_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class QuestionContentReceiptVerifier(Protocol):
    def verify_question_content_receipt(
        self,
        *,
        initialization_id: str,
        content_ref: str,
        content_hash: str,
        schema_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        confirmation_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class RootQuestionReceiptVerifier(Protocol):
    def verify_root_question_receipt(
        self,
        *,
        initialization_id: str,
        quest_ref: str,
        question_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class OwnerConflict(RuntimeError):
    """An idempotent Owner command was replayed with different semantics."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def decoded_object(value: str) -> dict[str, object]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored value is not a JSON object")
    return cast(dict[str, object], decoded)


def new_ref(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
