-- 0002_bundle_target_dag.sql
-- Durable Bundle target graph, exact admission, published-source bindings,
-- resident task identity, trusted resource requests/leases, and terminal reports.
-- Additive only: 0001_appendix_a.sql remains byte-for-byte frozen.

PRAGMA foreign_keys = ON;

-- Composite child keys below make cross-cycle references impossible even when
-- a caller supplies a valid target id with the wrong cycle id.
CREATE UNIQUE INDEX ux_build_target_id_cycle
  ON build_target(id, cycle_id);

CREATE TABLE bundle_target_node (
  target_id INTEGER PRIMARY KEY,
  cycle_id INTEGER NOT NULL,
  target_key TEXT NOT NULL
    CHECK (
      length(target_key) BETWEEN 1 AND 128
      AND target_key NOT IN ('.', '..')
      AND target_key NOT GLOB '*[^A-Za-z0-9._-]*'
    ),
  parent_target_id INTEGER,
  parent_baseline_ref TEXT,
  registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (cycle_id, target_key),
  UNIQUE (target_id, cycle_id),
  FOREIGN KEY (target_id, cycle_id)
    REFERENCES build_target(id, cycle_id),
  FOREIGN KEY (parent_target_id, cycle_id)
    REFERENCES build_target(id, cycle_id),
  CHECK (parent_target_id IS NULL OR parent_target_id <> target_id),
  CHECK (
    (parent_target_id IS NULL) !=
    (parent_baseline_ref IS NULL)
    OR (parent_target_id IS NULL AND parent_baseline_ref IS NULL)
  ),
  CHECK (
    parent_baseline_ref IS NULL
    OR length(parent_baseline_ref) BETWEEN 1 AND 4096
  )
);

CREATE TABLE bundle_target_dependency (
  id INTEGER PRIMARY KEY,
  cycle_id INTEGER NOT NULL,
  upstream_target_id INTEGER NOT NULL,
  downstream_target_id INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (cycle_id, upstream_target_id, downstream_target_id),
  FOREIGN KEY (upstream_target_id, cycle_id)
    REFERENCES bundle_target_node(target_id, cycle_id),
  FOREIGN KEY (downstream_target_id, cycle_id)
    REFERENCES bundle_target_node(target_id, cycle_id),
  CHECK (upstream_target_id <> downstream_target_id)
);
CREATE INDEX ix_bundle_dependency_downstream
  ON bundle_target_dependency(cycle_id, downstream_target_id, upstream_target_id);

CREATE TABLE bundle_target_admission (
  id INTEGER PRIMARY KEY,
  target_id INTEGER NOT NULL UNIQUE,
  cycle_id INTEGER NOT NULL,
  phase_commit_id INTEGER NOT NULL UNIQUE REFERENCES phase_commit(id),
  publication_decision_id INTEGER NOT NULL UNIQUE REFERENCES decision(id),
  manifest_ref TEXT NOT NULL CHECK (length(manifest_ref) BETWEEN 1 AND 4096),
  manifest_hash TEXT NOT NULL
    CHECK (
      length(manifest_hash) = 64
      AND manifest_hash NOT GLOB '*[^0-9a-f]*'
    ),
  baseline_id INTEGER NOT NULL REFERENCES baseline(id),
  variant_id INTEGER NOT NULL REFERENCES variant(id),
  evaluation_id INTEGER NOT NULL REFERENCES evaluation(id),
  attempt_id INTEGER NOT NULL REFERENCES evaluation_attempt(id),
  source_ref TEXT,
  source_hash TEXT,
  source_hash_alg TEXT,
  admitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (id, target_id, cycle_id),
  FOREIGN KEY (target_id, cycle_id)
    REFERENCES build_target(id, cycle_id),
  CHECK (
    (source_ref IS NULL AND source_hash IS NULL AND source_hash_alg IS NULL)
    OR (
      length(source_ref) BETWEEN 1 AND 4096
      AND length(source_hash) = 64
      AND source_hash NOT GLOB '*[^0-9a-f]*'
      AND length(source_hash_alg) BETWEEN 1 AND 64
    )
  )
);

