-- 0001_appendix_a.sql —— 唯一规范 schema（逐字摘自 reference/第一部分-系统架构设计.md 附录 A，
-- 行 918–1614；v2.2 终稿 + v2.3/v2.4/v2.4.1 增量已并入原文）。
-- 治理：本文件为字节冻结对象——checksum 由 orchestrator/database.py 锁定，任何改动 = 决策性改动
-- 走检查点评审；OPEN 三裁定见 ../README.md。目标计数：36 表 / 72 触发器 / 29 索引（含 UNIQUE 自动索引）/ 1 视图。
PRAGMA foreign_keys = ON;

-- ===================== 问题树 / 周期 =====================
CREATE TABLE goal (
  id INTEGER NOT NULL, version INTEGER NOT NULL,
  text TEXT NOT NULL, predicate_json TEXT NOT NULL,
  previous_version INTEGER,
  created_cycle INTEGER REFERENCES cycle(id),
  directive_id  INTEGER REFERENCES directive(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id, version),
  FOREIGN KEY (id, previous_version) REFERENCES goal(id, version)
);

CREATE TABLE cycle (
  id INTEGER PRIMARY KEY,
  goal_id INTEGER NOT NULL, goal_ver INTEGER NOT NULL,
  active_question_id INTEGER REFERENCES question(id),
  status TEXT NOT NULL CHECK (status IN ('created','idea','plan','bundle','reasoning','done','aborted','failed')),
  route  TEXT CHECK (route IN ('bootstrap','attack','decompose','reuse_only','eval_only','goal_amend','dependency_wait')),
  cost_total REAL NOT NULL DEFAULT 0,
  policy_version TEXT NOT NULL,
  started_at_step TEXT, failure_kind TEXT,
  next_question_id INTEGER REFERENCES question(id),
  next_intent TEXT CHECK (next_intent IS NULL OR next_intent IN ('attack','decompose','terminate')),
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
  FOREIGN KEY (goal_id, goal_ver) REFERENCES goal(id, version)
);

