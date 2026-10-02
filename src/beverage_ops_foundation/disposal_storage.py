"""老产品处置平台的表结构。

所有处置业务表独立于基础模块，仅通过 actor_id / organization_id 与基础登记数据关联，
在同一个 SQLite 数据库、同一事务体系内工作。
"""

from __future__ import annotations

DISPOSAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS disposal_rulesets (
    ruleset_id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    version TEXT NOT NULL,
    decision_matrix_json TEXT NOT NULL,
    loss_basis_json TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(family, version)
);
CREATE TABLE IF NOT EXISTS disposal_batches (
    batch_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    original_quantity INTEGER NOT NULL CHECK(original_quantity > 0),
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    unit TEXT NOT NULL,
    age_days INTEGER NOT NULL CHECK(age_days >= 0),
    package_state TEXT NOT NULL,
    location_type TEXT NOT NULL,
    location_ref TEXT NOT NULL,
    dealer_id TEXT NOT NULL DEFAULT '',
    owner_org_id TEXT NOT NULL,
    custodian_actor_id TEXT NOT NULL,
    ruleset_id TEXT NOT NULL REFERENCES disposal_rulesets(ruleset_id),
    ruleset_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_disposal_batches_product ON disposal_batches(product_id);
CREATE INDEX IF NOT EXISTS idx_disposal_batches_dealer ON disposal_batches(dealer_id);
-- 批次在各位置的实际在库量，所有移动必须保持 SUM = disposal_batches.quantity
CREATE TABLE IF NOT EXISTS disposal_batch_stocks (
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    location_type TEXT NOT NULL,
    location_ref TEXT NOT NULL,
    custodian_actor_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    PRIMARY KEY(batch_id, location_type, location_ref, custodian_actor_id)
);
CREATE TABLE IF NOT EXISTS disposal_freezes (
    freeze_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    scope TEXT NOT NULL,
    scope_ref TEXT NOT NULL DEFAULT '',
    affected_quantity INTEGER NOT NULL CHECK(affected_quantity > 0),
    reason TEXT NOT NULL,
    is_quality INTEGER NOT NULL,
    raised_by TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    lifted_by TEXT NOT NULL DEFAULT '',
    lifted_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_freezes_batch ON disposal_freezes(batch_id);
CREATE TABLE IF NOT EXISTS disposal_plans (
    plan_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    ruleset_id TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    responsible_actor_id TEXT NOT NULL,
    warehouse_ref TEXT NOT NULL,
    repack_station_ref TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    submitted_at TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    rejected_by TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    revision_of TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    due_days INTEGER NOT NULL DEFAULT 7
);
CREATE TABLE IF NOT EXISTS disposal_plan_lines (
    line_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES disposal_plans(plan_id),
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    suggested_action TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    location_type TEXT NOT NULL,
    location_ref TEXT NOT NULL,
    dealer_id TEXT NOT NULL DEFAULT '',
    custodian_snapshot TEXT NOT NULL,
    UNIQUE(plan_id, batch_id)
);
CREATE TABLE IF NOT EXISTS disposal_steps (
    step_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES disposal_plans(plan_id),
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    sequence INTEGER NOT NULL,
    action TEXT NOT NULL,
    required_qty INTEGER NOT NULL CHECK(required_qty > 0),
    done_qty INTEGER NOT NULL DEFAULT 0 CHECK(done_qty <= required_qty),
    status TEXT NOT NULL,
    from_location_type TEXT NOT NULL,
    from_location_ref TEXT NOT NULL,
    to_location_type TEXT NOT NULL,
    to_location_ref TEXT NOT NULL,
    responsible_actor_id TEXT NOT NULL,
    counterparty_actor_id TEXT NOT NULL DEFAULT '',
    due_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    irreversibly_closed INTEGER NOT NULL DEFAULT 0,
    closure_kind TEXT NOT NULL DEFAULT '',
    closure_evidence TEXT NOT NULL DEFAULT '',
    settled_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, batch_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_steps_batch ON disposal_steps(batch_id);
CREATE INDEX IF NOT EXISTS idx_steps_status ON disposal_steps(status);
CREATE TABLE IF NOT EXISTS disposal_custody_events (
    event_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL REFERENCES disposal_steps(step_id),
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    direction TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    from_actor_id TEXT NOT NULL,
    to_actor_id TEXT NOT NULL,
    confirmed_by_from INTEGER NOT NULL DEFAULT 0,
    confirmed_by_to INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    evidence_ref TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_custody_step ON disposal_custody_events(step_id);
-- 现场扫描回执只作为作业证据登记，不直接移动数量；同一步骤/类型/证据编号视为重复扫描
CREATE TABLE IF NOT EXISTS disposal_scans (
    scan_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL REFERENCES disposal_steps(step_id),
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    scan_type TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    scanner_actor_id TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    duplicate_of TEXT NOT NULL DEFAULT '',
    late INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scans_step ON disposal_scans(step_id);
-- 折价退款/补贴结算事实，一经记录不可撤销
CREATE TABLE IF NOT EXISTS disposal_settlements (
    settlement_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL UNIQUE REFERENCES disposal_steps(step_id),
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    amount REAL NOT NULL CHECK(amount >= 0),
    currency TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


def ensure_disposal_schema(connection) -> None:
    """幂等执行处置平台建表语句。"""

    connection.executescript(DISPOSAL_SCHEMA)
