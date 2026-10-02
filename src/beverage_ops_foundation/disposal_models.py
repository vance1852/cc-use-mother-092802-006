"""老产品处置平台使用的只读数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuleSet:
    """不可变的处置规则版本。"""

    ruleset_id: str
    version: str
    decision_matrix: dict[str, str]
    loss_basis: dict[str, Any]
    registered_by: str
    created_at: str


@dataclass(frozen=True)
class Batch:
    """一个需要清理的老产品批次及其处置档案。"""

    batch_id: str
    product_id: str
    product_name: str
    quantity: int
    unit: str
    age_days: int
    package_state: str
    location_type: str          # channel | self_warehouse
    location_ref: str
    dealer_id: str              # 渠道位置下的责任经销商；自有库存为空
    owner_org_id: str
    custodian_actor_id: str     # 当前保管责任方
    ruleset_id: str
    ruleset_version: str
    frozen: bool
    frozen_scope: str
    frozen_reason: str
    created_at: str
    version: int


@dataclass(frozen=True)
class PlanLine:
    """计划行：某个批次在某个渠道位置上的建议处置及数量。"""

    batch_id: str
    quantity: int
    suggested_action: str
    location_type: str
    location_ref: str
    dealer_id: str


@dataclass(frozen=True)
class DisposalPlan:
    """处置计划：审批后才能生成可执行步骤。"""

    plan_id: str
    status: str                 # draft | submitted | approved | rejected | superseded
    ruleset_id: str
    ruleset_version: str
    lines: tuple[PlanLine, ...]
    responsible_actor_id: str
    created_by: str
    created_at: str
    submitted_at: str
    approved_by: str
    approved_at: str
    reject_reason: str
    revision_of: str
    revision: int


@dataclass(frozen=True)
class DisposalStep:
    """审批后生成的可执行处置步骤。"""

    step_id: str
    plan_id: str
    batch_id: str
    sequence: int
    action: str
    required_qty: int
    done_qty: int
    status: str                 # pending | in_progress | awaiting_counterparty | done | blocked
    from_location_type: str
    from_location_ref: str
    to_location_type: str
    to_location_ref: str
    responsible_actor_id: str
    counterparty_actor_id: str
    due_at: str
    completed_at: str
    irreversibly_closed: bool
    closure_kind: str           # destroyed | discount_settled | ""
    settled_at: str
    created_at: str


@dataclass(frozen=True)
class CustodyEvent:
    """跨主体保管交接记录，双方确认且数量守恒。"""

    event_id: str
    step_id: str
    batch_id: str
    direction: str              # handover_out | handover_in | destroy_witness
    quantity: int
    from_actor_id: str
    to_actor_id: str
    confirmed_by_from: bool
    confirmed_by_to: bool
    status: str                 # pending | confirmed | rejected
    evidence_ref: str
    occurred_at: str
    recorded_at: str
    late: bool


@dataclass(frozen=True)
class ScanRecord:
    """现场扫描/作业回执的登记结果（含重复扫描判定）。"""

    scan_id: str
    step_id: str
    batch_id: str
    scan_type: str              # pickup | repack | discount | return | destroy
    quantity: int
    scanner_actor_id: str
    evidence_ref: str
    duplicate_of: str
    occurred_at: str
    received_at: str
    late: bool


@dataclass(frozen=True)
class FreezeRecord:
    """质量或标签风险的范围冻结记录。"""

    freeze_id: str
    batch_id: str
    scope: str                  # batch | package_group | quantity
    affected_quantity: int
    reason: str
    quality: bool
    raised_by: str
    raised_at: str
    lifted_at: str


@dataclass(frozen=True)
class PendingAction:
    """管理视图：未完成动作行。"""

    step_id: str
    plan_id: str
    batch_id: str
    product_id: str
    dealer_id: str
    action: str
    status: str
    required_qty: int
    remaining_qty: int
    responsible_actor_id: str
    due_at: str
    overdue: bool
    frozen: bool


@dataclass(frozen=True)
class LossBasisLine:
    """管理视图：损益依据行。"""

    batch_id: str
    product_id: str
    action: str
    outcome_type: str
    quantity: int
    unit_basis: dict[str, Any]
    ruleset_id: str
    ruleset_version: str
    plan_id: str
    step_id: str
    settled_at: str