CREATE TABLE question (
  id INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES question(id),
  goal_id INTEGER NOT NULL, goal_ver INTEGER NOT NULL, born_goal_ver INTEGER NOT NULL,
  text TEXT NOT NULL, predicate_json TEXT,
  status TEXT NOT NULL CHECK (status IN ('open','active','answered','refuted','inconclusive','dead_end')),
  visit_count INTEGER NOT NULL DEFAULT 0, decompose_count INTEGER NOT NULL DEFAULT 0,
  score REAL, est_cost REAL,
  source TEXT NOT NULL CHECK (source IN ('agent','human','decompose','goal_amend','revalidate')),
  born_cycle INTEGER REFERENCES cycle(id), active_cycle INTEGER REFERENCES cycle(id), closed_cycle INTEGER REFERENCES cycle(id),
  FOREIGN KEY (goal_id, goal_ver) REFERENCES goal(id, version),
  CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE TABLE question_dep (
  id INTEGER PRIMARY KEY,
  question_id INTEGER NOT NULL REFERENCES question(id),
  dep_type TEXT NOT NULL CHECK (dep_type IN ('question','baseline')),
  depends_on_question_id INTEGER REFERENCES question(id),
  depends_on_baseline_id INTEGER REFERENCES baseline(id),
  status TEXT NOT NULL CHECK (status IN ('pending','satisfied','blocked')),
  created_cycle INTEGER REFERENCES cycle(id),
  CHECK (
    (dep_type='question' AND depends_on_question_id IS NOT NULL AND depends_on_question_id <> question_id AND depends_on_baseline_id IS NULL) OR
    (dep_type='baseline' AND depends_on_baseline_id IS NOT NULL AND depends_on_question_id IS NULL)
  )
);

CREATE TABLE idea (
  id INTEGER PRIMARY KEY,
  question_id INTEGER NOT NULL REFERENCES question(id),
  cycle_id INTEGER NOT NULL REFERENCES cycle(id),
  content_md TEXT NOT NULL, novelty_refs_json TEXT, audit_score REAL, audit_json TEXT,
  status TEXT NOT NULL CHECK (status IN ('candidate','selected','failed')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_idea_selected_per_cycle ON idea(cycle_id, question_id) WHERE status='selected';

-- ===================== baseline 池：对象模型 =====================
CREATE TABLE baseline (
  id INTEGER PRIMARY KEY, slug TEXT NOT NULL, canonical_key TEXT NOT NULL UNIQUE,
  parent_id INTEGER REFERENCES baseline(id),
  code_ref TEXT, commit_hash TEXT, identity_doc TEXT, capability_summary TEXT,
  born_cycle INTEGER REFERENCES cycle(id),
  status TEXT NOT NULL CHECK (status IN ('planned','building','legal','build_failed','abandoned','deprecated')),
  provenance TEXT NOT NULL DEFAULT 'self_built' CHECK (provenance IN ('self_built','external_import')),   -- v2.3：自建 / 外部导入
  license_status TEXT NOT NULL DEFAULT 'na' CHECK (license_status IN ('allow','deny','review','na')),
  CHECK (provenance <> 'external_import' OR license_status = 'allow')   -- 外部导入基线 license 必须 allow（经 external_import 闸保证）
);
CREATE TABLE baseline_tag (
  baseline_id INTEGER NOT NULL REFERENCES baseline(id), tag TEXT NOT NULL,
  PRIMARY KEY (baseline_id, tag)
);
CREATE TABLE variant (
  id INTEGER PRIMARY KEY, baseline_id INTEGER NOT NULL REFERENCES baseline(id),
  variant_key TEXT NOT NULL, config_json TEXT NOT NULL, has_overrides INTEGER NOT NULL DEFAULT 0,
  born_question INTEGER REFERENCES question(id), result_summary TEXT, env_hash TEXT, summary_doc TEXT,
  status TEXT NOT NULL CHECK (status IN ('planned','building','legal','build_failed','abandoned','deprecated')),
  UNIQUE (baseline_id, variant_key)                         -- 无 protocol 列
);
CREATE TABLE run (
  id INTEGER PRIMARY KEY, cycle_id INTEGER NOT NULL REFERENCES cycle(id), variant_id INTEGER NOT NULL REFERENCES variant(id), build_target_id INTEGER NOT NULL REFERENCES build_target(id),
  kind TEXT NOT NULL CHECK (kind IN ('build','exec','import')), seed INTEGER,   -- import=登记外部模型/建索引缓存（审计用，非训练）
  status TEXT NOT NULL CHECK (status IN ('created','running','success','failed')),
  failure_kind TEXT CHECK (failure_kind IS NULL OR failure_kind IN ('build','smoke','timeout','runtime','data_invalid','aborted')), commit_hash TEXT, env_hash TEXT, cost REAL,
  CHECK (status <> 'failed' OR failure_kind IS NOT NULL)
);
CREATE TABLE checkpoint (   -- 可评 target（权重 checkpoint 是常见情形；亦含外部模型/prompt/算法/检索索引）
  id INTEGER PRIMARY KEY, variant_id INTEGER NOT NULL REFERENCES variant(id),
  ckpt_key TEXT NOT NULL, path TEXT NOT NULL, content_hash TEXT NOT NULL, hash_alg TEXT NOT NULL,
  artifact_type TEXT NOT NULL DEFAULT 'checkpoint' CHECK (artifact_type IN ('checkpoint','external_model','prompt_only','algorithm','retrieval_index')),
  origin TEXT NOT NULL DEFAULT 'run_produced' CHECK (origin IN ('run_produced','external_import','none')),
  manifest_hash TEXT, source_uri TEXT, revision TEXT,   -- manifest_hash 为附加语义哈希（与 content_hash 不互斥；content_hash 仍必填）
  produced_by_run INTEGER REFERENCES run(id),
  UNIQUE (variant_id, ckpt_key),
  CHECK (origin <> 'run_produced'   OR produced_by_run IS NOT NULL),                                    -- 本池产出须有 run
  CHECK (origin <> 'external_import' OR (source_uri IS NOT NULL AND revision IS NOT NULL AND manifest_hash IS NOT NULL)),  -- 外部 target 须可溯源+digest
  CHECK (origin <> 'none' OR produced_by_run IS NULL)                                                   -- prompt/算法等无本池 run
);
CREATE TABLE protocol (
  id INTEGER NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
  scope_spec_json TEXT NOT NULL, derived_from_question INTEGER REFERENCES question(id),
  PRIMARY KEY (id, version)
);
CREATE TABLE metric_def (
  id INTEGER NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('higher','lower')), unit TEXT, compute_spec TEXT, readout_rule TEXT,
  PRIMARY KEY (id, version)
);
CREATE TABLE protocol_metric (
  protocol_id INTEGER NOT NULL, protocol_ver INTEGER NOT NULL, metric_id INTEGER NOT NULL, metric_ver INTEGER NOT NULL,
  PRIMARY KEY (protocol_id, protocol_ver, metric_id, metric_ver),
  FOREIGN KEY (protocol_id, protocol_ver) REFERENCES protocol(id, version),
  FOREIGN KEY (metric_id, metric_ver)     REFERENCES metric_def(id, version)
);
CREATE TABLE build_target (
  id INTEGER PRIMARY KEY, cycle_id INTEGER NOT NULL REFERENCES cycle(id), question_id INTEGER REFERENCES question(id),
  target_kind TEXT NOT NULL CHECK (target_kind IN ('build','exec','eval','import')),   -- import 与 run.kind='import' 一致（trg_run_target_consistent）
  seq INTEGER NOT NULL, critical INTEGER NOT NULL DEFAULT 1 CHECK (critical IN (0,1)),
  status TEXT NOT NULL CHECK (status IN ('pending','building','smoke','running','complete','skipped','failed','engineering_blocked')),
  baseline_id INTEGER REFERENCES baseline(id), variant_id INTEGER REFERENCES variant(id),
  evaluation_id INTEGER REFERENCES evaluation(id),
  eval_action TEXT CHECK (eval_action IS NULL OR eval_action IN ('create_evaluation','append_attempt')),
  attempt_purpose TEXT CHECK (attempt_purpose IS NULL OR attempt_purpose IN ('factory','retry','metric_append','repro_eval','standalone_eval','protocol_upgrade')),
  evaluation_source TEXT CHECK (evaluation_source IS NULL OR evaluation_source IN ('factory','protocol_upgrade','standalone_eval')), eval_key TEXT,
  budget_estimate REAL, failure_kind TEXT, plan_ref TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE (cycle_id, seq),
  CHECK (target_kind <> 'eval' OR eval_action IS NOT NULL),
  CHECK (eval_action <> 'append_attempt' OR evaluation_id IS NOT NULL),
  CHECK (eval_action <> 'create_evaluation' OR (eval_key IS NOT NULL AND evaluation_source IS NOT NULL))
);

-- 对照元：逻辑测量单元（变体 × 评估协议@版本）—— 唯一化到 (variant,protocol,ver)
CREATE TABLE evaluation (
  id INTEGER PRIMARY KEY,
  variant_id INTEGER NOT NULL REFERENCES variant(id),
  protocol_id INTEGER NOT NULL, protocol_ver INTEGER NOT NULL,
  eval_key TEXT NOT NULL,                                    -- variant 内唯一的文件夹标签（UNIQUE(variant_id,eval_key)）
  source TEXT NOT NULL CHECK (source IN ('factory','protocol_upgrade','standalone_eval')),
  status TEXT NOT NULL CHECK (status IN ('created','running','success','failed','abandoned')),
  canonical_attempt_id INTEGER REFERENCES evaluation_attempt(id),
  created_cycle INTEGER NOT NULL REFERENCES cycle(id),
  build_target_id INTEGER REFERENCES build_target(id),
  cost REAL NOT NULL DEFAULT 0,
  target_set_hash TEXT NOT NULL CHECK (length(target_set_hash) > 0),   -- I6：本 eval 消费 target 集合的规范哈希（selector 确定性产物；INSERT 时写、身份冻结；空集亦有定值哈希）
  FOREIGN KEY (protocol_id, protocol_ver) REFERENCES protocol(id, version),
  UNIQUE (variant_id, protocol_id, protocol_ver),           -- 复用判定 O(1)；一格子一 evaluation
  UNIQUE (variant_id, eval_key)                             -- 防 evaluations/<eval-key>/ 路径碰撞
);

-- 一次真实评估执行；同格子的 retry/复现/补指标都是 attempt
CREATE TABLE evaluation_attempt (
  id INTEGER PRIMARY KEY, evaluation_id INTEGER NOT NULL REFERENCES evaluation(id),
  cycle_id INTEGER NOT NULL REFERENCES cycle(id), build_target_id INTEGER REFERENCES build_target(id),
  attempt_no INTEGER NOT NULL,
  purpose TEXT NOT NULL CHECK (purpose IN ('factory','retry','metric_append','repro_eval','standalone_eval','protocol_upgrade')),
  status TEXT NOT NULL CHECK (status IN ('created','running','success','failed','aborted')),
  failure_kind TEXT CHECK (failure_kind IS NULL OR failure_kind IN
    ('timeout','runtime','data_invalid','metric_missing','protocol_violation','env_invalid','artifact_invalid','aborted')),
  retry_of INTEGER REFERENCES evaluation_attempt(id),
  commit_hash TEXT, env_hash TEXT, transcript_ref TEXT, artifact_ref TEXT,
  started_cycle INTEGER REFERENCES cycle(id), completed_cycle INTEGER REFERENCES cycle(id),
  watchdog_sec REAL, cost REAL NOT NULL DEFAULT 0,
  CHECK (status <> 'failed' OR failure_kind IS NOT NULL),
  UNIQUE (evaluation_id, attempt_no)
);

CREATE TABLE metric_result (
  id INTEGER PRIMARY KEY,
  evaluation_id INTEGER NOT NULL REFERENCES evaluation(id),
  evaluation_attempt_id INTEGER NOT NULL REFERENCES evaluation_attempt(id),
  checkpoint_id INTEGER REFERENCES checkpoint(id),
  metric_id INTEGER NOT NULL, metric_ver INTEGER NOT NULL, value REAL NOT NULL,
  scope TEXT NOT NULL CHECK (scope IN ('fold','aggregate')),
  CHECK ((scope='fold' AND checkpoint_id IS NOT NULL) OR (scope='aggregate' AND checkpoint_id IS NULL)),
  FOREIGN KEY (metric_id, metric_ver) REFERENCES metric_def(id, version)
);
-- aggregate 因 NULL 不去重：拆两个 partial unique index
CREATE UNIQUE INDEX uq_mr_fold ON metric_result(evaluation_attempt_id, checkpoint_id, metric_id, metric_ver) WHERE scope='fold';
CREATE UNIQUE INDEX uq_mr_agg  ON metric_result(evaluation_attempt_id, metric_id, metric_ver) WHERE scope='aggregate';
-- 召回/排行榜：协议中心反查（"数据集 X 上谁最强"）+ run kind 过滤（benchmark/数据集中心研究入口；数据集仍走 protocol，不进 tag）
CREATE INDEX idx_evaluation_protocol_status ON evaluation(protocol_id, protocol_ver, status);
CREATE INDEX idx_run_kind_status ON run(kind, status);

-- ===================== 结论 / 证据 / 账本 / 卡片 =====================
CREATE TABLE answer (
  id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL REFERENCES question(id),
  goal_id INTEGER NOT NULL, goal_ver INTEGER NOT NULL, cycle_id INTEGER NOT NULL REFERENCES cycle(id),
  verdict TEXT NOT NULL CHECK (verdict IN ('answered','refuted')), answer_md TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (goal_id, goal_ver) REFERENCES goal(id, version)
);
CREATE UNIQUE INDEX uq_answer_one_per_question_goal ON answer(question_id, goal_id, goal_ver);

CREATE TABLE answer_applicability (
  id INTEGER PRIMARY KEY, answer_id INTEGER NOT NULL REFERENCES answer(id),
  goal_id INTEGER NOT NULL, goal_ver INTEGER NOT NULL, audit_cycle INTEGER REFERENCES cycle(id),
  status TEXT NOT NULL CHECK (status IN ('pending','blocked','still_applicable','needs_revalidation','obsolete','contradicted')),
  rationale_md TEXT, spawned_question_id INTEGER REFERENCES question(id),
  FOREIGN KEY (goal_id, goal_ver) REFERENCES goal(id, version),
  UNIQUE (answer_id, goal_id, goal_ver),
  CHECK (status NOT IN ('needs_revalidation','contradicted') OR spawned_question_id IS NOT NULL)
);

CREATE TABLE decision (
  id INTEGER PRIMARY KEY, cycle_id INTEGER REFERENCES cycle(id), question_id INTEGER REFERENCES question(id),
  directive_id INTEGER REFERENCES directive(id),
  actor TEXT NOT NULL CHECK (actor IN ('agent','gate','judge','human','orchestrator')),
  type TEXT NOT NULL, prompt_version TEXT, policy_version TEXT, payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE directive (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('set_budget','pause','resume','abort_cycle','inject_question','reprioritize','prune_branch','goal_amend','note')),
  hardness TEXT NOT NULL CHECK (hardness IN ('hard','soft')),
  status TEXT NOT NULL CHECK (status IN ('pending','consumed','rejected','superseded')),
  consume_at TEXT NOT NULL CHECK (consume_at IN ('immediate','stage_boundary','reasoning_start')),
  payload_json TEXT NOT NULL, created_cycle INTEGER REFERENCES cycle(id), consumed_cycle INTEGER REFERENCES cycle(id),
  consumed_decision_id INTEGER REFERENCES decision(id),
  source_interaction_message_id INTEGER REFERENCES interaction_message(id),   -- v2.3：directive 若由人机消息派生则回指来源（护「分类→directive」provenance）
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE evidence (
  id INTEGER PRIMARY KEY, answer_id INTEGER NOT NULL REFERENCES answer(id), question_id INTEGER NOT NULL REFERENCES question(id),
  goal_id INTEGER NOT NULL, goal_ver INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('evaluation','child_answer','human','literature')),
  evaluation_id INTEGER REFERENCES evaluation(id), evaluation_attempt_id INTEGER REFERENCES evaluation_attempt(id),
  metric_result_id INTEGER REFERENCES metric_result(id),
  metric_id INTEGER, metric_ver INTEGER, scope TEXT CHECK (scope IS NULL OR scope IN ('fold','aggregate')),
  child_answer_id INTEGER REFERENCES answer(id), human_decision_id INTEGER REFERENCES decision(id), literature_ref TEXT,
  claim_md TEXT NOT NULL, created_cycle INTEGER REFERENCES cycle(id), valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0,1)),
  FOREIGN KEY (goal_id, goal_ver) REFERENCES goal(id, version),
  CHECK (
    (kind='evaluation'   AND evaluation_id IS NOT NULL AND metric_result_id IS NOT NULL AND metric_id IS NOT NULL AND metric_ver IS NOT NULL
        AND child_answer_id IS NULL AND human_decision_id IS NULL AND literature_ref IS NULL)
    OR (kind='child_answer' AND child_answer_id IS NOT NULL
        AND evaluation_id IS NULL AND evaluation_attempt_id IS NULL AND metric_result_id IS NULL AND metric_id IS NULL AND metric_ver IS NULL AND scope IS NULL
        AND human_decision_id IS NULL AND literature_ref IS NULL)
    OR (kind='human'        AND human_decision_id IS NOT NULL
        AND evaluation_id IS NULL AND evaluation_attempt_id IS NULL AND metric_result_id IS NULL AND metric_id IS NULL AND metric_ver IS NULL AND scope IS NULL
        AND child_answer_id IS NULL AND literature_ref IS NULL)
    OR (kind='literature'   AND literature_ref IS NOT NULL
        AND evaluation_id IS NULL AND evaluation_attempt_id IS NULL AND metric_result_id IS NULL AND metric_id IS NULL AND metric_ver IS NULL AND scope IS NULL
        AND child_answer_id IS NULL AND human_decision_id IS NULL)
  )
);

