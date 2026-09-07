"""Bounded observational reads for the home page, never execution authority."""
from datetime import datetime, timezone
from sqlalchemy import text

from meta_research.database import Database
from meta_research.query_timing import measure_query, query_section, record_attempt

STAGES = {'idea': '研究思路', 'plan': '验证计划', 'bundle': '实验与证据', 'reasoning': '研究判断'}


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return str(value)


class RuntimeStatusReader:
    def __init__(self, database: Database):
        self.database = database

    def query(self):
        with measure_query('runtime_status') as timing, self.database.read_snapshot() as c:
            record_attempt()
            with query_section('position'):
                event = c.execute(text('SELECT revision, recorded_at FROM durable_feed ORDER BY revision DESC LIMIT 1')).mappings().first()
                foreground = c.execute(text('''
                    SELECT quest_ref, cycle_ref, question_ref, stage, epoch, status,
                           pending_operation_ref, updated_at
                    FROM ae_foreground_heads ORDER BY updated_at DESC, quest_ref DESC LIMIT 1
                ''')).mappings().first()
                run = None
                root = None
                waiting = None
                pending_requests = 0
                if foreground is not None:
                    scope = dict(foreground)
                    run = c.execute(text('''
                        SELECT r.run_ref, r.request_ref, r.status, r.updated_at,
                               r.current_attempt_ref, r.current_fence_ref, r.root_session_ref
                        FROM ar_stage_runs r JOIN ae_stage_run_requests q ON q.request_ref=r.request_ref
                        WHERE q.quest_ref=:quest_ref AND q.cycle_ref=:cycle_ref
                          AND q.question_ref=:question_ref AND q.stage=:stage AND q.epoch=:epoch
                          AND r.cycle_ref=q.cycle_ref AND r.stage=q.stage AND r.epoch=q.epoch
                        ORDER BY r.created_at DESC, r.run_ref DESC LIMIT 1
                    '''), scope).mappings().first()
                    if run is not None:
                        with query_section('current_target'):
                            root = c.execute(text('''
                                SELECT r.target_ref, r.target_run_ref AS run_ref, r.status,
                                       r.updated_at, r.cancel_reason, t.target_key AS title
                                FROM ar_target_root_lifecycles r
                                JOIN ar_target_launches l ON l.launch_ref=r.launch_ref
                                  AND l.target_ref=r.target_ref AND l.target_run_ref=r.target_run_ref
                                  AND l.root_session_ref=r.root_session_ref
                                JOIN rg_targets t ON t.target_ref=r.target_ref AND t.graph_ref=l.graph_ref
                                WHERE l.stage_request_ref=:request_ref AND l.quest_ref=:quest_ref
                                  AND r.status NOT IN ('completed','cancelled','failed','interrupted')
                                ORDER BY r.created_at DESC, r.lifecycle_ref DESC LIMIT 1
                            '''), {**scope, **dict(run)}).mappings().first()
                            waiting = c.execute(text('''
                                SELECT h.request_ref, h.obligation, h.kind
                                FROM owner_human_requests h JOIN owner_human_request_waiters w
                                  ON w.request_ref=h.request_ref
                                WHERE h.quest_ref=:quest_ref AND h.is_current=1 AND h.status='open'
                                  AND w.status NOT IN ('consumed','released','cancelled','superseded')
                                  AND (
                                    json_extract(w.target_assertion_json,'$.run_ref')=:run_ref
                                    OR (
                                      json_extract(w.target_assertion_json,'$.root.run_ref')=:run_ref
                                      AND json_extract(w.target_assertion_json,'$.root.attempt_ref')=:current_attempt_ref
                                      AND json_extract(w.target_assertion_json,'$.root.fence_ref')=:current_fence_ref
                                      AND json_extract(w.target_assertion_json,'$.root.root_session_ref')=:root_session_ref
                                    )
                                  )
                                ORDER BY h.updated_at DESC LIMIT 1
                            '''), {**scope, **dict(run)}).mappings().first()
                    pending_requests = c.execute(text('''
                        SELECT COUNT(*) FROM owner_human_requests
                        WHERE quest_ref=:quest_ref AND is_current=1 AND status='open'
                    '''), scope).scalar_one()
            task = None
            state = 'idle' if foreground is None else 'pending'
            reason = None
            if foreground is not None:
                task = {'kind': 'stage', 'title': STAGES.get(foreground['stage'], foreground['stage']),
                        'run_ref': None if run is None else run['run_ref'], 'target_ref': None,
                        'status': 'pending' if run is None else run['status']}
                if root is not None:
                    task = {'kind': 'target', 'title': root['title'], 'run_ref': root['run_ref'],
                            'target_ref': root['target_ref'], 'status': root['status']}
                raw = task['status']
                state = ('running' if raw in ('running','starting') else 'advancing' if raw == 'active'
                         else 'completed' if raw in ('completed','succeeded') else 'failed' if raw in ('failed','interrupted')
                         else 'paused' if raw in ('paused','suspended') else 'waiting' if 'wait' in raw or raw == 'blocked'
                         else 'pending')
                if foreground['status'] in ('paused','suspended'):
                    state, reason = 'paused', '当前研究已暂停'
                elif foreground['status'] in ('completed','closed'):
                    state = 'completed'
                elif waiting is not None and root is None:
                    state, reason = 'waiting', waiting['obligation']
                elif foreground['pending_operation_ref']:
                    reason = '等待当前研究控制操作完成'
                elif state == 'pending':
                    reason = '等待工作器接续当前阶段'
                elif raw == 'awaiting_acceptance':
                    reason = '当前阶段输出已生成，等待系统接纳'
                elif state == 'waiting':
                    reason = '等待当前任务的继续条件；详情中可查看具体原因'
            observed_at = datetime.now(timezone.utc).isoformat()
            result = {
                'schema_ref': 'meta-research/runtime-status/v1',
                'revision': 0 if event is None else event['revision'],
                'observed_at': observed_at,
                'updated_at': observed_at if event is None else timestamp(event['recorded_at']),
                'state': state, 'current_task': task, 'waiting_reason': reason,
                'pending_requests': pending_requests,
                'foreground': None if foreground is None else dict(foreground),
            }
            result['query_diagnostics'] = timing.public()
            return result