CREATE TABLE bundle_source_request (
  id INTEGER PRIMARY KEY,
  cycle_id INTEGER NOT NULL,
  downstream_target_id INTEGER NOT NULL,
  input_key TEXT NOT NULL
    CHECK (
      length(input_key) BETWEEN 1 AND 128
      AND input_key NOT IN ('.', '..')
      AND input_key NOT GLOB '*[^A-Za-z0-9._-]*'
    ),
  upstream_target_id INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (downstream_target_id, input_key),
  UNIQUE (
    id, cycle_id, downstream_target_id, upstream_target_id, input_key
  ),
  FOREIGN KEY (upstream_target_id, cycle_id)
    REFERENCES bundle_target_node(target_id, cycle_id),
  FOREIGN KEY (downstream_target_id, cycle_id)
    REFERENCES bundle_target_node(target_id, cycle_id),
  CHECK (upstream_target_id <> downstream_target_id)
);

CREATE TABLE bundle_source_binding (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL UNIQUE,
  cycle_id INTEGER NOT NULL,
  downstream_target_id INTEGER NOT NULL,
  input_key TEXT NOT NULL,
  upstream_target_id INTEGER NOT NULL,
  upstream_admission_id INTEGER NOT NULL,
  publication_decision_id INTEGER NOT NULL REFERENCES decision(id),
  manifest_ref TEXT NOT NULL CHECK (length(manifest_ref) BETWEEN 1 AND 4096),
  manifest_hash TEXT NOT NULL
    CHECK (
      length(manifest_hash) = 64
      AND manifest_hash NOT GLOB '*[^0-9a-f]*'
    ),
  source_ref TEXT NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 4096),
  source_hash TEXT NOT NULL
    CHECK (
      length(source_hash) = 64
      AND source_hash NOT GLOB '*[^0-9a-f]*'
    ),
  source_hash_alg TEXT NOT NULL CHECK (length(source_hash_alg) BETWEEN 1 AND 64),
  bound_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (downstream_target_id, input_key),
  FOREIGN KEY (
    request_id, cycle_id, downstream_target_id, upstream_target_id, input_key
  ) REFERENCES bundle_source_request(
    id, cycle_id, downstream_target_id, upstream_target_id, input_key
  ),
  FOREIGN KEY (upstream_admission_id, upstream_target_id, cycle_id)
    REFERENCES bundle_target_admission(id, target_id, cycle_id)
);

CREATE TABLE bundle_worker_task (
  id INTEGER PRIMARY KEY,
  build_target_id INTEGER NOT NULL,
  cycle_id INTEGER NOT NULL,
  role TEXT NOT NULL
    CHECK (role IN ('worker', 'code_review', 'result_review')),
  provider_task_id TEXT,
  status TEXT NOT NULL
    CHECK (
      status IN (
        'created', 'running', 'waiting', 'completed', 'failed', 'cancelled'
      )
    ),
  receipt_ref TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (build_target_id, role),
  FOREIGN KEY (build_target_id, cycle_id)
    REFERENCES build_target(id, cycle_id),
  CHECK (
    provider_task_id IS NULL
    OR length(provider_task_id) BETWEEN 1 AND 4096
  ),
  CHECK (receipt_ref IS NULL OR length(receipt_ref) BETWEEN 1 AND 4096)
);
CREATE UNIQUE INDEX ux_bundle_worker_provider_task
  ON bundle_worker_task(provider_task_id)
  WHERE provider_task_id IS NOT NULL;
CREATE INDEX ix_bundle_worker_cycle_status
  ON bundle_worker_task(cycle_id, status, build_target_id);