CREATE TABLE runner_call (
  id INTEGER PRIMARY KEY, cycle_id INTEGER REFERENCES cycle(id),
  phase TEXT NOT NULL CHECK (phase IN ('idea','plan','bundle','reasoning','audit','watchdog','orchestrator','interaction_query','import_search')),   -- v2.3：+人机查询应答 / 外部检索（均只读）
  purpose TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('created','running','success','failed','aborted')),
  prompt_version TEXT, policy_version TEXT, transcript_ref TEXT, failure_kind TEXT, started_at TEXT, finished_at TEXT
);
CREATE TABLE ledger (
  id INTEGER PRIMARY KEY, cycle_id INTEGER NOT NULL REFERENCES cycle(id),
  phase TEXT NOT NULL CHECK (phase IN ('idea','plan','bundle','reasoning','audit','watchdog','orchestrator','interaction_query','import_search')),
  run_id INTEGER REFERENCES run(id), evaluation_id INTEGER REFERENCES evaluation(id),
  evaluation_attempt_id INTEGER REFERENCES evaluation_attempt(id), runner_call_id INTEGER REFERENCES runner_call(id),
  tokens_input INTEGER NOT NULL DEFAULT 0, tokens_output INTEGER NOT NULL DEFAULT 0, tokens_total INTEGER NOT NULL DEFAULT 0,
  wallclock_sec REAL NOT NULL DEFAULT 0, money REAL NOT NULL DEFAULT 0, policy_version TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (run_id IS NOT NULL OR evaluation_id IS NOT NULL OR evaluation_attempt_id IS NOT NULL OR runner_call_id IS NOT NULL)
);
CREATE TABLE card (
  id INTEGER PRIMARY KEY,
  card_type TEXT NOT NULL CHECK (card_type IN ('question','subtree','family','baseline','variant','failure','protocol')),
  ref_id INTEGER NOT NULL, goal_id INTEGER, goal_ver INTEGER, card_md TEXT NOT NULL, src_hash TEXT NOT NULL,
  updated_cycle INTEGER REFERENCES cycle(id), stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0,1)),
  UNIQUE (card_type, ref_id)
);

-- ===================== 触发器：不变量机械落死 =====================
-- I2 履约 + attempt 归属 + 跨变体一致性
CREATE TRIGGER trg_mr_i2_ins BEFORE INSERT ON metric_result
BEGIN
  SELECT CASE WHEN (SELECT evaluation_id FROM evaluation_attempt WHERE id=NEW.evaluation_attempt_id) <> NEW.evaluation_id
    THEN RAISE(ABORT,'metric_result: attempt not under evaluation') END;
  SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM evaluation e JOIN protocol_metric pm
      ON pm.protocol_id=e.protocol_id AND pm.protocol_ver=e.protocol_ver
      WHERE e.id=NEW.evaluation_id AND pm.metric_id=NEW.metric_id AND pm.metric_ver=NEW.metric_ver)
    THEN RAISE(ABORT,'I2: metric not declared by evaluation protocol') END;
  SELECT CASE WHEN NEW.checkpoint_id IS NOT NULL
      AND (SELECT c.variant_id FROM checkpoint c WHERE c.id=NEW.checkpoint_id)
       <> (SELECT e.variant_id FROM evaluation e WHERE e.id=NEW.evaluation_id)
    THEN RAISE(ABORT,'checkpoint 与 evaluation 不属于同一 variant') END;
