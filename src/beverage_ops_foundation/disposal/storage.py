"""老产品处置域的 SQLite 表结构。

所有表在基础库的同一数据库内创建，CREATE IF NOT EXISTS 保证重复初始化安全。
"""

from __future__ import annotations

DISPOSAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS disposal_rule_versions (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
);
CREATE TABLE IF NOT EXISTS disposal_batches (
    batch_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    unit_cost REAL,
    produced_at TEXT NOT NULL,
    inbound_at TEXT NOT NULL,
    package_status TEXT NOT NULL,
    location_type TEXT NOT NULL,
    location_org_id TEXT NOT NULL,
    location_name TEXT NOT NULL,
    distributor_id TEXT,
    responsible_party TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    parent_batch_id TEXT REFERENCES disposal_batches(batch_id),
    is_legacy INTEGER NOT NULL CHECK(is_legacy IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (rule_id, rule_version) REFERENCES disposal_rule_versions(rule_id, version)
);
CREATE TABLE IF NOT EXISTS disposal_freezes (
    freeze_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    reason TEXT NOT NULL,
    scope_note TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','released')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT,
    release_reason TEXT
);
CREATE TABLE IF NOT EXISTS disposal_plans (
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK(plan_version >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','approved','superseded','completed')),
    title TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    revision_reason TEXT,
    superseded_at TEXT,
    PRIMARY KEY (plan_id, plan_version)
);
CREATE TABLE IF NOT EXISTS disposal_plan_lines (
    line_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    origin_line_id TEXT,
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    decision TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_price REAL,
    due_at TEXT,
    target_org_id TEXT,
    target_location_name TEXT,
    note TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    finalized_qty INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (plan_id, plan_version) REFERENCES disposal_plans(plan_id, plan_version)
);
CREATE TABLE IF NOT EXISTS disposal_steps (
    step_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    line_id TEXT NOT NULL REFERENCES disposal_plan_lines(line_id),
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    sequence INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','in_transit','completed','superseded')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    source_org_id TEXT NOT NULL,
    target_org_id TEXT NOT NULL,
    target_location_name TEXT NOT NULL DEFAULT '',
    responsible_party TEXT NOT NULL,
    unit_price REAL,
    due_at TEXT,
    output_batch_id TEXT REFERENCES disposal_batches(batch_id),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    completed_by TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id, plan_version) REFERENCES disposal_plans(plan_id, plan_version)
);
CREATE TABLE IF NOT EXISTS disposal_handoff_confirmations (
    step_id TEXT NOT NULL REFERENCES disposal_steps(step_id),
    party_role TEXT NOT NULL CHECK(party_role IN ('dispatch','receipt')),
    actor_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    scan_code TEXT,
    note TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (step_id, party_role)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_disposal_scan_code
    ON disposal_handoff_confirmations(scan_code) WHERE scan_code IS NOT NULL;
CREATE TABLE IF NOT EXISTS disposal_custody_ledger (
    ledger_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    batch_id TEXT NOT NULL REFERENCES disposal_batches(batch_id),
    quantity_delta INTEGER NOT NULL,
    step_id TEXT,
    plan_id TEXT,
    plan_version INTEGER,
    counterparty_batch_id TEXT,
    org_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disposal_ledger_batch ON disposal_custody_ledger(batch_id, created_at, ledger_id);
CREATE INDEX IF NOT EXISTS idx_disposal_batches_product ON disposal_batches(product_id);
CREATE INDEX IF NOT EXISTS idx_disposal_batches_distributor ON disposal_batches(distributor_id);
CREATE INDEX IF NOT EXISTS idx_disposal_steps_status ON disposal_steps(status);
CREATE TABLE IF NOT EXISTS disposal_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def ensure_disposal_schema(database) -> None:
    """在既有数据库上幂等创建处置域表结构。"""

    database.connection.executescript(DISPOSAL_SCHEMA)

