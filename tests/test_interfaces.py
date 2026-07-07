"""接口缝冒烟：orchestrator/interfaces.py 在运行时 Python（3.9）可导入、数据类可实例化。

把「Python 3.9 兼容」与「Protocol 清单齐全」从口头变成断言（§6.10 签名冻结的最小守卫）。
"""
import typing

from orchestrator import interfaces as ifc

EXPECTED_PROTOCOLS = [
    # 核心（§6.10 上表）
    "Runner", "Compiler", "Gate", "Ctx", "Recall", "StateStore", "Advancer",
    # v2.3 子系统（§6.10 下表）
    "WriteDaemon", "Connector", "Classifier", "Responder", "StatusPublisher",
    "InteractionStore", "Importer", "LicenseReviewer", "ObservationExtractor",
    "PhaseCommitStore",
]


def test_protocol_inventory():
    for name in EXPECTED_PROTOCOLS:
        obj = getattr(ifc, name)
        assert isinstance(obj, type) and typing.get_origin(obj) is None, name
        assert issubclass(obj, typing.Protocol), f"{name} 应为 Protocol"


def test_dataclasses_instantiable():
    pack = ifc.ContextPack(cycle_id="c1", stage="idea", target_id=None,
                           anchor_md="锚", neighborhood_md="", retrieval_md="")
    assert pack.refs == [] and pack.pack_hash == ""

    art = ifc.Artifact(stage="reasoning", files={"selection.json": {}}, md="正文")
    assert "selection.json" in art.files

    sel = ifc.Selection(next_question_id=None, next_intent="terminate")
    assert sel.scores == []

    outcome = ifc.PlanOutcome()   # reasoning-only 轮传空对象（derive_next_route 契约）
    assert not outcome.blocked

    cmd = ifc.WriteCommand(kind="state:set_route", payload={}, idempotency_key="k1")
    assert cmd.txn_scope == "short"