CREATE TABLE bundle_scheduler_state (
  cycle_id INTEGER PRIMARY KEY REFERENCES cycle(id),
  revision INTEGER NOT NULL DEFAULT 0
    CHECK (revision BETWEEN 0 AND 9223372036854775806),
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE bundle_resource_request (
  build_target_id INTEGER PRIMARY KEY,
  cycle_id INTEGER NOT NULL,
  gpu_count INTEGER NOT NULL CHECK (gpu_count BETWEEN 0 AND 64),
  worker_slots INTEGER NOT NULL DEFAULT 1 CHECK (worker_slots >= 1),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (build_target_id, cycle_id),
  FOREIGN KEY (build_target_id, cycle_id)
    REFERENCES build_target(id, cycle_id)
);

CREATE TABLE bundle_resource_lease (
  id INTEGER PRIMARY KEY,
  build_target_id INTEGER NOT NULL,
  cycle_id INTEGER NOT NULL,
  resource_kind TEXT NOT NULL CHECK (resource_kind = 'gpu'),
  resource_key TEXT NOT NULL CHECK (length(resource_key) BETWEEN 1 AND 4096),
  contract_hash TEXT NOT NULL CHECK (length(contract_hash) BETWEEN 1 AND 4096),
  status TEXT NOT NULL CHECK (status IN ('active', 'releasing', 'released')),
  acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  released_at TEXT,
  guardian_receipt_ref TEXT,
  UNIQUE (build_target_id, resource_kind, resource_key),
  FOREIGN KEY (build_target_id, cycle_id)
    REFERENCES bundle_resource_request(build_target_id, cycle_id),
  CHECK (
    (status IN ('active', 'releasing') AND released_at IS NULL)
    OR (
      status = 'released'
      AND released_at IS NOT NULL
      AND guardian_receipt_ref IS NOT NULL
      AND length(guardian_receipt_ref) BETWEEN 1 AND 4096
    )
  )
);
CREATE UNIQUE INDEX ux_bundle_resource_live
  ON bundle_resource_lease(resource_key)
  WHERE status IN ('active', 'releasing');
CREATE INDEX ix_bundle_resource_target_status
  ON bundle_resource_lease(build_target_id, status);

CREATE TABLE bundle_terminal_report (
  build_target_id INTEGER PRIMARY KEY,
  cycle_id INTEGER NOT NULL,
  report_ref TEXT NOT NULL CHECK (length(report_ref) BETWEEN 1 AND 4096),
  report_hash TEXT NOT NULL
    CHECK (
      length(report_hash) = 64
      AND report_hash NOT GLOB '*[^0-9a-f]*'
    ),
  status TEXT NOT NULL
    CHECK (status IN ('complete', 'failed', 'skipped', 'replan_required')),
  summary_json TEXT NOT NULL
    CHECK (json_valid(summary_json) AND json_type(summary_json) = 'object'),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (build_target_id, cycle_id),
  FOREIGN KEY (build_target_id, cycle_id)
    REFERENCES build_target(id, cycle_id)
);

-- Exact admission is append-only and is impossible without all durable DB
-- facts. Filesystem bytes are reverified by BundleGraph immediately before
-- this insert; SQLite independently closes the identity/status half.
CREATE TRIGGER trg_bundle_admission_facts
BEFORE INSERT ON bundle_target_admission
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM build_target bt
    JOIN baseline b ON b.id = NEW.baseline_id
    JOIN variant v ON v.id = NEW.variant_id
    JOIN evaluation e ON e.id = NEW.evaluation_id
    JOIN evaluation_attempt ea ON ea.id = NEW.attempt_id
    WHERE bt.id = NEW.target_id
      AND bt.cycle_id = NEW.cycle_id
      AND bt.status = 'complete'
      AND bt.baseline_id = NEW.baseline_id
      AND bt.variant_id = NEW.variant_id
      AND bt.evaluation_id = NEW.evaluation_id
      AND b.status = 'legal'
      AND v.baseline_id = b.id
      AND v.status = 'legal'
      AND e.variant_id = v.id
      AND e.build_target_id = bt.id
      AND e.status = 'success'
      AND ea.evaluation_id = e.id
      AND ea.cycle_id = bt.cycle_id
      AND ea.build_target_id = bt.id
      AND ea.status = 'success'
  ) THEN RAISE(ABORT, 'bundle admission domain identity/status incomplete') END;

  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM phase_commit pc
    WHERE pc.id = NEW.phase_commit_id
      AND pc.cycle_id = NEW.cycle_id
      AND pc.stage = 'bundle'
      AND pc.target_id = NEW.target_id
  ) THEN RAISE(ABORT, 'bundle admission missing exact phase_commit') END;

  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM decision d
    WHERE d.id = NEW.publication_decision_id
      AND d.cycle_id = NEW.cycle_id
      AND d.actor = 'gate'
      AND d.type = 'pool_publication'
      AND json_extract(d.payload_json, '$.schema')
          = 'meta-research-pool-db-binding/v1'
      AND json_extract(d.payload_json, '$.manifest_ref') = NEW.manifest_ref
      AND json_extract(d.payload_json, '$.manifest_hash') = NEW.manifest_hash
      AND json_extract(d.payload_json, '$.baseline_id') = NEW.baseline_id
      AND json_extract(d.payload_json, '$.variant_id') = NEW.variant_id
      AND json_extract(d.payload_json, '$.evaluation_id') = NEW.evaluation_id
      AND json_extract(d.payload_json, '$.attempt_id') = NEW.attempt_id
  ) THEN RAISE(ABORT, 'bundle admission missing exact formal publication') END;
