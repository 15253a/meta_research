"""测试公共设施：定位系统根目录、加载 schema / fixture、$ref 注册表、DB 最小合法图。"""
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

SYSTEM_ROOT = Path(__file__).resolve().parent.parent   # meta-research/
SCHEMAS_DIR = SYSTEM_ROOT / "schemas"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# 让测试可 import orchestrator 包
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from orchestrator import database as _db   # noqa: E402（须在 sys.path 注入后）


def seed_minimal(conn: sqlite3.Connection) -> None:
    """最小合法图（CP2.1 否定用例共用）：goal→cycle→question(answered，带 success evaluation 证据链)＋池对象＋账本行。

    走完一次 I3 关问，顺带正向验证冻结 schema 可承载完整因果链；否定用例从中派生非法写入。
    """
    conn.executescript("""
    INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}');
    INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) VALUES (1,1,1,'reasoning','v0');
    INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source)
      VALUES (1,1,1,1,'q1','active','agent');

    INSERT INTO baseline(id,slug,canonical_key,status) VALUES (1,'b','bk1','planned');
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (1,1,'v1','{}','planned');
    INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (1,1,'proto','{}');
    INSERT INTO metric_def(id,version,name,direction) VALUES (1,1,'acc','higher');
    INSERT INTO protocol_metric(protocol_id,protocol_ver,metric_id,metric_ver) VALUES (1,1,1,1);

    -- 两个 build_target：eval（承 evaluation）+ build（承 run），均 complete
    INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id,
                             eval_action,eval_key,evaluation_source)
      VALUES (1,1,1,'eval',1,'complete',1,'create_evaluation','e1','factory');
    INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id)
      VALUES (2,1,1,'build',2,'complete',1);

    INSERT INTO run(id,cycle_id,variant_id,build_target_id,kind,status) VALUES (1,1,1,2,'build','success');
    INSERT INTO checkpoint(id,variant_id,ckpt_key,path,content_hash,hash_alg,artifact_type,origin)
      VALUES (1,1,'c1','/x','h','sha256','algorithm','none');

    -- evaluation：先 created，落 success attempt + metric_result，再 UPDATE 成 success+canonical
    INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,
                           created_cycle,build_target_id,target_set_hash)
      VALUES (1,1,1,1,'e1','factory','created',1,1,'h0');
    INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status)
      VALUES (1,1,1,1,1,'factory','success');
    INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope)
      VALUES (1,1,1,1,1,0.9,'aggregate');
    UPDATE evaluation SET status='success', canonical_attempt_id=1 WHERE id=1;

    -- 结论 + 证据（evaluation 证据指向成功测量）→ 关问 answered（走 I3）
    INSERT INTO answer(id,question_id,goal_id,goal_ver,cycle_id,verdict,answer_md)
      VALUES (1,1,1,1,1,'answered','a');
    INSERT INTO evidence(id,answer_id,question_id,goal_id,goal_ver,kind,
                         evaluation_id,evaluation_attempt_id,metric_result_id,metric_id,metric_ver,scope,claim_md)
      VALUES (1,1,1,1,1,'evaluation',1,1,1,1,1,'aggregate','because');
    UPDATE question SET status='answered' WHERE id=1;

    INSERT INTO decision(id,actor,type,payload_json) VALUES (1,'agent','note','{}');
    INSERT INTO ledger(id,cycle_id,phase,evaluation_attempt_id,policy_version) VALUES (1,1,'idea',1,'v0');
    INSERT INTO phase_commit(id,cycle_id,stage,artifact_hash) VALUES (1,1,'reasoning','ph');
    """)
    conn.commit()


@pytest.fixture()
def seeded_conn():
    """内存库 + seed_minimal（CP2.1/CP2.2 否定用例夹具）。"""
    c = _db.connect(":memory:")
    seed_minimal(c)
    yield c
    c.close()


@pytest.fixture(scope="session")
def system_root() -> Path:
    return SYSTEM_ROOT


@pytest.fixture()
def docker_workspace_tmp_path() -> Path:
    """Put nested-Docker bind fixtures on the production VEPFS mount domain.

    Pytest's default ``/tmp`` is on an outer overlay, while the rootless Docker
    daemon consumes workspace paths through the VEPFS bindfs mapping.  A long
    suite can therefore expose a freshly populated ``/tmp`` bind as stale or
    empty.  Real container tests use this fixture so their bind visibility
    matches production work roots; ordinary unit tests retain ``tmp_path``.
    """
    parent = SYSTEM_ROOT / "runtime" / "pytest-docker"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent.chmod(0o700)
    path = Path(tempfile.mkdtemp(prefix="docker-test-", dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def load_schema(name: str) -> dict:
    """按短名加载 schema（如 'plan' → schemas/plan.schema.json）。"""
    path = SCHEMAS_DIR / f"{name}.schema.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def schema_registry() -> Registry:
    """全部 schema 按 $id 入注册表——跨文件 $ref（如 idea_audit → idea_set.$defs.audit_score）由此解析。"""
    resources = []
    for path in SCHEMAS_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], DRAFT202012.create_resource(schema)))
    return Registry().with_resources(resources)


def make_validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name), registry=schema_registry())


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def iter_fixture_cases(kind: str):
    """遍历 fixtures/<kind>/<schema_name>/*.json → (schema_name, 文件路径)。

    kind ∈ {valid, invalid}。目录名 = schema 短名，一目录多用例。
    """
    base = FIXTURES_DIR / kind
    if not base.is_dir():
        return
    for schema_dir in sorted(base.iterdir()):
        if not schema_dir.is_dir():
            continue
        for case in sorted(schema_dir.glob("*.json")):
            yield schema_dir.name, case
