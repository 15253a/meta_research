"""Small research-semantic envelopes; data bytes and custody remain in RM."""
from __future__ import annotations

import json
import math

from meta_research.owners.common import AcceptedAssetBinding, AcceptanceReceipt, OwnerConflict, canonical_json


DATASET_SCHEMA = "meta-research/dataset/v1"
DATASET_VERSION_SCHEMA = "meta-research/dataset-version/v1"
DATASET_REFERENCE_SCHEMA = "meta-research/dataset-reference/v1"
DATASET_DERIVATION_SCHEMA = "meta-research/dataset-derivation/v1"


def dataset_text(value: object, field: str, *, optional: bool = False) -> str:
    if not isinstance(value, str) or (not optional and not value.strip()) or "\x00" in value:
        raise OwnerConflict(f"dataset_{field}_invalid")
    if len(value.encode("utf-8")) > (65536 if field in {"meaning", "notes", "purpose", "processing"} else 1024):
        raise OwnerConflict(f"dataset_{field}_invalid")
    return value


def dataset_metadata(value: object) -> dict[str, object]:
    """Keep domain-specific descriptions free-form, but always portable JSON."""
    if value is None:
        return {}
    def valid(item: object) -> bool:
        if item is None or isinstance(item, (str, bool, int)):
            return True
        if isinstance(item, float):
            return math.isfinite(item)
        if isinstance(item, list):
            return all(valid(child) for child in item)
        if isinstance(item, dict):
            return all(isinstance(key, str) and valid(child) for key, child in item.items())
        return False
    try:
        if not isinstance(value, dict) or not valid(value):
            raise ValueError
        encoded = canonical_json(value)
        if len(encoded.encode("utf-8")) > 262144:
            raise ValueError
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise OwnerConflict("dataset_metadata_invalid") from error


def dataset_asset_binding(value: object) -> AcceptedAssetBinding:
    if isinstance(value, AcceptedAssetBinding):
        value = value.as_dict()
    if not isinstance(value, dict) or set(value) != {"asset_ref", "version_ref", "content_hash", "manifest_hash", "receipt"}:
        raise OwnerConflict("dataset_asset_binding_invalid")
    receipt = value["receipt"]
    keys = {"issuer", "kind", "receipt_ref", "subject_ref", "payload_hash"}
    if not isinstance(receipt, dict) or set(receipt) not in (keys, keys | {"status"}) or receipt.get("status", "accepted") != "accepted":
        raise OwnerConflict("dataset_asset_binding_invalid")
    for field in ("asset_ref", "version_ref", "content_hash", "manifest_hash"):
        dataset_text(value[field], "asset_binding")
    for field in keys:
        dataset_text(receipt[field], "asset_binding")
    for digest in (value["content_hash"], value["manifest_hash"], receipt["payload_hash"]):
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise OwnerConflict("dataset_asset_binding_invalid")
    return AcceptedAssetBinding(
        asset_ref=value["asset_ref"], version_ref=value["version_ref"],
        content_hash=value["content_hash"], manifest_hash=value["manifest_hash"],
        receipt=AcceptanceReceipt(**{key: receipt[key] for key in keys}),
    )