END;
-- success ⇔ canonical（INSERT+UPDATE 都校验），canonical 必须是本 eval 成功 attempt
CREATE TRIGGER trg_eval_sc_ins BEFORE INSERT ON evaluation
BEGIN
  SELECT CASE WHEN NEW.status='success' AND NEW.canonical_attempt_id IS NULL THEN RAISE(ABORT,'success evaluation 必须有 canonical_attempt') END;
  SELECT CASE WHEN NEW.canonical_attempt_id IS NOT NULL AND NEW.status<>'success' THEN RAISE(ABORT,'canonical 非空时 evaluation 必须 success') END;
  SELECT CASE WHEN NEW.canonical_attempt_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM evaluation_attempt ea WHERE ea.id=NEW.canonical_attempt_id AND ea.evaluation_id=NEW.id AND ea.status='success')
    THEN RAISE(ABORT,'canonical 必须是本 evaluation 的成功 attempt') END;
END;
CREATE TRIGGER trg_eval_sc_upd BEFORE UPDATE ON evaluation
BEGIN
  SELECT CASE WHEN NEW.status='success' AND NEW.canonical_attempt_id IS NULL THEN RAISE(ABORT,'success evaluation 必须有 canonical_attempt') END;
  SELECT CASE WHEN NEW.canonical_attempt_id IS NOT NULL AND NEW.status<>'success' THEN RAISE(ABORT,'canonical 非空时 evaluation 必须 success') END;
  SELECT CASE WHEN NEW.canonical_attempt_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM evaluation_attempt ea WHERE ea.id=NEW.canonical_attempt_id AND ea.evaluation_id=NEW.id AND ea.status='success')
    THEN RAISE(ABORT,'canonical 必须是本 evaluation 的成功 attempt') END;
  SELECT CASE WHEN NEW.variant_id<>OLD.variant_id OR NEW.protocol_id<>OLD.protocol_id OR NEW.protocol_ver<>OLD.protocol_ver
      OR NEW.source<>OLD.source OR NEW.created_cycle<>OLD.created_cycle OR NEW.eval_key<>OLD.eval_key
      OR NEW.target_set_hash<>OLD.target_set_hash
    THEN RAISE(ABORT,'evaluation 身份字段不可变（含 target_set_hash，I6）') END;
  SELECT CASE WHEN OLD.status='success' AND NEW.status NOT IN ('success','abandoned')
    THEN RAISE(ABORT,'success evaluation 不可回退（只可 success/abandoned）') END;
END;
CREATE TRIGGER trg_eval_nodel BEFORE DELETE ON evaluation BEGIN SELECT RAISE(ABORT,'evaluation 不可删除'); END;
-- 一致性：checkpoint/run 同变体；attempt.retry_of 同 evaluation
CREATE TRIGGER trg_ckpt_run_var BEFORE INSERT ON checkpoint WHEN NEW.produced_by_run IS NOT NULL
BEGIN SELECT CASE WHEN (SELECT variant_id FROM run WHERE id=NEW.produced_by_run)<>NEW.variant_id THEN RAISE(ABORT,'checkpoint 与 run 不同 variant') END; END;
CREATE TRIGGER trg_attempt_retry BEFORE INSERT ON evaluation_attempt WHEN NEW.retry_of IS NOT NULL
BEGIN SELECT CASE WHEN (SELECT evaluation_id FROM evaluation_attempt WHERE id=NEW.retry_of)<>NEW.evaluation_id THEN RAISE(ABORT,'retry_of 跨 evaluation') END; END;
-- 终态 attempt 冻结 + 不可删
CREATE TRIGGER trg_attempt_frozen BEFORE UPDATE ON evaluation_attempt WHEN OLD.status IN ('success','failed','aborted')
BEGIN SELECT RAISE(ABORT,'终态 evaluation_attempt 冻结'); END;
CREATE TRIGGER trg_attempt_nodel BEFORE DELETE ON evaluation_attempt BEGIN SELECT RAISE(ABORT,'evaluation_attempt 不可删除'); END;
-- I3：关问题须有有效证据；问题初始状态只能 open/active
CREATE TRIGGER trg_q_init BEFORE INSERT ON question WHEN NEW.status NOT IN ('open','active')
BEGIN SELECT RAISE(ABORT,'question 初始状态只能 open/active'); END;
CREATE TRIGGER trg_q_i3 BEFORE UPDATE OF status, goal_id, goal_ver ON question WHEN NEW.status IN ('answered','refuted')
BEGIN SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM answer a JOIN evidence e ON e.answer_id=a.id
    WHERE a.question_id=NEW.id AND a.goal_id=NEW.goal_id AND a.goal_ver=NEW.goal_ver AND a.verdict=NEW.status AND e.valid=1)
  THEN RAISE(ABORT,'I3: answered/refuted question requires valid evidence（当前 goal 版本）') END; END;
CREATE TRIGGER trg_q_closed_goalfrozen BEFORE UPDATE OF goal_id, goal_ver ON question
WHEN OLD.status IN ('answered','refuted','dead_end') AND (NEW.goal_id<>OLD.goal_id OR NEW.goal_ver<>OLD.goal_ver)
BEGIN SELECT RAISE(ABORT,'已关闭问题不可改 goal 版本（脱离证据当前版本）'); END;
-- answer 绑 question 当前 goal 版本；evidence 绑其 answer 的 goal 版本 + 一致性
CREATE TRIGGER trg_answer_goalver BEFORE INSERT ON answer
BEGIN SELECT CASE WHEN NEW.goal_id<>(SELECT goal_id FROM question WHERE id=NEW.question_id)
   OR NEW.goal_ver<>(SELECT goal_ver FROM question WHERE id=NEW.question_id)
  THEN RAISE(ABORT,'answer 的 goal 版本须等于 question 当前 goal 版本') END; END;
CREATE TRIGGER trg_evidence_qa BEFORE INSERT ON evidence
BEGIN
  SELECT CASE WHEN (SELECT question_id FROM answer WHERE id=NEW.answer_id) <> NEW.question_id THEN RAISE(ABORT,'evidence question_id 须等于 answer.question_id') END;
  SELECT CASE WHEN NEW.goal_id<>(SELECT goal_id FROM answer WHERE id=NEW.answer_id) OR NEW.goal_ver<>(SELECT goal_ver FROM answer WHERE id=NEW.answer_id)
    THEN RAISE(ABORT,'evidence 的 goal 版本须等于其 answer') END;
END;
CREATE TRIGGER trg_evidence_eval_valid BEFORE INSERT ON evidence WHEN NEW.kind='evaluation'
BEGIN SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM metric_result mr JOIN evaluation_attempt ea ON ea.id=mr.evaluation_attempt_id
      JOIN evaluation e ON e.id=mr.evaluation_id
    WHERE mr.id=NEW.metric_result_id AND mr.evaluation_id=NEW.evaluation_id
      AND ea.status='success' AND e.status='success'
      AND mr.metric_id=NEW.metric_id AND mr.metric_ver=NEW.metric_ver AND (NEW.scope IS NULL OR mr.scope=NEW.scope))
  THEN RAISE(ABORT,'evaluation 证据须指向成功评估的具体 metric_result') END; END;
CREATE TRIGGER trg_evidence_child BEFORE INSERT ON evidence WHEN NEW.kind='child_answer'
BEGIN
  SELECT CASE WHEN (SELECT question_id FROM answer WHERE id=NEW.child_answer_id) = NEW.question_id
    THEN RAISE(ABORT,'child_answer 不能引用同一问题的 answer') END;
  SELECT CASE WHEN (SELECT q.status FROM answer a JOIN question q ON q.id=a.question_id WHERE a.id=NEW.child_answer_id) NOT IN ('answered','refuted')
    THEN RAISE(ABORT,'child_answer 的子问题须已关闭(answered/refuted)') END;
  SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM answer a WHERE a.id=NEW.child_answer_id AND (
      (a.goal_id=NEW.goal_id AND a.goal_ver=NEW.goal_ver)
      OR EXISTS (SELECT 1 FROM answer_applicability aa WHERE aa.answer_id=a.id AND aa.goal_id=NEW.goal_id AND aa.goal_ver=NEW.goal_ver AND aa.status='still_applicable')))
    THEN RAISE(ABORT,'child_answer evidence is not applicable to this goal version') END;
