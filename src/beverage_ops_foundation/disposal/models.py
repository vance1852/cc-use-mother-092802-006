"""老产品处置域在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuleVersion:
    """不可变的处置规则版本。"""

    rule_id: str
    version: int
    payload: dict[str, Any]
    payload_hash: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Batch:
    """一批需要清理的老产品库存。"""

    batch_id: str
    product_id: str
    product_name: str
    quantity: int
    unit: str
    unit_cost: float | None
    produced_at: str
    inbound_at: str
    package_status: str
    location_type: str
    location_org_id: str
    location_name: str
    distributor_id: str | None
    responsible_party: str
    rule_id: str
    rule_version: int
    parent_batch_id: str | None
    is_legacy: bool
    created_by: str
    created_at: str
    # 查询时派生的台账口径
    age_days: int = 0
    frozen_qty: int = 0
    in_transit_qty: int = 0
    available_qty: int = 0


@dataclass(frozen=True)
class Freeze:
    """针对质量或标签风险的范围冻结记录。"""

    freeze_id: str
    batch_id: str
    quantity: int
    reason: str
    scope_note: str
    status: str
    created_by: str
    created_at: str
    released_by: str | None
    released_at: str | None
    release_reason: str | None


@dataclass(frozen=True)
class PlanLine:
    """处置计划中的单个批次行。"""

    line_id: str
    plan_id: str
    plan_version: int
    origin_line_id: str | None
    batch_id: str
    decision: str
    quantity: int
    unit_price: float | None
    due_at: str | None
    note: str
    rule_id: str
    rule_version: int
    snapshot: dict[str, Any]
    finalized_qty: int


@dataclass(frozen=True)
class Step:
    """计划审批后生成的可执行步骤。"""

    step_id: str
    plan_id: str
    plan_version: int
    line_id: str
    batch_id: str
    sequence: int
    step_type: str
    status: str
    quantity: int
    source_org_id: str
    target_org_id: str
    responsible_party: str
    due_at: str | None
    output_batch_id: str | None
    evidence: dict[str, Any]
    completed_by: str | None
    completed_at: str | None
    created_at: str


@dataclass(frozen=True)
class DisposalReceipt:
    """处置域写操作的幂等结果，extra 保存晚到/重复等附加判定。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {"request_id": self.request_id, "resource_type": self.resource_type,
                "resource_id": self.resource_id, "replayed": self.replayed}
        data.update(self.extra)
        return data
