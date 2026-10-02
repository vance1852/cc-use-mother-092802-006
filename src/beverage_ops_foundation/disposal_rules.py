"""老产品处置规则集的版本管理。

规则集是不可变的命名版本：同一版本号一经登记，其动作矩阵与损益口径即被冻结，
历史批次与处置计划永久引用登记时的版本，从而保证"处置规则版本"可追溯。
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ValidationError

VERSION_FORMAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

# 处置动作取值与中文标签。
ACTION_PICKUP = "pickup"      # 提货：从渠道位置收回
ACTION_REPACK = "repack"      # 换包/换标
ACTION_DISCOUNT = "discount"  # 折价就地销售
ACTION_RETURN = "return"      # 退仓
ACTION_DESTROY = "destroy"    # 销毁

ACTIONS: frozenset[str] = frozenset(
    {ACTION_PICKUP, ACTION_REPACK, ACTION_DISCOUNT, ACTION_RETURN, ACTION_DESTROY}
)

# 动作产生的财务/库存结果类型，用于"损益依据"归集。
OUTCOME_RESALE = "resale"          # 折价销售回款
OUTCOME_RETURNED_STOCK = "stock"   # 回到自有库存
OUTCOME_DESTROYED = "destroyed"    # 销毁（报损）
OUTCOME_REPACKED = "repacked"      # 换包后重新可售

ACTION_OUTCOMES: dict[str, str] = {
    ACTION_PICKUP: OUTCOME_RETURNED_STOCK,
    ACTION_REPACK: OUTCOME_REPACKED,
    ACTION_DISCOUNT: OUTCOME_RESALE,
    ACTION_RETURN: OUTCOME_RETURNED_STOCK,
    ACTION_DESTROY: OUTCOME_DESTROYED,
}

# 终结性动作：执行并确认后，货物形成不可被普通修订撤销的终态。
TERMINAL_ACTIONS: frozenset[str] = frozenset({ACTION_DESTROY, ACTION_DISCOUNT})

# 包装状态取值。
PACKAGE_INTACT = "intact"
PACKAGE_SCRATCHED = "scratched"   # 外观磨损但标识完整
PACKAGE_LABEL_OLD = "label_old"   # 老标签，需要换标
PACKAGE_DAMAGED = "damaged"       # 包装破损

PACKAGE_STATES: frozenset[str] = frozenset(
    {PACKAGE_INTACT, PACKAGE_SCRATCHED, PACKAGE_LABEL_OLD, PACKAGE_DAMAGED}
)


def validate_version(version: str) -> str:
    version = str(version or "").strip()
    if not VERSION_FORMAT.fullmatch(version):
        raise ValidationError("规则版本格式无效")
    return version


def validate_matrix(matrix: Any) -> dict[str, str]:
    """校验并归一化"包装状态 -> 处置动作"决策矩阵。"""

    if not isinstance(matrix, dict) or not matrix:
        raise ValidationError("decision_matrix 必须是非空对象")
    normalized: dict[str, str] = {}
    for state, action in matrix.items():
        if state not in PACKAGE_STATES:
            raise ValidationError(f"未知包装状态: {state}")
        if action not in ACTIONS:
            raise ValidationError(f"未知处置动作: {action}")
        normalized[state] = action
    return normalized


def decide_action(matrix: dict[str, str], package_state: str) -> str:
    """按规则集为包装状态选出处置动作；规则未覆盖时默认退仓。"""

    return matrix.get(package_state, ACTION_RETURN)