END;
-- I1 / append-only：不可变事实表禁止 UPDATE/DELETE
CREATE TRIGGER trg_protocol_noupd BEFORE UPDATE ON protocol BEGIN SELECT RAISE(ABORT,'protocol append-only（改场景升 version）'); END;
CREATE TRIGGER trg_protocol_nodel BEFORE DELETE ON protocol BEGIN SELECT RAISE(ABORT,'protocol 不可删'); END;
CREATE TRIGGER trg_metricdef_noupd BEFORE UPDATE ON metric_def BEGIN SELECT RAISE(ABORT,'metric_def append-only（改口径升 version）'); END;
CREATE TRIGGER trg_metricdef_nodel BEFORE DELETE ON metric_def BEGIN SELECT RAISE(ABORT,'metric_def 不可删'); END;
CREATE TRIGGER trg_pm_noupd BEFORE UPDATE ON protocol_metric BEGIN SELECT RAISE(ABORT,'protocol_metric append-only'); END;
CREATE TRIGGER trg_pm_nodel BEFORE DELETE ON protocol_metric BEGIN SELECT RAISE(ABORT,'protocol_metric 不可删（只增）'); END;
CREATE TRIGGER trg_mr_noupd BEFORE UPDATE ON metric_result BEGIN SELECT RAISE(ABORT,'metric_result append-only'); END;
CREATE TRIGGER trg_mr_nodel BEFORE DELETE ON metric_result BEGIN SELECT RAISE(ABORT,'metric_result 不可删'); END;
CREATE TRIGGER trg_ckpt_noupd BEFORE UPDATE ON checkpoint BEGIN SELECT RAISE(ABORT,'checkpoint append-only'); END;
CREATE TRIGGER trg_ckpt_nodel BEFORE DELETE ON checkpoint BEGIN SELECT RAISE(ABORT,'checkpoint 不可删'); END;
CREATE TRIGGER trg_answer_noupd BEFORE UPDATE ON answer BEGIN SELECT RAISE(ABORT,'answer append-only'); END;
CREATE TRIGGER trg_answer_nodel BEFORE DELETE ON answer BEGIN SELECT RAISE(ABORT,'answer 不可删（护 I3）'); END;
CREATE TRIGGER trg_evidence_noupd BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT,'evidence append-only（护 I3）'); END;
CREATE TRIGGER trg_evidence_nodel BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT,'evidence 不可删（护 I3）'); END;
CREATE TRIGGER trg_decision_noupd BEFORE UPDATE ON decision BEGIN SELECT RAISE(ABORT,'decision append-only'); END;
CREATE TRIGGER trg_decision_nodel BEFORE DELETE ON decision BEGIN SELECT RAISE(ABORT,'decision 不可删'); END;
CREATE TRIGGER trg_ledger_noupd BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'ledger append-only'); END;
CREATE TRIGGER trg_ledger_nodel BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'ledger 不可删'); END;
-- goal append-only（改目标 = INSERT 新 version）
CREATE TRIGGER trg_goal_noupd BEFORE UPDATE ON goal BEGIN SELECT RAISE(ABORT,'goal append-only（改目标 INSERT 新 version）'); END;
CREATE TRIGGER trg_goal_nodel BEFORE DELETE ON goal BEGIN SELECT RAISE(ABORT,'goal 不可删'); END;
-- run 终态冻结 + 不可删
CREATE TRIGGER trg_run_frozen BEFORE UPDATE ON run WHEN OLD.status IN ('success','failed') BEGIN SELECT RAISE(ABORT,'终态 run 冻结'); END;
CREATE TRIGGER trg_run_nodel BEFORE DELETE ON run BEGIN SELECT RAISE(ABORT,'run 不可删'); END;
-- dead_end 关闭需 decision（prune 裁定/证据）
CREATE TRIGGER trg_q_deadend BEFORE UPDATE OF status ON question WHEN NEW.status='dead_end'
BEGIN SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM decision d WHERE d.question_id=NEW.id AND d.type='prune_branch') THEN RAISE(ABORT,'dead_end 关闭需 decision(type=prune_branch)') END; END;
-- 关闭问题不可重开（answered/refuted/dead_end 终态）
CREATE TRIGGER trg_q_no_reopen BEFORE UPDATE OF status ON question WHEN OLD.status IN ('answered','refuted','dead_end') AND NEW.status<>OLD.status
BEGIN SELECT RAISE(ABORT,'已关闭问题(answered/refuted/dead_end)不可重开'); END;
-- human 证据须指向 actor=human 的 decision
CREATE TRIGGER trg_evidence_human BEFORE INSERT ON evidence WHEN NEW.kind='human'
BEGIN SELECT CASE WHEN (SELECT actor FROM decision WHERE id=NEW.human_decision_id) <> 'human' THEN RAISE(ABORT,'human 证据须指向 actor=human 的 decision') END; END;
-- 身份/append-only 冻结：baseline/variant 身份字段不可变
CREATE TRIGGER trg_baseline_identity_frozen BEFORE UPDATE ON baseline
WHEN NEW.canonical_key<>OLD.canonical_key OR NEW.parent_id IS NOT OLD.parent_id OR NEW.slug<>OLD.slug
BEGIN SELECT RAISE(ABORT,'baseline 身份字段(canonical_key/parent_id/slug)不可变'); END;
CREATE TRIGGER trg_variant_identity_frozen BEFORE UPDATE ON variant
WHEN NEW.config_json<>OLD.config_json OR NEW.variant_key<>OLD.variant_key OR NEW.baseline_id<>OLD.baseline_id
BEGIN SELECT RAISE(ABORT,'variant 身份字段(config_json/variant_key/baseline_id)不可变(append-only)'); END;
-- #10 防环/重挂：question.parent_id 不可改
CREATE TRIGGER trg_question_parent_frozen BEFORE UPDATE OF parent_id ON question WHEN NEW.parent_id IS NOT OLD.parent_id
BEGIN SELECT RAISE(ABORT,'question.parent_id 不可改(防环/重挂)'); END;
-- #8 evidence.evaluation_attempt_id 必须等于 metric_result 的 attempt
CREATE TRIGGER trg_evidence_attempt_consistent BEFORE INSERT ON evidence WHEN NEW.kind='evaluation' AND NEW.evaluation_attempt_id IS NOT NULL
BEGIN SELECT CASE WHEN NEW.evaluation_attempt_id <> (SELECT evaluation_attempt_id FROM metric_result WHERE id=NEW.metric_result_id)
  THEN RAISE(ABORT,'evidence.evaluation_attempt_id 须等于 metric_result 的 attempt') END; END;
-- #4 required metric 集结构化落库（plan 写入；gate_finish_attempt 据此逐项核 aggregate 覆盖）
CREATE TABLE build_target_required_metric (
  build_target_id INTEGER NOT NULL REFERENCES build_target(id),
  metric_id INTEGER NOT NULL, metric_ver INTEGER NOT NULL,
  PRIMARY KEY (build_target_id, metric_id, metric_ver),
  FOREIGN KEY (metric_id, metric_ver) REFERENCES metric_def(id, version)
);
-- #2 run 与 build_target 绑定一致性（kind/cycle/variant）
CREATE TRIGGER trg_run_target_consistent BEFORE INSERT ON run
WHEN NOT EXISTS (SELECT 1 FROM build_target bt WHERE bt.id=NEW.build_target_id
    AND bt.target_kind=NEW.kind AND bt.cycle_id=NEW.cycle_id AND bt.variant_id=NEW.variant_id)