END;

CREATE TRIGGER trg_bundle_source_binding_facts
BEFORE INSERT ON bundle_source_binding
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM bundle_target_admission a
    WHERE a.id = NEW.upstream_admission_id
      AND a.target_id = NEW.upstream_target_id
      AND a.cycle_id = NEW.cycle_id
      AND a.publication_decision_id = NEW.publication_decision_id
      AND a.manifest_ref = NEW.manifest_ref
      AND a.manifest_hash = NEW.manifest_hash
      AND a.source_ref = NEW.source_ref
      AND a.source_hash = NEW.source_hash
      AND a.source_hash_alg = NEW.source_hash_alg
  ) THEN RAISE(ABORT, 'bundle source binding is not the exact upstream admission') END;
END;

CREATE TRIGGER trg_bundle_node_noupd
BEFORE UPDATE ON bundle_target_node
BEGIN SELECT RAISE(ABORT, 'bundle target node is immutable'); END;
CREATE TRIGGER trg_bundle_node_nodel
BEFORE DELETE ON bundle_target_node
BEGIN SELECT RAISE(ABORT, 'bundle target node is durable'); END;

CREATE TRIGGER trg_bundle_dependency_noupd
BEFORE UPDATE ON bundle_target_dependency
BEGIN SELECT RAISE(ABORT, 'bundle target dependency is immutable'); END;
CREATE TRIGGER trg_bundle_dependency_nodel
BEFORE DELETE ON bundle_target_dependency
BEGIN SELECT RAISE(ABORT, 'bundle target dependency is durable'); END;

CREATE TRIGGER trg_bundle_admission_noupd
BEFORE UPDATE ON bundle_target_admission
BEGIN SELECT RAISE(ABORT, 'bundle target admission is immutable'); END;
CREATE TRIGGER trg_bundle_admission_nodel
BEFORE DELETE ON bundle_target_admission
BEGIN SELECT RAISE(ABORT, 'bundle target admission is durable'); END;

CREATE TRIGGER trg_bundle_source_request_noupd
BEFORE UPDATE ON bundle_source_request
BEGIN SELECT RAISE(ABORT, 'bundle source request is immutable'); END;
CREATE TRIGGER trg_bundle_source_request_nodel
BEFORE DELETE ON bundle_source_request
BEGIN SELECT RAISE(ABORT, 'bundle source request is durable'); END;

CREATE TRIGGER trg_bundle_source_binding_noupd
BEFORE UPDATE ON bundle_source_binding
BEGIN SELECT RAISE(ABORT, 'bundle source binding is immutable'); END;
CREATE TRIGGER trg_bundle_source_binding_nodel
BEFORE DELETE ON bundle_source_binding
BEGIN SELECT RAISE(ABORT, 'bundle source binding is durable'); END;

CREATE TRIGGER trg_bundle_resource_request_noupd
BEFORE UPDATE ON bundle_resource_request
BEGIN SELECT RAISE(ABORT, 'bundle resource request is immutable'); END;
CREATE TRIGGER trg_bundle_resource_request_nodel
BEFORE DELETE ON bundle_resource_request
BEGIN SELECT RAISE(ABORT, 'bundle resource request is durable'); END;

CREATE TRIGGER trg_bundle_terminal_report_noupd
BEFORE UPDATE ON bundle_terminal_report
BEGIN SELECT RAISE(ABORT, 'bundle terminal report is immutable'); END;
CREATE TRIGGER trg_bundle_terminal_report_nodel
BEFORE DELETE ON bundle_terminal_report
BEGIN SELECT RAISE(ABORT, 'bundle terminal report is durable'); END;
