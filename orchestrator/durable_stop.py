"""Append-only helpers for effective global-stop state.

A ``global_stop`` remains historical truth forever. A narrowly authorized
recovery appends a ``global_stop_release`` naming the exact stop id; it never
updates or deletes the incident. Only unmatched stops remain effective.
"""
from __future__ import annotations

import json
from typing import Any, Optional, Tuple


def active_global_stop(conn: Any) -> Optional[Tuple[int, dict]]:
    row = conn.execute(
        "SELECT s.id,s.payload_json FROM decision s "
        "WHERE s.actor='orchestrator' AND s.type='global_stop' "
        "AND NOT EXISTS ("
        " SELECT 1 FROM decision r "
        " WHERE r.actor='orchestrator' AND r.type='global_stop_release' "
        " AND json_valid(r.payload_json) "
        " AND json_extract(r.payload_json,'$.global_stop_id')=s.id"
        ") ORDER BY s.id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1])
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("global_stop payload 损坏") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("reason"), str):
        raise RuntimeError("global_stop payload 缺合法 reason")
    return int(row[0]), payload


def has_active_global_stop(conn: Any) -> bool:
    return active_global_stop(conn) is not None


__all__ = ["active_global_stop", "has_active_global_stop"]