BEGIN SELECT RAISE(ABORT,'run 的 kind/cycle/variant 须与其 build_target 一致'); END;
-- #9 success evaluation 弃用/清空 canonical 双守卫：① 被 valid evidence 引用（证据 append-only 不可撤，修补 I3 洞）；② pool_publish：已发布(legal)变体的出厂 eval（发布基础不可凭空消失，要撤须先 deprecate 变体）
CREATE TRIGGER trg_eval_no_abandon_if_cited BEFORE UPDATE ON evaluation
WHEN OLD.status='success' AND (NEW.status<>'success' OR NEW.canonical_attempt_id IS NULL)
BEGIN
  SELECT CASE WHEN EXISTS (SELECT 1 FROM evidence WHERE evaluation_id=OLD.id AND kind='evaluation' AND valid=1)
    THEN RAISE(ABORT,'已被 valid evidence 引用的 success evaluation 不可弃用/清空 canonical') END;
  SELECT CASE WHEN OLD.source='factory' AND EXISTS (SELECT 1 FROM variant v WHERE v.id=OLD.variant_id AND v.status='legal')
    THEN RAISE(ABORT,'已发布(legal)变体的出厂 evaluation 不可弃用/清空 canonical（须先 deprecate 变体）') END;
END;
-- 阶段级原子提交的幂等落库（commit_phase 承载；唯一键防重复提交，kill-9 重启幂等：同键且 artifact_hash 相同才跳过、不同拒绝，§4.2.5）
CREATE TABLE phase_commit (
  id INTEGER PRIMARY KEY,
  cycle_id INTEGER NOT NULL REFERENCES cycle(id),
  stage TEXT NOT NULL CHECK (stage IN ('idea','plan','bundle','reasoning')),
  target_id INTEGER REFERENCES build_target(id),
  artifact_hash TEXT NOT NULL,
  committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX ux_phase_commit ON phase_commit (cycle_id, stage, IFNULL(target_id, -1));
-- 重要2 child_answer 证据须来自当前问题子树，或经 satisfied question_dep（与 §4.1.4 gate_close_question 一致，DB 层焊死）
CREATE TRIGGER trg_evidence_child_scope BEFORE INSERT ON evidence WHEN NEW.kind='child_answer'
BEGIN
  SELECT CASE WHEN NOT (
    EXISTS (WITH RECURSIVE sub(qid) AS (
              SELECT NEW.question_id
              UNION ALL SELECT q.id FROM question q JOIN sub ON q.parent_id=sub.qid)
            SELECT 1 FROM answer a
            WHERE a.id=NEW.child_answer_id AND a.question_id IN (SELECT qid FROM sub))
    OR EXISTS (SELECT 1 FROM answer a JOIN question_dep qd
                 ON qd.question_id=NEW.question_id AND qd.dep_type='question'
                    AND qd.depends_on_question_id=a.question_id AND qd.status='satisfied'
               WHERE a.id=NEW.child_answer_id)
  ) THEN RAISE(ABORT,'child_answer 证据须来自当前问题子树或 satisfied question_dep') END;
END;
-- 建议 phase_commit 冻结（恢复真相，append-only）
CREATE TRIGGER trg_phase_commit_noupd BEFORE UPDATE ON phase_commit BEGIN SELECT RAISE(ABORT,'phase_commit append-only（恢复真相）'); END;
CREATE TRIGGER trg_phase_commit_nodel BEFORE DELETE ON phase_commit BEGIN SELECT RAISE(ABORT,'phase_commit 不可删（恢复真相）'); END;

-- ===================== v2.3 增量：执行日志 / 外部 import / 人机交互 =====================
-- 结构纪律（写入纪律见 §4.1）：execution_log/execution_observation 与 interaction_* **不在任何 gate 判据输入集**——
--   门禁经 gate_input_* 只读视图取数（视图集不含这些表），且 evidence/metric_result/decision 无 FK 指向它们
--   （evidence.kind 不含 log/interaction）。故"日志/聊天/外部检索"永不成为证据、测量或门禁输入；只供 reasoning 解释与人机回话。

-- ① 执行日志（append-only；owner=run∨attempt 恰一；按 owner 限 log_kind）
CREATE TABLE execution_log (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES run(id),
  evaluation_attempt_id INTEGER REFERENCES evaluation_attempt(id),
  cycle_id INTEGER NOT NULL REFERENCES cycle(id),
  log_kind TEXT NOT NULL CHECK (log_kind IN ('train','eval','smoke','stderr','platform','import_clone')),
  ref TEXT NOT NULL, content_hash TEXT NOT NULL, bytes INTEGER CHECK (bytes IS NULL OR bytes >= 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK ((run_id IS NULL) != (evaluation_attempt_id IS NULL))           -- 恰一 owner
);
CREATE UNIQUE INDEX ux_execlog_run     ON execution_log(run_id, log_kind, content_hash) WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX ux_execlog_attempt ON execution_log(evaluation_attempt_id, log_kind, content_hash) WHERE evaluation_attempt_id IS NOT NULL;
CREATE TRIGGER trg_execlog_owner BEFORE INSERT ON execution_log
BEGIN
  SELECT CASE WHEN NEW.run_id IS NOT NULL AND (
      NEW.log_kind NOT IN ('train','smoke','stderr','platform','import_clone')
      OR (SELECT cycle_id FROM run WHERE id=NEW.run_id) <> NEW.cycle_id
      OR (NEW.log_kind='train'        AND (SELECT kind FROM run WHERE id=NEW.run_id) NOT IN ('build','exec'))
      OR (NEW.log_kind='import_clone' AND (SELECT kind FROM run WHERE id=NEW.run_id) <> 'import'))
    THEN RAISE(ABORT,'execution_log: run owner 的 log_kind/cycle/kind 不一致') END;
  SELECT CASE WHEN NEW.evaluation_attempt_id IS NOT NULL AND (
      NEW.log_kind NOT IN ('eval','stderr','platform')
      OR (SELECT cycle_id FROM evaluation_attempt WHERE id=NEW.evaluation_attempt_id) <> NEW.cycle_id)
    THEN RAISE(ABORT,'execution_log: attempt owner 的 log_kind/cycle 不一致') END;
END;
CREATE TRIGGER trg_execlog_noupd BEFORE UPDATE ON execution_log BEGIN SELECT RAISE(ABORT,'execution_log append-only'); END;
CREATE TRIGGER trg_execlog_nodel BEFORE DELETE ON execution_log BEGIN SELECT RAISE(ABORT,'execution_log 不可删'); END;

-- ① 执行观测（append-only；FK 溯源到 log；机器事实仅 parser 写，codex 摘要只存 ref+hash，绝不冒充观测事实）
CREATE TABLE execution_observation (
  id INTEGER PRIMARY KEY,
  execution_log_id INTEGER NOT NULL REFERENCES execution_log(id),
  source TEXT NOT NULL CHECK (source IN ('parser','codex')),
  nan_seen INTEGER CHECK (nan_seen IS NULL OR nan_seen IN (0,1)),
  divergence_flag INTEGER CHECK (divergence_flag IS NULL OR divergence_flag IN (0,1)),
  oom_count INTEGER CHECK (oom_count IS NULL OR oom_count >= 0),
  warning_count INTEGER CHECK (warning_count IS NULL OR warning_count >= 0),
  retry_count INTEGER CHECK (retry_count IS NULL OR retry_count >= 0),
  last_loss REAL, loss_trend TEXT CHECK (loss_trend IS NULL OR loss_trend IN ('down','flat','up','nan','unknown')),
  wall_clock_sec REAL CHECK (wall_clock_sec IS NULL OR wall_clock_sec >= 0),
  parser_json TEXT CHECK (parser_json IS NULL OR json_valid(parser_json)),
  parser_version TEXT, extraction_policy_hash TEXT,   -- 观测可回放：同原始产物/日志 + 同 parser_version/policy → 同 observation（护 P6）
  digest_ref TEXT, digest_hash TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- codex 摘要不得写任何机器事实/解析列，只准 digest_ref+digest_hash（叙述在被引制品里、不入库当事实，护 P4）
  CHECK (source <> 'codex' OR (nan_seen IS NULL AND divergence_flag IS NULL AND oom_count IS NULL
     AND warning_count IS NULL AND retry_count IS NULL AND last_loss IS NULL AND loss_trend IS NULL
     AND wall_clock_sec IS NULL AND parser_json IS NULL AND parser_version IS NULL AND extraction_policy_hash IS NULL AND digest_ref IS NOT NULL AND digest_hash IS NOT NULL)),
  CHECK (source <> 'parser' OR (digest_ref IS NULL AND digest_hash IS NULL))
);
CREATE TRIGGER trg_execobs_noupd BEFORE UPDATE ON execution_observation BEGIN SELECT RAISE(ABORT,'execution_observation append-only'); END;
CREATE TRIGGER trg_execobs_nodel BEFORE DELETE ON execution_observation BEGIN SELECT RAISE(ABORT,'execution_observation 不可删'); END;

-- 对照 / 审计 + reasoning 可达视图：每条 metric_result 一跳取其评估 / 训练产物 ref（train/eval 产物：原始日志 + 曲线/event/csv 等）。
-- 纪律：属人 / 审计 / 对照 + reasoning 可达族，绝不登记进 gate_input_*；底表含 execution_log → Gate 连接 authorizer
--       既有 deny SELECT execution_log 自动拒之（§6.13(2)）。产物永不作正向 evidence / gate 判据（I3 不变）。
-- group_concat 经内层 ORDER BY rh 稳定展示；本视图不入 hash / 不作 I6 确定性产物。
CREATE VIEW v_metric_result_trajectory AS
SELECT
  mr.id AS metric_result_id, mr.evaluation_attempt_id, mr.scope,
  -- 评估产物：attempt-owned，fold/aggregate 行都直达（同 attempt 共享其评估产物）
  (SELECT group_concat(rh) FROM (
     SELECT el.ref || '@' || el.content_hash AS rh
       FROM execution_log el
      WHERE el.evaluation_attempt_id = mr.evaluation_attempt_id AND el.log_kind = 'eval'
      ORDER BY rh))                                                   AS eval_artifact_refs,
  -- 训练产物：fold 行=自身 checkpoint 的训练 run；aggregate 行=其 attempt 全部 fold checkpoint 的训练 run（去重）。
  -- 无 build/exec 训练 run（produced_by_run NULL）→ JOIN 落空 → NULL（诚实表达）。
  (SELECT group_concat(rh) FROM (
     SELECT DISTINCT el.ref || '@' || el.content_hash AS rh
       FROM metric_result f
       JOIN checkpoint ck    ON ck.id = f.checkpoint_id
       JOIN execution_log el ON el.run_id = ck.produced_by_run AND el.log_kind = 'train'
      WHERE f.scope = 'fold'
        AND ( (mr.scope = 'fold'      AND f.id = mr.id)
           OR (mr.scope = 'aggregate' AND f.evaluation_attempt_id = mr.evaluation_attempt_id) )
      ORDER BY rh))                                                   AS train_artifact_refs
FROM metric_result mr;

-- ② 外部候选（完全不可变发现快照；护 I6：选择只读此快照 + 固定 policy，禁读 wall-clock/latest/id）
CREATE TABLE external_candidate (
  id INTEGER PRIMARY KEY,
  question_id INTEGER NOT NULL REFERENCES question(id),
  discovered_cycle INTEGER NOT NULL REFERENCES cycle(id),
  trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('new_structure','stuck','human_named','sota_reference')),
  trigger_snapshot_hash TEXT NOT NULL,        -- 触发判据快照哈希（stuck: visit/连续 inconclusive/policy 阈值；类型门: 判定）
  need_summary TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('paper','repo','model_hub','other')),
  canonical_uri TEXT NOT NULL, paper_uri TEXT, revision TEXT,     -- repo/model_hub 须 pinned 不可变 commit/tag
  license_id_seen TEXT,                        -- 发现时所见 license（只读快照值；权威裁定走 license_review）
  search_provider TEXT, search_query TEXT,
  search_snapshot_json TEXT NOT NULL CHECK (json_valid(search_snapshot_json)),
  search_snapshot_hash TEXT NOT NULL, rank INTEGER NOT NULL CHECK (rank >= 0),
  retrieved_at TEXT NOT NULL,                  -- 审计用，禁作选择输入
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (source_kind NOT IN ('repo','model_hub') OR revision IS NOT NULL)
);
CREATE UNIQUE INDEX ux_extcand_rev   ON external_candidate(question_id, trigger_snapshot_hash, canonical_uri, revision) WHERE revision IS NOT NULL;
CREATE UNIQUE INDEX ux_extcand_norev ON external_candidate(question_id, trigger_snapshot_hash, canonical_uri)           WHERE revision IS NULL;
CREATE TRIGGER trg_extcand_noupd BEFORE UPDATE ON external_candidate BEGIN SELECT RAISE(ABORT,'external_candidate append-only(发现快照)'); END;
CREATE TRIGGER trg_extcand_nodel BEFORE DELETE ON external_candidate BEGIN SELECT RAISE(ABORT,'external_candidate 不可删(护 I6)'); END;

