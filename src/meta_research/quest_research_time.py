"""AE's shared Quest clock; HC applies explicit budget approvals in its transaction.

Service lifecycle facts are distinct from model/run status. A suspended model
waiting for a human still uses time. Only explicit human pause stops a Quest.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import threading
import time

from sqlalchemy import text

from meta_research.database import Database
from meta_research.owners.common import OwnerConflict, canonical_hash, new_ref
from meta_research.time_budget import time_budget_seconds


_LIVE_SERVICES: dict[str, str] = {}
_SERVICE_LOCK = threading.RLock()
_MISSING = object()
_POLICY = "Quest 共用有效经过时间；并行只计一次，跨 Cycle 累计；人工暂停和服务停机不计，等待 HumanRequest 计时。"


def _key(database: Database) -> str:
    return str(database.path.resolve())


def begin_quest_research_time(connection, *, quest_ref, budget, started_at):
    """Called in the existing first-Cycle activation transaction, exactly once."""
    seconds = time_budget_seconds(budget)
    revision = canonical_hash({"quest_ref": quest_ref, "initial_approved_seconds": seconds})
    connection.execute(text(
        "INSERT INTO ae_quest_time_budgets (quest_ref,budget_revision,approved_seconds,"
        "started_at,confirmed_used_seconds,checkpoint_at,paused,completed,unknown_intervals_json) "
        "VALUES (:quest,:revision,:seconds,:now,0,:now,0,0,'[]')"
    ), {"quest": quest_ref, "revision": revision, "seconds": seconds, "now": started_at})
    connection.execute(text(
        "INSERT INTO ae_quest_time_budget_revisions (budget_revision,quest_ref,prior_revision,"
        "approved_seconds,source_ref,created_at) VALUES (:revision,:quest,NULL,:seconds,:quest,:now)"
    ), {"quest": quest_ref, "revision": revision, "seconds": seconds, "now": started_at})


def _service(connection):
    return connection.execute(text(
        "SELECT * FROM ae_research_time_service WHERE singleton='service'"
    )).mappings().first()


def _unmeasured_quest(connection, quest_ref, now):
    quest = connection.execute(text(
        "SELECT goal_json,draft_hash FROM rg_quests WHERE quest_ref=:quest"
    ), {"quest": quest_ref}).mappings().first()
    if quest is None:
        raise OwnerConflict("quest_research_time_not_found")
    draft = json.loads(quest["goal_json"])
    if canonical_hash(draft) != quest["draft_hash"]:
        raise OwnerConflict("quest_research_time_initial_budget_invalid")
    seconds = time_budget_seconds(draft.get("time_budget", "open"))
    initial = connection.execute(text(
        "SELECT activated_at FROM ae_initial_cycles WHERE quest_ref=:quest"
    ), {"quest": quest_ref}).mappings().first()
    started = None if initial is None else initial["activated_at"]
    unknown = [] if started is None else [{"start": started, "end": now, "reason": "historical_time_evidence_missing"}]
    return {"quest_ref": quest_ref, "budget_revision": canonical_hash({"quest_ref": quest_ref, "initial_approved_seconds": seconds}),
            "approved_seconds": seconds, "used_seconds": 0.0 if started is None else None,
            "confirmed_used_seconds": 0.0, "remaining_seconds": seconds if started is None else None,
            "overrun_seconds": 0.0 if started is None and seconds is not None else None,
            "observed_at": now, "accuracy": "exact" if started is None else "unknown",
            "status": "not_started" if started is None else "stopped", "unknown_intervals": unknown,
            "budget_kind": "unlimited" if seconds is None else "finite", "shared": True, "clock_policy": _POLICY,
            "measurement_basis": "recorded_intervals", "accounting_corrections": []}


def _seed_unmeasured_quests(connection, now):
    rows = connection.execute(text(
        "SELECT i.quest_ref,i.activated_at,q.goal_json,q.draft_hash,c.suspension_reason,h.status FROM ae_initial_cycles i "
        "JOIN rg_quests q ON q.quest_ref=i.quest_ref JOIN ae_foreground_heads h ON h.quest_ref=i.quest_ref "
        "JOIN ae_cycles c ON c.cycle_ref=h.cycle_ref LEFT JOIN ae_quest_time_budgets b ON b.quest_ref=i.quest_ref "
        "WHERE b.quest_ref IS NULL"
    )).mappings().all()
    for row in rows:
        draft = json.loads(row["goal_json"])
        if canonical_hash(draft) != row["draft_hash"]:
            raise OwnerConflict("quest_research_time_initial_budget_invalid")
        begin_quest_research_time(connection, quest_ref=row["quest_ref"],
                                 budget=draft.get("time_budget", "open"), started_at=row["activated_at"])
        connection.execute(text(
            "UPDATE ae_quest_time_budgets SET checkpoint_at=:now,paused=:paused,completed=:completed,"
            "unknown_intervals_json=:unknown WHERE quest_ref=:quest"
        ), {"quest": row["quest_ref"], "now": now,
            "paused": row["status"] == "abandoned" or row["suspension_reason"] in {
                "human_paused", "human_cancelled", "question_pruned", "question_restored_revalidation_required"},
            "completed": row["status"] == "completed", "unknown": json.dumps([
                {"start": row["activated_at"], "end": now, "reason": "historical_time_evidence_missing"}])})


def _checkpoint(connection, now):
    service = _service(connection)
    if (service is None or service["status"] != "active" or
            _LIVE_SERVICES.get(str(Path(connection.engine.url.database).resolve())) != service["service_ref"]):
        return
    if now < service["last_observed_at"]:
        for row in connection.execute(text(
            "SELECT quest_ref,unknown_intervals_json FROM ae_quest_time_budgets WHERE paused=0 AND completed=0"
        )).mappings():
            unknown = json.loads(row["unknown_intervals_json"])
            interval = {"start": now, "end": service["last_observed_at"], "reason": "wall_clock_regressed"}
            if interval not in unknown:
                connection.execute(text(
                    "UPDATE ae_quest_time_budgets SET unknown_intervals_json=:unknown WHERE quest_ref=:quest"
                ), {"unknown": json.dumps([*unknown, interval]), "quest": row["quest_ref"]})
        return
    connection.execute(text(
        "UPDATE ae_quest_time_budgets SET confirmed_used_seconds=confirmed_used_seconds + "
        "MAX(0,:now-checkpoint_at), checkpoint_at=:now WHERE paused=0 AND completed=0"
    ), {"now": now})
    connection.execute(text(
        "UPDATE ae_research_time_service SET last_observed_at=:now WHERE singleton='service'"
    ), {"now": now})


def set_quest_time_paused(connection, quest_ref, *, paused, observed_at):
    _checkpoint(connection, observed_at)
    connection.execute(text(
        "UPDATE ae_quest_time_budgets SET paused=:paused,checkpoint_at=MAX(checkpoint_at,:now) WHERE quest_ref=:quest"
    ), {"paused": paused, "now": observed_at, "quest": quest_ref})


def finish_quest_research_time(connection, quest_ref, *, observed_at):
    _checkpoint(connection, observed_at)
    connection.execute(text(
        "UPDATE ae_quest_time_budgets SET completed=1,checkpoint_at=MAX(checkpoint_at,:now) WHERE quest_ref=:quest"
    ), {"now": observed_at, "quest": quest_ref})


class QuestResearchTimeMixin:
    """Mechanical methods of the existing AdvancementEngine Owner."""

    def start_research_time_service(self):
        with _SERVICE_LOCK:
            if getattr(self, "_research_time_service_ref", None) == _LIVE_SERVICES.get(_key(self._database)) and getattr(self, "_research_time_service_ref", None):
                return
            now = time.time()
            service_ref = new_ref("research_time_service")
            with self._database.fenced_write() as connection:
                _seed_unmeasured_quests(connection, now)
                previous = _service(connection)
                if previous is not None and previous["status"] == "active":
                    for row in connection.execute(text(
                        "SELECT quest_ref,checkpoint_at,unknown_intervals_json FROM ae_quest_time_budgets "
                        "WHERE paused=0 AND completed=0"
                    )).mappings():
                        unknown = json.loads(row["unknown_intervals_json"])
                        unknown.append({"start": row["checkpoint_at"], "end": now,
                                        "reason": "service_stop_unobserved"})
                        connection.execute(text(
                            "UPDATE ae_quest_time_budgets SET unknown_intervals_json=:unknown WHERE quest_ref=:quest"
                        ), {"unknown": json.dumps(unknown), "quest": row["quest_ref"]})
                connection.execute(text(
                    "INSERT INTO ae_research_time_service (singleton,service_ref,status,started_at,last_observed_at) "
                    "VALUES ('service',:ref,'active',:now,:now) ON CONFLICT(singleton) DO UPDATE SET "
                    "service_ref=:ref,status='active',started_at=:now,last_observed_at=:now"
                ), {"ref": service_ref, "now": now})
                connection.execute(text("UPDATE ae_quest_time_budgets SET checkpoint_at=:now"), {"now": now})
                self._feed.record(connection, "advancement_engine.research_time_service_started",
                                  {"service_ref": service_ref, "observed_at": now})
            self._research_time_service_ref = service_ref
            _LIVE_SERVICES[_key(self._database)] = service_ref

    def checkpoint_research_time_service(self):
        service_ref = getattr(self, "_research_time_service_ref", None)
        if service_ref is None or _LIVE_SERVICES.get(_key(self._database)) != service_ref:
            return
        with self._database.fenced_write() as connection:
            service = _service(connection)
            if service and service["service_ref"] == service_ref:
                _checkpoint(connection, time.time())

    def stop_research_time_service(self):
        service_ref = getattr(self, "_research_time_service_ref", None)
        if service_ref is None or _LIVE_SERVICES.get(_key(self._database)) != service_ref:
            return
        with _SERVICE_LOCK, self._database.fenced_write() as connection:
            service = _service(connection)
            if service and service["service_ref"] == service_ref:
                _checkpoint(connection, time.time())
                connection.execute(text("UPDATE ae_research_time_service SET status='stopped' WHERE singleton='service'"))
                self._feed.record(connection, "advancement_engine.research_time_service_stopped",
                                  {"service_ref": service_ref, "observed_at": time.time()})
            _LIVE_SERVICES.pop(_key(self._database), None)
            self._research_time_service_ref = None

    def query_research_time(self, quest_ref):
        with self._database.read() as connection:
            return query_quest_research_time_in_transaction(connection, quest_ref, database_path=self._database.path)


def query_quest_research_time_in_transaction(connection, quest_ref, *, database_path=None):
    row = connection.execute(text(
        "SELECT * FROM ae_quest_time_budgets WHERE quest_ref=:quest"
    ), {"quest": quest_ref}).mappings().first()
    if row is None:
        return _unmeasured_quest(connection, quest_ref, time.time())
    service = _service(connection)
    now = time.time()
    live = bool(service and service["status"] == "active" and database_path is not None
                and _LIVE_SERVICES.get(str(Path(database_path).resolve())) == service["service_ref"])
    confirmed = float(row["confirmed_used_seconds"])
    elapsed = confirmed
    if live and not row["paused"] and not row["completed"]:
        elapsed += max(0.0, now - row["checkpoint_at"])
    confirmed = elapsed
    elapsed += row["accounting_adjustment_seconds"]
    unknown = json.loads(row["unknown_intervals_json"])
    if not row["paused"] and not row["completed"]:
        if not live and service and service["status"] == "active" and now > row["checkpoint_at"]:
            unknown = [*unknown, {"start": row["checkpoint_at"], "end": now, "reason": "service_liveness_unobserved"}]
        elif live and now < row["checkpoint_at"]:
            interval = {"start": now, "end": row["checkpoint_at"], "reason": "wall_clock_regressed"}
            if interval not in unknown:
                unknown = [*unknown, interval]
    corrections = json.loads(row["accounting_corrections_json"])
    approved = row["approved_seconds"]
    status = "completed" if row["completed"] else "paused" if row["paused"] else "running" if live else "stopped"
    return {"quest_ref": quest_ref, "budget_revision": row["budget_revision"],
            "approved_seconds": approved, "used_seconds": None if unknown else elapsed,
            "confirmed_used_seconds": confirmed, "remaining_seconds": None if unknown or approved is None else max(0.0, approved-elapsed),
            "overrun_seconds": None if unknown or approved is None else max(0.0, elapsed-approved),
            "observed_at": now, "last_accounted_at": now if live else row["checkpoint_at"], "accuracy": "unknown" if unknown else "exact",
            "status": status, "unknown_intervals": unknown, "budget_kind": "unlimited" if approved is None else "finite",
            "shared": True, "clock_policy": _POLICY,
            "measurement_basis": "human_confirmation" if corrections else "recorded_intervals",
            "accounting_corrections": corrections}


def read_quest_research_time_in_transaction(connection, quest_ref):
    return query_quest_research_time_in_transaction(
        connection, quest_ref, database_path=connection.engine.url.database)


def _seconds(value, *, allow_zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value < 0 or not allow_zero and value == 0 or value > 9_007_199_254_740_991):
        raise OwnerConflict("quest_time_budget_amount_invalid")
    return float(value)


def apply_quest_time_budget(connection, *, quest_ref, expected_revision, approval_ref,
                          request_ref, response_ref, additional_seconds=None, new_total_seconds=_MISSING,
                          confirmed_remaining_seconds=None, correction_reason=None):
    """Apply one already authenticated HC approval in HC's fenced transaction.

    Time passage is deliberately absent from the compare-and-swap identity.
    Ordinary human conversation must never call this transition.
    """
    if (additional_seconds is None) == (new_total_seconds is _MISSING):
        raise OwnerConflict("quest_time_budget_amount_invalid")
    if additional_seconds is not None:
        additional_seconds = _seconds(additional_seconds)
    if new_total_seconds is not _MISSING and new_total_seconds is not None:
        new_total_seconds = _seconds(new_total_seconds, allow_zero=True)
    if confirmed_remaining_seconds is not None:
        confirmed_remaining_seconds = _seconds(confirmed_remaining_seconds, allow_zero=True)
        if not isinstance(correction_reason, str) or not correction_reason.strip() or len(correction_reason) > 4000:
            raise OwnerConflict("quest_time_budget_correction_invalid")
    elif correction_reason is not None:
        raise OwnerConflict("quest_time_budget_correction_invalid")
    command = {"quest_ref": quest_ref, "expected_revision": expected_revision,
               "approval_ref": approval_ref, "request_ref": request_ref, "response_ref": response_ref,
               "amount_kind": "additional" if additional_seconds is not None else "total",
               "seconds": additional_seconds if additional_seconds is not None else new_total_seconds,
               "confirmed_remaining_seconds": confirmed_remaining_seconds, "correction_reason": correction_reason}
    command_hash = canonical_hash(command)
    replay = connection.execute(text(
        "SELECT command_hash,receipt_json FROM ae_quest_time_budget_approvals WHERE approval_ref=:ref"
    ), {"ref": approval_ref}).mappings().first()
    if replay is not None:
        if replay["command_hash"] != command_hash:
            raise OwnerConflict("idempotency_conflict")
        return json.loads(replay["receipt_json"])
    row = connection.execute(text("SELECT * FROM ae_quest_time_budgets WHERE quest_ref=:quest"), {"quest": quest_ref}).mappings().first()
    if row is None:
        _seed_unmeasured_quests(connection, time.time())
        row = connection.execute(text("SELECT * FROM ae_quest_time_budgets WHERE quest_ref=:quest"), {"quest": quest_ref}).mappings().first()
        if row is None:
            raise OwnerConflict("quest_research_time_not_found")
    if row["budget_revision"] != expected_revision:
        raise OwnerConflict("quest_time_budget_revision_stale")
    prior_total = row["approved_seconds"]
    if additional_seconds is not None:
        if prior_total is None:
            raise OwnerConflict("quest_time_budget_already_unlimited")
        total = _seconds(prior_total + additional_seconds, allow_zero=True)
    else:
        total = new_total_seconds
    now = time.time()
    _checkpoint(connection, now)
    correction = None
    if confirmed_remaining_seconds is not None:
        remaining = _seconds(confirmed_remaining_seconds, allow_zero=True)
        if total is None or remaining > total or not isinstance(correction_reason, str) or not correction_reason.strip() or len(correction_reason) > 4000:
            raise OwnerConflict("quest_time_budget_correction_invalid")
        accounted = connection.execute(text("SELECT * FROM ae_quest_time_budgets WHERE quest_ref=:quest"), {"quest": quest_ref}).mappings().one()
        correction = {"approval_ref": approval_ref, "confirmed_remaining_seconds": remaining,
                      "reason": correction_reason, "observed_at": now,
                      "previous_confirmed_used_seconds": accounted["confirmed_used_seconds"],
                      "previous_unknown_intervals": json.loads(accounted["unknown_intervals_json"])}
        corrections = json.loads(accounted["accounting_corrections_json"]) + [correction]
        connection.execute(text(
            "UPDATE ae_quest_time_budgets SET accounting_adjustment_seconds=:adjustment,"
            "accounting_corrections_json=:corrections,unknown_intervals_json='[]' WHERE quest_ref=:quest"
        ), {"quest": quest_ref, "adjustment": total-remaining-accounted["confirmed_used_seconds"],
            "corrections": json.dumps(corrections, ensure_ascii=False)})
    elif correction_reason is not None:
        raise OwnerConflict("quest_time_budget_correction_invalid")
    revision = canonical_hash({**command, "approved_seconds": total})
    connection.execute(text(
        "UPDATE ae_quest_time_budgets SET approved_seconds=:seconds,budget_revision=:revision "
        "WHERE quest_ref=:quest AND budget_revision=:prior"
    ), {"quest": quest_ref, "seconds": total, "revision": revision, "prior": expected_revision})
    connection.execute(text(
        "INSERT INTO ae_quest_time_budget_revisions (budget_revision,quest_ref,prior_revision,"
        "approved_seconds,source_ref,created_at) VALUES (:revision,:quest,:prior,:seconds,:source,:now)"
    ), {"revision": revision, "quest": quest_ref, "prior": expected_revision,
        "seconds": total, "source": approval_ref, "now": now})
    receipt = {"status": "applied", "quest_ref": quest_ref, "approval_ref": approval_ref,
               "request_ref": request_ref, "response_ref": response_ref,
               "prior_budget_revision": expected_revision, "budget_revision": revision,
               "previous_approved_seconds": prior_total, "approved_seconds": total,
               "effective_at": now}
    if correction is not None:
        receipt["time_accounting_correction"] = correction
    connection.execute(text(
        "INSERT INTO ae_quest_time_budget_approvals (approval_ref,quest_ref,request_ref,response_ref,command_hash,receipt_json) "
        "VALUES (:approval,:quest,:request,:response,:hash,:receipt)"
    ), {"approval": approval_ref, "quest": quest_ref, "request": request_ref, "response": response_ref,
        "hash": command_hash, "receipt": json.dumps(receipt, ensure_ascii=False, sort_keys=True)})
    return receipt


def read_quest_research_time(workspace: Path, quest_ref: str):
    current = Path(workspace).resolve()
    root = next((path for path in (current, *current.parents) if (path / "data-root.json").is_file()), None)
    if root is None:
        raise OwnerConflict("quest_research_time_unavailable")
    database = Database(root / "meta-research.sqlite3")
    try:
        with database.read() as connection:
            return query_quest_research_time_in_transaction(connection, quest_ref, database_path=database.path)
    finally:
        database.close()


def research_time_work_constraint(snapshot):
    """Stable decision input: time passage changes it only at exhaustion."""
    status = "available"
    if snapshot["budget_kind"] == "finite":
        if snapshot["accuracy"] == "unknown":
            status = "unconfirmed"
        elif snapshot["remaining_seconds"] <= 0:
            status = "exhausted"
    return {"budget_revision": snapshot["budget_revision"], "status": status}


def require_time_for_new_work(connection, quest_ref):
    snapshot = read_quest_research_time_in_transaction(connection, quest_ref)
    constraint = research_time_work_constraint(snapshot)
    if constraint["status"] != "available":
        raise OwnerConflict("quest_time_budget_" + constraint["status"], {
            "effect_status": "not_applied", "research_time": snapshot,
            "message": "当前研究时间不足或尚未确认，新的 Target 尚未接纳。",
            "correction": "读取当前研究时间；沿 human_request.open 申请明确额度或校正缺证余额，并在原根继续。可先对账、保管和收口已有工作。",
        })
