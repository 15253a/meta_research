"""Exercise the read model's real SQL across current and superseded attempts."""
import json
from meta_research.database import Database
from meta_research.runtime_status import RuntimeStatusReader


def test_waiting_reason_follows_current_attempt_and_not_a_newer_historical_request(tmp_path):
    db=Database(tmp_path/'status.sqlite3')
    with db.write() as c:
        schemas={
            'durable_feed':'revision INTEGER, recorded_at REAL',
            'ae_foreground_heads':'quest_ref, cycle_ref, question_ref, stage, epoch INTEGER, status, pending_operation_ref, updated_at REAL',
            'ae_stage_run_requests':'request_ref,quest_ref,cycle_ref,question_ref,stage,epoch INTEGER',
            'ar_stage_runs':'run_ref,request_ref,cycle_ref,stage,epoch INTEGER,status,current_attempt_ref,current_fence_ref,root_session_ref,created_at REAL,updated_at REAL',
            'ar_target_root_lifecycles':'lifecycle_ref,target_ref,target_run_ref,status,updated_at,cancel_reason,launch_ref,root_session_ref,created_at',
            'ar_target_launches':'launch_ref,target_ref,target_run_ref,root_session_ref,graph_ref,stage_request_ref,quest_ref',
            'rg_targets':'target_ref,graph_ref,target_key',
            'owner_human_requests':'request_ref,obligation,kind,quest_ref,is_current INTEGER,status,updated_at REAL',
            'owner_human_request_waiters':'request_ref,target_assertion_json,status',
        }
        for name,columns in schemas.items():
            c.exec_driver_sql(f'CREATE TABLE {name} ({columns})')
        c.exec_driver_sql('INSERT INTO durable_feed VALUES (20,100)')
        c.exec_driver_sql("INSERT INTO ae_foreground_heads VALUES ('quest','cycle','question','bundle',3,'active',NULL,100)")
        c.exec_driver_sql("INSERT INTO ae_stage_run_requests VALUES ('request','quest','cycle','question','bundle',3)")
        c.exec_driver_sql("INSERT INTO ar_stage_runs VALUES ('run','request','cycle','bundle',3,'awaiting_acceptance','attempt-current','fence-current','session-current',100,100)")
        for suffix,updated,obligation in (('current',100,'Current permission decision'),('old',200,'Historical permission decision')):
            c.exec_driver_sql('INSERT INTO owner_human_requests VALUES (?,?,?,?,?,?,?)',
                             (suffix,obligation,'capability_authorization','quest',1,'open',updated))
            assertion={'root':{'run_ref':'run','attempt_ref':'attempt-'+suffix,
                               'fence_ref':'fence-'+suffix,'root_session_ref':'session-'+suffix}}
            c.exec_driver_sql('INSERT INTO owner_human_request_waiters VALUES (?,?,?)',(suffix,json.dumps(assertion),'blocked'))
    try:
        reader=RuntimeStatusReader(db)
        status=reader.query()
        assert status['state']=='waiting'
        assert status['waiting_reason']=='Current permission decision'
        with db.write() as c:
            c.exec_driver_sql("UPDATE ar_stage_runs SET current_attempt_ref='attempt-successor'")
        status=reader.query()
        assert status['waiting_reason']=='当前阶段输出已生成，等待系统接纳'
    finally:
        db.close()