-- ② license 裁定（append-only 事件；解「候选不可变 vs review→allow」张力——裁定是事件不是字段）
CREATE TABLE license_review (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES external_candidate(id),
  decision TEXT NOT NULL CHECK (decision IN ('allow','deny','review')),
  license_id TEXT, evidence_ref TEXT, actor TEXT NOT NULL CHECK (actor IN ('auto','human')),
  license_scope_json TEXT CHECK (license_scope_json IS NULL OR json_valid(license_scope_json)),   -- {allow_eval,allow_modify,allow_publish_pool,allow_redistribute}；可加变体须 allow_modify
  decided_cycle INTEGER REFERENCES cycle(id), policy_hash TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (decision <> 'allow' OR license_scope_json IS NOT NULL)
);
CREATE TRIGGER trg_licrev_noupd BEFORE UPDATE ON license_review BEGIN SELECT RAISE(ABORT,'license_review append-only'); END;
CREATE TRIGGER trg_licrev_nodel BEFORE DELETE ON license_review BEGIN SELECT RAISE(ABORT,'license_review 不可删'); END;

-- ② 选择/导入事件（append-only；记确定性选择锚 candidate_set_hash + selection_key + policy_hash）
CREATE TABLE external_import (
  id INTEGER PRIMARY KEY,
  question_id INTEGER NOT NULL REFERENCES question(id),
  candidate_id INTEGER NOT NULL REFERENCES external_candidate(id),
  action TEXT NOT NULL CHECK (action IN ('selected','rejected_by_license','selected_for_materialization','imported','materialize_failed','superseded')),
  action_cycle INTEGER NOT NULL REFERENCES cycle(id),                        -- = import_cycle（选择/动作发生轮；候选集按此冻结）
  candidate_set_hash TEXT NOT NULL, selection_key TEXT NOT NULL, policy_hash TEXT NOT NULL,   -- I6 选择锚
  license_decision_snapshot_hash TEXT,                                        -- 物化选择所据的冻结 license 裁定集哈希
  license_review_id INTEGER REFERENCES license_review(id),
  baseline_id INTEGER REFERENCES baseline(id), manifest_hash TEXT,
  reason_json TEXT CHECK (reason_json IS NULL OR json_valid(reason_json)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (action <> 'imported' OR (baseline_id IS NOT NULL AND manifest_hash IS NOT NULL AND license_review_id IS NOT NULL AND license_decision_snapshot_hash IS NOT NULL)),
  CHECK (action <> 'selected_for_materialization' OR (baseline_id IS NOT NULL AND manifest_hash IS NULL)),   -- 占位绑定：选择时建 baseline(planned) 并回指（§3.6.3）；manifest 仍限 imported
  CHECK (action IN ('selected_for_materialization','imported') OR (baseline_id IS NULL AND manifest_hash IS NULL)),
  CHECK (action NOT IN ('selected_for_materialization','imported') OR license_decision_snapshot_hash IS NOT NULL)
);
CREATE TRIGGER trg_extimport_license BEFORE INSERT ON external_import WHEN NEW.action='imported'
BEGIN
  SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM license_review lr
      WHERE lr.id=NEW.license_review_id AND lr.candidate_id=NEW.candidate_id AND lr.decision='allow')
    THEN RAISE(ABORT,'imported 须有同候选 decision=allow 的 license_review') END;
  SELECT CASE WHEN NEW.action_cycle < (SELECT discovered_cycle FROM external_candidate WHERE id=NEW.candidate_id)
    THEN RAISE(ABORT,'action_cycle 不得早于候选 discovered_cycle') END;
END;
CREATE TRIGGER trg_extimport_noupd BEFORE UPDATE ON external_import BEGIN SELECT RAISE(ABORT,'external_import append-only'); END;
CREATE TRIGGER trg_extimport_nodel BEFORE DELETE ON external_import BEGIN SELECT RAISE(ABORT,'external_import 不可删'); END;

-- ④ 入站人消息（raw 不可变）
CREATE TABLE interaction_message (
  id INTEGER PRIMARY KEY,
  connector TEXT NOT NULL, conversation_id TEXT, session_ref TEXT,
  goal_id INTEGER, goal_ver INTEGER, cycle_id INTEGER REFERENCES cycle(id),
  raw_text TEXT NOT NULL, raw_hash TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (connector, idempotency_key),
  CHECK ((goal_id IS NULL) = (goal_ver IS NULL)),
  FOREIGN KEY (goal_id, goal_ver) REFERENCES goal(id, version)
);
CREATE TRIGGER trg_imsg_noupd BEFORE UPDATE ON interaction_message BEGIN SELECT RAISE(ABORT,'interaction_message append-only(原始入站不可变)'); END;
CREATE TRIGGER trg_imsg_nodel BEFORE DELETE ON interaction_message BEGIN SELECT RAISE(ABORT,'interaction_message 不可删'); END;

-- ④ 分类（每消息恰一 UNIQUE(message_id)，确定生效，无 latest；directive ⇔ 独立 directive 行且回指本消息）
CREATE TABLE interaction_classification (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL UNIQUE REFERENCES interaction_message(id),
  intent TEXT NOT NULL CHECK (intent IN ('query','directive','note','unclear')),
  directive_id INTEGER REFERENCES directive(id),
  classifier_runner_call_id INTEGER REFERENCES runner_call(id),
  classified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK ((intent='directive') = (directive_id IS NOT NULL))
);
CREATE TRIGGER trg_iclass_directive_prov BEFORE INSERT ON interaction_classification WHEN NEW.intent='directive'
BEGIN SELECT CASE WHEN (SELECT source_interaction_message_id FROM directive WHERE id=NEW.directive_id) IS NOT NEW.message_id
  THEN RAISE(ABORT,'directive 须回指本 message(source_interaction_message_id)') END; END;
CREATE TRIGGER trg_iclass_noupd BEFORE UPDATE ON interaction_classification BEGIN SELECT RAISE(ABORT,'interaction_classification append-only'); END;
CREATE TRIGGER trg_iclass_nodel BEFORE DELETE ON interaction_classification BEGIN SELECT RAISE(ABORT,'interaction_classification 不可删'); END;

-- ④ 出站回复（append-only；codex 回复须绑 phase=interaction_query 的 runner_call）
CREATE TABLE interaction_reply (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES interaction_message(id),
  reply_ref TEXT NOT NULL, reply_hash TEXT NOT NULL, reply_text TEXT,
  snapshot_cycle INTEGER REFERENCES cycle(id),        -- 读的已提交快照（确定可回放）
  responder_kind TEXT NOT NULL CHECK (responder_kind IN ('template','codex')),
  runner_call_id INTEGER REFERENCES runner_call(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (responder_kind <> 'codex' OR runner_call_id IS NOT NULL)
);
CREATE TRIGGER trg_ireply_codex_phase BEFORE INSERT ON interaction_reply WHEN NEW.responder_kind='codex'
BEGIN SELECT CASE WHEN (SELECT phase FROM runner_call WHERE id=NEW.runner_call_id) <> 'interaction_query'
  THEN RAISE(ABORT,'codex 回复须绑 phase=interaction_query 的 runner_call') END; END;
CREATE TRIGGER trg_ireply_noupd BEFORE UPDATE ON interaction_reply BEGIN SELECT RAISE(ABORT,'interaction_reply append-only'); END;
CREATE TRIGGER trg_ireply_nodel BEFORE DELETE ON interaction_reply BEGIN SELECT RAISE(ABORT,'interaction_reply 不可删'); END;

-- ===================== v2.4 增量：文件请求（§4.6.8）=====================
-- 纪律同人机表：不在任何 gate 判据输入集（authorizer deny 显式列名，《第二部分》§6.13(2)）；请求/答复不写 decision
CREATE TABLE interaction_request (
  id INTEGER PRIMARY KEY,
  goal_id INTEGER NOT NULL, goal_ver INTEGER NOT NULL,
  cycle_id INTEGER REFERENCES cycle(id), question_id INTEGER REFERENCES question(id),
  stage TEXT NOT NULL CHECK (stage IN ('idea','plan','bundle','reasoning')),
  status TEXT NOT NULL CHECK (status IN ('pending','resolved','cancelled')),
  summary_md TEXT NOT NULL,                 -- 易懂语言：期望做什么 / 为何自己获取不到 / 需要用户提供什么
  items_json TEXT NOT NULL,                 -- 冻结条目数组：{kind∈{dataset,paper,wet_lab,other}, desc, expected_files, attempted_paths, failure_reason, dest_hint}
  request_hash TEXT NOT NULL CHECK (length(request_hash) > 0),   -- items 内容哈希；创建幂等锚 (goal_id, request_hash)
  resolution_json TEXT,                     -- 终态一次写入：resolved=逐条 {provided: path+hash}|{unavailable: reason}；cancelled={cancelled, reason}
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT,
  resolved_message_id INTEGER REFERENCES interaction_message(id),   -- 用户答复/取消的入站 provenance（终态必填）
  FOREIGN KEY (goal_id, goal_ver) REFERENCES goal(id, version),
  CHECK ((status = 'pending') = (resolution_json IS NULL)),
  CHECK ((status = 'pending') = (resolved_at IS NULL)),
  CHECK ((status = 'pending') = (resolved_message_id IS NULL))
);
-- 「聚合一次性说清」的机械落点：同一 goal 至多一张 pending 单
CREATE UNIQUE INDEX uq_ireq_one_pending ON interaction_request(goal_id) WHERE status='pending';
-- 身份冻结：pending 期只允许一次性迁入终态（status/resolution/resolved_* 同时写），其余列不可改
CREATE TRIGGER trg_ireq_identity_frozen BEFORE UPDATE ON interaction_request
WHEN NEW.goal_id<>OLD.goal_id OR NEW.goal_ver<>OLD.goal_ver OR NEW.cycle_id IS NOT OLD.cycle_id
  OR NEW.question_id IS NOT OLD.question_id OR NEW.stage<>OLD.stage OR NEW.summary_md<>OLD.summary_md
  OR NEW.items_json<>OLD.items_json OR NEW.request_hash<>OLD.request_hash OR NEW.created_at<>OLD.created_at
BEGIN SELECT RAISE(ABORT,'interaction_request 身份列冻结(仅 status/resolution/resolved_* 可一次性迁移)'); END;
CREATE TRIGGER trg_ireq_terminal_frozen BEFORE UPDATE ON interaction_request WHEN OLD.status <> 'pending'
BEGIN SELECT RAISE(ABORT,'interaction_request 终态冻结(resolved/cancelled 不可再改)'); END;
CREATE TRIGGER trg_ireq_nodel BEFORE DELETE ON interaction_request
BEGIN SELECT RAISE(ABORT,'interaction_request 不可删'); END;
