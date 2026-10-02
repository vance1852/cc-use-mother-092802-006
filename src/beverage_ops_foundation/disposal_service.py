"""老产品处置平台领域服务。

在基础服务的稳定边界（操作者/权限、幂等回执、SQLite 事务、哈希审计链）上实现：

- 批次档案：库龄、包装状态、渠道位置、规则版本快照、责任方；
- 规则版本：不可变的命名版本，决策矩阵与损益口径永久冻结；
- 处置计划：草稿 -> 提交 -> 审批，审批后才生成提货/换包/折价/退仓/销毁步骤；
- 跨主体交接：双方确认，发出在途与签收分开落账，任何时刻数量守恒；
- 风险冻结：只冻结受影响批次/数量，其余部分照常执行；
- 终态保护：销毁与退款（折价结算）一经确认即不可被普通修订撤销；
- 确定性边界：晚到回执、重复扫描、计划改版都有明确且可复现的行为。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from .audit import append_event, canonical_json
from .disposal_models import (
    Batch,
    CustodyEvent,
    FreezeRecord,
    LossBasisLine,
    PendingAction,
    PlanLine,
    DisposalPlan,
    DisposalStep,
    ScanRecord,
)
from .disposal_rules import (
    ACTION_DESTROY,
    ACTION_DISCOUNT,
    ACTION_PICKUP,
    ACTION_REPACK,
    ACTION_OUTCOMES,
    ACTIONS,
    PACKAGE_STATES,
    decide_action,
    validate_matrix,
    validate_version,
)
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService

LOCATION_CHANNEL = "channel"
LOCATION_SELF_WAREHOUSE = "self_warehouse"
LOCATION_IN_TRANSIT = "in_transit"
LOCATION_REPACK_STATION = "repack_station"
LOCATION_DESTROYED = "destroyed"
LOCATION_TYPES = frozenset({LOCATION_CHANNEL, LOCATION_SELF_WAREHOUSE})

PLAN_DRAFT = "draft"
PLAN_SUBMITTED = "submitted"
PLAN_APPROVED = "approved"
PLAN_REJECTED = "rejected"
PLAN_SUPERSEDED = "superseded"

STEP_PENDING = "pending"
STEP_IN_PROGRESS = "in_progress"
STEP_AWAITING = "awaiting_counterparty"
STEP_DONE = "done"
STEP_SUPERSEDED = "superseded"
ACTIVE_STEP_STATUSES = frozenset({STEP_PENDING, STEP_IN_PROGRESS, STEP_AWAITING})

DEFAULT_DUE_DAYS = 7


class DisposalService(DomainService):
    """实现老产品处置全流程的领域服务。"""

    # ------------------------------------------------------------------ 规则版本

    def register_ruleset(self, *, request_id: str, actor_id: str, family: str, version: str,
                         decision_matrix: dict[str, str], loss_basis: dict[str, Any]) -> Any:
        family = self._text(family, "family", 64)
        version = validate_version(version)
        matrix = validate_matrix(decision_matrix)
        if not isinstance(loss_basis, dict) or not loss_basis:
            raise ValidationError("loss_basis 必须是非空对象")
        payload = {"actor_id": actor_id, "family": family, "version": version,
                   "decision_matrix": matrix, "loss_basis": loss_basis}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            existing = conn.execute(
                "SELECT * FROM disposal_rulesets WHERE family=? AND version=?", (family, version)
            ).fetchone()
            if existing:
                if existing["decision_matrix_json"] != canonical_json(matrix) \
                        or existing["loss_basis_json"] != canonical_json(loss_basis):
                    raise ConflictError("规则集同版本号已存在且内容不同，版本不可覆盖")
                return WriteReceipt(self._identifier(request_id, "request_id"),
                                    "ruleset", existing["ruleset_id"], True)

            def create() -> tuple[str, str, dict[str, Any]]:
                ruleset_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO disposal_rulesets(ruleset_id,family,version,decision_matrix_json,"
                    "loss_basis_json,registered_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (ruleset_id, family, version, canonical_json(matrix), canonical_json(loss_basis),
                     actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="ruleset.registered",
                             resource_type="ruleset", resource_id=ruleset_id,
                             detail={"family": family, "version": version, "decision_matrix": matrix},
                             occurred_at=self._now())
                return "ruleset", ruleset_id, {"ruleset_id": ruleset_id}

            return self._idempotent(conn, request_id=request_id, action="register_ruleset",
                                    payload=payload, create=create)

    def _ruleset(self, conn, ruleset_id: str):
        row = conn.execute("SELECT * FROM disposal_rulesets WHERE ruleset_id=?", (ruleset_id,)).fetchone()
        if row is None:
            raise NotFoundError("处置规则版本不存在")
        return row

    # ------------------------------------------------------------------ 批次

    def register_batch(self, *, request_id: str, actor_id: str, product_id: str, product_name: str,
                       quantity: int, unit: str, age_days: int, package_state: str,
                       location_type: str, location_ref: str, ruleset_id: str,
                       dealer_id: str = "", custodian_actor_id: str = "") -> Any:
        payload = {"actor_id": actor_id, "product_id": product_id, "quantity": quantity,
                   "age_days": age_days, "package_state": package_state,
                   "location_type": location_type, "location_ref": location_ref,
                   "ruleset_id": ruleset_id, "dealer_id": dealer_id,
                   "custodian_actor_id": custodian_actor_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            product_id = self._identifier(product_id, "product_id")
            product_name = self._text(product_name, "product_name")
            unit = self._text(unit, "unit", 16)
            if not isinstance(quantity, int) or quantity <= 0:
                raise ValidationError("quantity 必须是正整数")
            if not isinstance(age_days, int) or age_days < 0:
                raise ValidationError("age_days 必须是非负整数")
            if package_state not in PACKAGE_STATES:
                raise ValidationError("package_state 不在允许范围内")
            if location_type not in LOCATION_TYPES:
                raise ValidationError("location_type 必须是 channel 或 self_warehouse")
            location_ref = self._text(location_ref, "location_ref", 128)
            dealer_id = str(dealer_id or "").strip()
            if location_type == LOCATION_CHANNEL and not dealer_id:
                raise ValidationError("渠道位置必须提供 dealer_id")
            if location_type == LOCATION_SELF_WAREHOUSE:
                dealer_id = ""
            ruleset = self._ruleset(conn, ruleset_id)
            custodian_id = custodian_actor_id or actor_id
            custodian = self._actor(conn, custodian_id)
            if location_type == LOCATION_SELF_WAREHOUSE and actor.role != "admin" \
                    and custodian.organization_id != actor.organization_id:
                raise PermissionDenied("自有库存的保管责任方必须属于登记主体")

            def create() -> tuple[str, str, dict[str, Any]]:
                batch_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO disposal_batches(batch_id,product_id,product_name,original_quantity,quantity,"
                    "unit,age_days,package_state,location_type,location_ref,dealer_id,owner_org_id,"
                    "custodian_actor_id,ruleset_id,ruleset_version,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, product_id, product_name, quantity, quantity, unit, age_days, package_state,
                     location_type, location_ref, dealer_id, actor.organization_id, custodian_id,
                     ruleset_id, ruleset["version"], self._now()),
                )
                conn.execute(
                    "INSERT INTO disposal_batch_stocks(batch_id,location_type,location_ref,"
                    "custodian_actor_id,quantity) VALUES(?,?,?,?,?)",
                    (batch_id, location_type, location_ref, custodian_id, quantity),
                )
                append_event(conn, actor_id=actor_id, action="batch.registered",
                             resource_type="batch", resource_id=batch_id,
                             detail={"product_id": product_id, "quantity": quantity, "age_days": age_days,
                                     "package_state": package_state, "location_type": location_type,
                                     "location_ref": location_ref, "dealer_id": dealer_id,
                                     "custodian_actor_id": custodian_id,
                                     "ruleset_id": ruleset_id, "ruleset_version": ruleset["version"]},
                             occurred_at=self._now())
                return "batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(conn, request_id=request_id, action="register_batch",
                                    payload=payload, create=create)

    def _batch(self, conn, batch_id: str):
        row = conn.execute("SELECT * FROM disposal_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def _batch_view(self, conn, row) -> Batch:
        freezes = conn.execute(
            "SELECT * FROM disposal_freezes WHERE batch_id=? AND lifted_at='' ORDER BY raised_at", (row["batch_id"],)
        ).fetchall()
        if freezes:
            whole = [f for f in freezes if f["scope"] == "batch"]
            scope = "batch" if whole else "partial"
            reason = whole[0]["reason"] if whole else "；".join(sorted({f["reason"] for f in freezes}))
            frozen = True
        else:
            scope, reason, frozen = "", "", False
        return Batch(row["batch_id"], row["product_id"], row["product_name"], row["quantity"], row["unit"],
                     row["age_days"], row["package_state"], row["location_type"], row["location_ref"],
                     row["dealer_id"], row["owner_org_id"], row["custodian_actor_id"],
                     row["ruleset_id"], row["ruleset_version"], frozen, scope, reason,
                     row["created_at"], row["version"])

    def get_batch(self, batch_id: str) -> Batch:
        with self.database.transaction() as conn:
            return self._batch_view(conn, self._batch(conn, batch_id))

    def list_batches(self, *, product_id: str = "", dealer_id: str = "") -> list[Batch]:
        query = "SELECT * FROM disposal_batches"
        clauses, params = [], []
        if product_id:
            clauses.append("product_id=?")
            params.append(product_id)
        if dealer_id:
            clauses.append("dealer_id=?")
            params.append(dealer_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, batch_id"
        with self.database.transaction() as conn:
            return [self._batch_view(conn, row)
                    for row in conn.execute(query, params)]

    # ------------------------------------------------------------------ 数量账

    def _adjust_stock(self, conn, batch_id: str, location_type: str, location_ref: str,
                      custodian: str, delta: int) -> None:
        row = conn.execute(
            "SELECT quantity FROM disposal_batch_stocks WHERE batch_id=? AND location_type=? "
            "AND location_ref=? AND custodian_actor_id=?",
            (batch_id, location_type, location_ref, custodian),
        ).fetchone()
        new_qty = (row["quantity"] if row else 0) + delta
        if new_qty < 0:
            raise ConflictError("库存数量不足，数量守恒校验失败")
        if row:
            if new_qty == 0:
                conn.execute(
                    "DELETE FROM disposal_batch_stocks WHERE batch_id=? AND location_type=? "
                    "AND location_ref=? AND custodian_actor_id=?",
                    (batch_id, location_type, location_ref, custodian),
                )
            else:
                conn.execute(
                    "UPDATE disposal_batch_stocks SET quantity=? WHERE batch_id=? AND location_type=? "
                    "AND location_ref=? AND custodian_actor_id=?",
                    (new_qty, batch_id, location_type, location_ref, custodian),
                )
        elif delta > 0:
            conn.execute(
                "INSERT INTO disposal_batch_stocks(batch_id,location_type,location_ref,"
                "custodian_actor_id,quantity) VALUES(?,?,?,?,?)",
                (batch_id, location_type, location_ref, custodian, new_qty),
            )

    def _active_freeze_qty(self, conn, batch_id: str) -> tuple[int, bool]:
        """返回(冻结数量, 是否整批冻结)。整批冻结直接阻断一切作业。"""

        rows = conn.execute(
            "SELECT scope, affected_quantity FROM disposal_freezes WHERE batch_id=? AND lifted_at=''",
            (batch_id,),
        ).fetchall()
        whole = any(r["scope"] == "batch" for r in rows)
        return (sum(r["affected_quantity"] for r in rows), whole)

    def _available_qty(self, conn, batch_id: str) -> int:
        batch = self._batch(conn, batch_id)
        frozen, whole = self._active_freeze_qty(conn, batch_id)
        if whole:
            return 0
        return max(0, batch["quantity"] - frozen)

    def verify_conservation(self, batch_id: str) -> dict[str, Any]:
        """核对原始量 = 当前各处在库(含在途) + 已销毁 + 已折价售出。"""

        with self.database.transaction() as conn:
            return self._conservation(conn, batch_id)

    def _conservation(self, conn, batch_id: str) -> dict[str, Any]:
        batch = self._batch(conn, batch_id)
        stocks = [dict(location_type=r["location_type"], location_ref=r["location_ref"],
                       custodian_actor_id=r["custodian_actor_id"], quantity=r["quantity"])
                  for r in conn.execute(
                      "SELECT * FROM disposal_batch_stocks WHERE batch_id=? ORDER BY location_type,location_ref",
                      (batch_id,))]
        on_hand = sum(s["quantity"] for s in stocks)
        destroyed = conn.execute(
            "SELECT COALESCE(SUM(done_qty),0) AS q FROM disposal_steps "
            "WHERE batch_id=? AND action=? AND irreversibly_closed=1",
            (batch_id, ACTION_DESTROY),
        ).fetchone()["q"]
        discounted = conn.execute(
            "SELECT COALESCE(SUM(done_qty),0) AS q FROM disposal_steps "
            "WHERE batch_id=? AND action=? AND irreversibly_closed=1",
            (batch_id, ACTION_DISCOUNT),
        ).fetchone()["q"]
        balanced = on_hand + destroyed + discounted == batch["original_quantity"]
        return {"batch_id": batch_id, "original_quantity": batch["original_quantity"],
                "on_hand_quantity": on_hand, "destroyed_quantity": destroyed,
                "discounted_quantity": discounted, "stocks": stocks, "balanced": balanced}

    # ------------------------------------------------------------------ 冻结

    def raise_freeze(self, *, request_id: str, actor_id: str, batch_id: str, scope: str,
                     reason: str, affected_quantity: int | None = None, scope_ref: str = "",
                     is_quality: bool = True) -> Any:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "scope": scope, "reason": reason,
                   "affected_quantity": affected_quantity, "scope_ref": scope_ref, "is_quality": is_quality}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            batch = self._batch(conn, batch_id)
            reason = self._text(reason, "reason", 300)
            if scope not in {"batch", "package_group", "quantity"}:
                raise ValidationError("scope 必须是 batch / package_group / quantity")
            if scope == "batch":
                affected_quantity = batch["quantity"]
            else:
                if not isinstance(affected_quantity, int) or affected_quantity <= 0:
                    raise ValidationError("affected_quantity 必须是正整数")
                if affected_quantity > batch["quantity"]:
                    raise ValidationError("冻结数量不能超过批次当前数量")
                already = conn.execute(
                    "SELECT COALESCE(SUM(affected_quantity),0) AS q FROM disposal_freezes "
                    "WHERE batch_id=? AND lifted_at='' AND scope!='batch'", (batch_id,)).fetchone()["q"]
                if already + affected_quantity > batch["quantity"]:
                    raise ValidationError("累计冻结数量不能超过批次当前数量")
                if scope == "package_group" and not self._text(scope_ref, "scope_ref", 128):
                    raise ValidationError("包装组冻结必须提供 scope_ref")

            def create() -> tuple[str, str, dict[str, Any]]:
                freeze_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO disposal_freezes(freeze_id,batch_id,scope,scope_ref,affected_quantity,"
                    "reason,is_quality,raised_by,raised_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (freeze_id, batch_id, scope, scope_ref, affected_quantity, reason,
                     1 if is_quality else 0, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="freeze.raised",
                             resource_type="freeze", resource_id=freeze_id,
                             detail={"batch_id": batch_id, "scope": scope, "scope_ref": scope_ref,
                                     "affected_quantity": affected_quantity, "reason": reason,
                                     "is_quality": is_quality}, occurred_at=self._now())
                return "freeze", freeze_id, {"freeze_id": freeze_id}

            return self._idempotent(conn, request_id=request_id, action="raise_freeze",
                                    payload=payload, create=create)

    def lift_freeze(self, *, request_id: str, actor_id: str, freeze_id: str) -> Any:
        payload = {"actor_id": actor_id, "freeze_id": freeze_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer")
            freeze = conn.execute("SELECT * FROM disposal_freezes WHERE freeze_id=?", (freeze_id,)).fetchone()
            if freeze is None:
                raise NotFoundError("冻结记录不存在")
            if freeze["lifted_at"]:
                raise ConflictError("冻结已经解除")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE disposal_freezes SET lifted_by=?, lifted_at=? WHERE freeze_id=?",
                             (actor_id, self._now(), freeze_id))
                append_event(conn, actor_id=actor_id, action="freeze.lifted",
                             resource_type="freeze", resource_id=freeze_id,
                             detail={"batch_id": freeze["batch_id"]}, occurred_at=self._now())
                return "freeze", freeze_id, {"freeze_id": freeze_id, "lifted": True}

            return self._idempotent(conn, request_id=request_id, action="lift_freeze",
                                    payload=payload, create=create)

    def list_freezes(self, batch_id: str, *, include_lifted: bool = False) -> list[FreezeRecord]:
        sql = "SELECT * FROM disposal_freezes WHERE batch_id=?"
        if not include_lifted:
            sql += " AND lifted_at=''"
        sql += " ORDER BY raised_at"
        with self.database.transaction() as conn:
            self._batch(conn, batch_id)
            return [FreezeRecord(r["freeze_id"], r["batch_id"], r["scope"], r["affected_quantity"],
                                 r["reason"], bool(r["is_quality"]), r["raised_by"], r["raised_at"],
                                 r["lifted_at"]) for r in conn.execute(sql, (batch_id,))]

    # ------------------------------------------------------------------ 计划

    def _validate_lines(self, conn, raw_lines: Any, ruleset_row) -> list[dict[str, Any]]:
        if not isinstance(raw_lines, list) or not raw_lines:
            raise ValidationError("lines 必须是非空数组")
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        matrix = json.loads(ruleset_row["decision_matrix_json"])
        for raw in raw_lines:
            if not isinstance(raw, dict):
                raise ValidationError("计划行必须是对象")
            batch_id = raw.get("batch_id", "")
            if batch_id in seen:
                raise ValidationError("同一计划内批次不能重复")
            seen.add(batch_id)
            batch = self._batch(conn, batch_id)
            if batch["ruleset_id"] != ruleset_row["ruleset_id"]:
                raise ValidationError(f"批次 {batch_id} 登记的规则版本与本计划不一致")
            qty = raw.get("quantity")
            if not isinstance(qty, int) or qty <= 0:
                raise ValidationError("计划行 quantity 必须是正整数")
            if qty > batch["quantity"]:
                raise ValidationError(f"批次 {batch_id} 计划数量超过批次当前数量")
            action = raw.get("action") or decide_action(matrix, batch["package_state"])
            if action not in ACTIONS:
                raise ValidationError(f"未知处置动作: {action}")
            # 计划行可显式锁定出货位置（批次分散在多处时必需）；默认取存量最大的位置。
            want_type = str(raw.get("location_type", "")).strip()
            want_ref = str(raw.get("location_ref", "")).strip()
            locations = conn.execute(
                "SELECT location_type, location_ref, custodian_actor_id, SUM(quantity) AS q "
                "FROM disposal_batch_stocks WHERE batch_id=? GROUP BY location_type, location_ref, "
                "custodian_actor_id ORDER BY q DESC",
                (batch_id,),
            ).fetchall()
            if want_type:
                if want_type not in {LOCATION_CHANNEL, LOCATION_SELF_WAREHOUSE,
                                    LOCATION_REPACK_STATION, LOCATION_IN_TRANSIT}:
                    raise ValidationError("计划行 location_type 不被支持")
                matched = [r for r in locations
                           if r["location_type"] == want_type
                           and (not want_ref or r["location_ref"] == want_ref)]
                if not matched:
                    raise ValidationError(
                        f"批次 {batch_id} 在指定位置 {want_type}:{want_ref or '*'} 没有在库")
                current = matched[0]
            else:
                current = locations[0]
            total_here = sum(r["q"] for r in locations)
            if qty > total_here:
                raise ValidationError(
                    f"批次 {batch_id} 现存数量 {total_here} 不足，已销毁或折价售出的数量不能再排入计划")
            # 一条计划行从单一位置出货；分散在多个位置时请按位置拆成不同计划。
            if qty > current["q"]:
                raise ValidationError(
                    f"批次 {batch_id} 在位置 {current['location_type']}:{current['location_ref']} "
                    f"最多可排 {current['q']}")
            normalized.append({"batch_id": batch_id, "quantity": qty, "action": action,
                               "location_type": current["location_type"],
                               "location_ref": current["location_ref"], "dealer_id": batch["dealer_id"],
                               "custodian": current["custodian_actor_id"]})
        return normalized

    def create_plan(self, *, request_id: str, actor_id: str, ruleset_id: str, lines: list[dict[str, Any]],
                    warehouse_ref: str, repack_station_ref: str, responsible_actor_id: str = "",
                    due_days: int = DEFAULT_DUE_DAYS) -> Any:
        payload = {"actor_id": actor_id, "ruleset_id": ruleset_id, "lines": lines,
                   "warehouse_ref": warehouse_ref, "repack_station_ref": repack_station_ref,
                   "responsible_actor_id": responsible_actor_id, "due_days": due_days}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            ruleset = self._ruleset(conn, ruleset_id)
            warehouse_ref = self._text(warehouse_ref, "warehouse_ref", 128)
            repack_station_ref = self._text(repack_station_ref, "repack_station_ref", 128)
            if not isinstance(due_days, int) or due_days <= 0:
                raise ValidationError("due_days 必须是正整数")
            responsible_id = responsible_actor_id or actor_id
            responsible = self._actor(conn, responsible_id)
            if responsible.organization_id != actor.organization_id and actor.role != "admin":
                raise PermissionDenied("责任人必须属于同一主体")
            normalized = self._validate_lines(conn, lines, ruleset)

            def create() -> tuple[str, str, dict[str, Any]]:
                plan_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO disposal_plans(plan_id,status,ruleset_id,ruleset_version,"
                    "responsible_actor_id,warehouse_ref,repack_station_ref,created_by,created_at,due_days) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, PLAN_DRAFT, ruleset_id, ruleset["version"], responsible_id,
                     warehouse_ref, repack_station_ref, actor_id, self._now(), due_days),
                )
                for line in normalized:
                    conn.execute(
                        "INSERT INTO disposal_plan_lines(line_id,plan_id,batch_id,quantity,"
                        "suggested_action,location_type,location_ref,dealer_id,custodian_snapshot) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, plan_id, line["batch_id"], line["quantity"], line["action"],
                         line["location_type"], line["location_ref"], line["dealer_id"], line["custodian"]),
                    )
                append_event(conn, actor_id=actor_id, action="plan.created",
                             resource_type="plan", resource_id=plan_id,
                             detail={"ruleset_id": ruleset_id, "lines": len(normalized),
                                     "due_days": due_days}, occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id, "status": PLAN_DRAFT}

            return self._idempotent(conn, request_id=request_id, action="create_plan",
                                    payload=payload, create=create)

    def _plan(self, conn, plan_id: str):
        row = conn.execute("SELECT * FROM disposal_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("处置计划不存在")
        return row

    def _plan_lines(self, conn, plan_id: str) -> list[PlanLine]:
        return [PlanLine(r["batch_id"], r["quantity"], r["suggested_action"], r["location_type"],
                         r["location_ref"], r["dealer_id"])
                for r in conn.execute(
                    "SELECT * FROM disposal_plan_lines WHERE plan_id=? ORDER BY line_id", (plan_id,))]

    def get_plan(self, plan_id: str) -> DisposalPlan:
        with self.database.transaction() as conn:
            row = self._plan(conn, plan_id)
            return DisposalPlan(row["plan_id"], row["status"], row["ruleset_id"], row["ruleset_version"],
                                tuple(self._plan_lines(conn, plan_id)), row["responsible_actor_id"],
                                row["created_by"], row["created_at"], row["submitted_at"],
                                row["approved_by"], row["approved_at"], row["reject_reason"],
                                row["revision_of"], row["revision"])

    def update_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                    lines: list[dict[str, Any]]) -> Any:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "lines": lines}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            plan = self._plan(conn, plan_id)
            if plan["status"] != PLAN_DRAFT:
                raise ConflictError("只有草稿计划可以直接修改，非草稿请发起改版")
            if plan["created_by"] != actor_id and actor.role != "admin":
                raise PermissionDenied("只能修改自己创建的草稿")
            ruleset = self._ruleset(conn, plan["ruleset_id"])
            normalized = self._validate_lines(conn, lines, ruleset)

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("DELETE FROM disposal_plan_lines WHERE plan_id=?", (plan_id,))
                for line in normalized:
                    conn.execute(
                        "INSERT INTO disposal_plan_lines(line_id,plan_id,batch_id,quantity,"
                        "suggested_action,location_type,location_ref,dealer_id,custodian_snapshot) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, plan_id, line["batch_id"], line["quantity"], line["action"],
                         line["location_type"], line["location_ref"], line["dealer_id"], line["custodian"]),
                    )
                append_event(conn, actor_id=actor_id, action="plan.updated",
                             resource_type="plan", resource_id=plan_id,
                             detail={"lines": len(normalized)}, occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id, "status": PLAN_DRAFT, "updated": True}

            return self._idempotent(conn, request_id=request_id, action="update_plan",
                                    payload=payload, create=create)

    def submit_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> Any:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            plan = self._plan(conn, plan_id)
            if plan["created_by"] != actor_id and actor.role != "admin":
                raise PermissionDenied("只能提交自己创建的计划")
            if plan["status"] != PLAN_DRAFT:
                raise ConflictError("只有草稿计划可以提交")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE disposal_plans SET status=?, submitted_at=? WHERE plan_id=?",
                             (PLAN_SUBMITTED, self._now(), plan_id))
                append_event(conn, actor_id=actor_id, action="plan.submitted",
                             resource_type="plan", resource_id=plan_id, detail={},
                             occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id, "status": PLAN_SUBMITTED}

            return self._idempotent(conn, request_id=request_id, action="submit_plan",
                                    payload=payload, create=create)

    def approve_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> Any:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer")
            plan = self._plan(conn, plan_id)
            if plan["status"] != PLAN_SUBMITTED:
                raise ConflictError("只有已提交的计划可以审批")
            if plan["created_by"] == actor_id:
                raise PermissionDenied("审批人与创建人不能为同一人")
            ruleset = self._ruleset(conn, plan["ruleset_id"])
            plan_lines = conn.execute(
                "SELECT * FROM disposal_plan_lines WHERE plan_id=?", (plan_id,)).fetchall()

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                due = (self.clock.now() + timedelta(days=plan["due_days"])) \
                    .isoformat().replace("+00:00", "Z")
                generated: list[str] = []
                for line in plan_lines:
                    batch = self._batch(conn, line["batch_id"])
                    available = self._available_qty(conn, line["batch_id"])
                    if available < line["quantity"]:
                        raise ConflictError(
                            f"批次 {line['batch_id']} 存在冻结，可执行数量 {available} 不足 {line['quantity']}")
                    committed = conn.execute(
                        "SELECT COALESCE(SUM(s.required_qty - s.done_qty),0) AS q "
                        "FROM disposal_steps s JOIN disposal_plans p ON s.plan_id=p.plan_id "
                        "WHERE s.batch_id=? AND p.status=? AND s.status IN (?,?,?)",
                        (line["batch_id"], PLAN_APPROVED, STEP_PENDING, STEP_IN_PROGRESS, STEP_AWAITING),
                    ).fetchone()["q"]
                    stock_here = conn.execute(
                        "SELECT COALESCE(SUM(quantity),0) AS q FROM disposal_batch_stocks "
                        "WHERE batch_id=? AND location_type=? AND location_ref=?",
                        (line["batch_id"], line["location_type"], line["location_ref"]),
                    ).fetchone()["q"]
                    if stock_here - committed < line["quantity"]:
                        raise ConflictError(f"批次 {line['batch_id']} 已被其他在执行计划占用，可用数量不足")
                    counterparty = self._current_custodian(
                        conn, line["batch_id"], line["location_type"], line["location_ref"],
                        line["custodian_snapshot"])
                    sequence = 1
                    action = line["suggested_action"]
                    if action == ACTION_REPACK:
                        self._insert_step(conn, plan, line, batch, sequence, ACTION_PICKUP,
                                          line["location_type"], line["location_ref"],
                                          LOCATION_REPACK_STATION, plan["repack_station_ref"],
                                          counterparty, due, now)
                        generated.append(f"{line['batch_id']}:{sequence}")
                        sequence += 1
                        self._insert_step(conn, plan, line, batch, sequence, ACTION_REPACK,
                                          LOCATION_REPACK_STATION, plan["repack_station_ref"],
                                          LOCATION_SELF_WAREHOUSE, plan["warehouse_ref"],
                                          plan["responsible_actor_id"], due, now)
                        generated.append(f"{line['batch_id']}:{sequence}")
                    elif action == ACTION_DESTROY:
                        self._insert_step(conn, plan, line, batch, sequence, ACTION_DESTROY,
                                          line["location_type"], line["location_ref"],
                                          LOCATION_DESTROYED, "", counterparty, due, now)
                        generated.append(f"{line['batch_id']}:{sequence}")
                    elif action == ACTION_DISCOUNT:
                        self._insert_step(conn, plan, line, batch, sequence, ACTION_DISCOUNT,
                                          line["location_type"], line["location_ref"],
                                          line["location_type"], line["location_ref"],
                                          counterparty, due, now)
                        generated.append(f"{line['batch_id']}:{sequence}")
                    else:  # pickup / return 都是收回自有仓
                        self._insert_step(conn, plan, line, batch, sequence, action,
                                          line["location_type"], line["location_ref"],
                                          LOCATION_SELF_WAREHOUSE, plan["warehouse_ref"],
                                          counterparty, due, now)
                        generated.append(f"{line['batch_id']}:{sequence}")
                    conn.execute("UPDATE disposal_plan_lines SET locked=1 WHERE line_id=?", (line["line_id"],))
                conn.execute("UPDATE disposal_plans SET status=?, approved_by=?, approved_at=? WHERE plan_id=?",
                             (PLAN_APPROVED, actor_id, now, plan_id))
                append_event(conn, actor_id=actor_id, action="plan.approved",
                             resource_type="plan", resource_id=plan_id,
                             detail={"steps": generated, "ruleset_version": ruleset["version"]},
                             occurred_at=now)
                return "plan", plan_id, {"plan_id": plan_id, "status": PLAN_APPROVED,
                                         "steps_generated": len(generated)}

            return self._idempotent(conn, request_id=request_id, action="approve_plan",
                                    payload=payload, create=create)

    def _current_custodian(self, conn, batch_id: str, location_type: str, location_ref: str,
                           fallback: str) -> str:
        row = conn.execute(
            "SELECT custodian_actor_id FROM disposal_batch_stocks "
            "WHERE batch_id=? AND location_type=? AND location_ref=? ORDER BY quantity DESC LIMIT 1",
            (batch_id, location_type, location_ref),
        ).fetchone()
        return row["custodian_actor_id"] if row else fallback

    def _insert_step(self, conn, plan, line, batch, sequence: int, action: str,
                     from_type: str, from_ref: str, to_type: str, to_ref: str,
                     counterparty: str, due: str, now: str) -> None:
        conn.execute(
            "INSERT INTO disposal_steps(step_id,plan_id,batch_id,sequence,action,required_qty,"
            "done_qty,status,from_location_type,from_location_ref,to_location_type,to_location_ref,"
            "responsible_actor_id,counterparty_actor_id,due_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, plan["plan_id"], line["batch_id"], sequence, action, line["quantity"],
             0, STEP_PENDING, from_type, from_ref, to_type, to_ref,
             plan["responsible_actor_id"], counterparty, due, now),
        )

    def reject_plan(self, *, request_id: str, actor_id: str, plan_id: str, reason: str) -> Any:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer")
            plan = self._plan(conn, plan_id)
            if plan["status"] != PLAN_SUBMITTED:
                raise ConflictError("只有已提交的计划可以驳回")
            reason = self._text(reason, "reason", 300)

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE disposal_plans SET status=?, rejected_by=?, reject_reason=? WHERE plan_id=?",
                    (PLAN_REJECTED, actor_id, reason, plan_id))
                append_event(conn, actor_id=actor_id, action="plan.rejected",
                             resource_type="plan", resource_id=plan_id, detail={"reason": reason},
                             occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id, "status": PLAN_REJECTED}

            return self._idempotent(conn, request_id=request_id, action="reject_plan",
                                    payload=payload, create=create)

    def revise_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                    lines: list[dict[str, Any]]) -> Any:
        """计划改版：旧版置为 superseded，未开始的步骤随之作废；已形成的事实全部保留。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "lines": lines}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            plan = self._plan(conn, plan_id)
            if plan["status"] not in {PLAN_SUBMITTED, PLAN_REJECTED, PLAN_APPROVED}:
                raise ConflictError("仅已提交、已驳回或已批准的计划可以改版；草稿请直接修改")
            ruleset = self._ruleset(conn, plan["ruleset_id"])
            normalized = self._validate_lines(conn, lines, ruleset)
            if plan["status"] == PLAN_APPROVED:
                blocking = conn.execute(
                    "SELECT COUNT(*) AS c FROM disposal_steps WHERE plan_id=? AND status IN (?,?)",
                    (plan_id, STEP_IN_PROGRESS, STEP_AWAITING),
                ).fetchone()["c"]
                if blocking:
                    raise ConflictError("存在在途或待对方确认的步骤，须先完成或拒收后才能改版")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                conn.execute("UPDATE disposal_plans SET status=? WHERE plan_id=?",
                             (PLAN_SUPERSEDED, plan_id))
                conn.execute(
                    "UPDATE disposal_steps SET status=? WHERE plan_id=? AND status=?",
                    (STEP_SUPERSEDED, plan_id, STEP_PENDING))
                new_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO disposal_plans(plan_id,status,ruleset_id,ruleset_version,"
                    "responsible_actor_id,warehouse_ref,repack_station_ref,created_by,created_at,"
                    "revision_of,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id, PLAN_DRAFT, plan["ruleset_id"], plan["ruleset_version"],
                     plan["responsible_actor_id"], plan["warehouse_ref"], plan["repack_station_ref"],
                     actor_id, now, plan_id, plan["revision"] + 1),
                )
                for line in normalized:
                    conn.execute(
                        "INSERT INTO disposal_plan_lines(line_id,plan_id,batch_id,quantity,"
                        "suggested_action,location_type,location_ref,dealer_id,custodian_snapshot) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, new_id, line["batch_id"], line["quantity"], line["action"],
                         line["location_type"], line["location_ref"], line["dealer_id"], line["custodian"]),
                    )
                append_event(conn, actor_id=actor_id, action="plan.revised",
                             resource_type="plan", resource_id=new_id,
                             detail={"revision_of": plan_id, "revision": plan["revision"] + 1,
                                     "lines": len(normalized)}, occurred_at=now)
                return "plan", new_id, {"plan_id": new_id, "status": PLAN_DRAFT,
                                        "revision_of": plan_id, "revision": plan["revision"] + 1}

            return self._idempotent(conn, request_id=request_id, action="revise_plan",
                                    payload=payload, create=create)

    def list_plans(self, status: str = "") -> list[DisposalPlan]:
        sql = "SELECT * FROM disposal_plans"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at, plan_id"
        with self.database.transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [DisposalPlan(r["plan_id"], r["status"], r["ruleset_id"], r["ruleset_version"],
                                 tuple(self._plan_lines(conn, r["plan_id"])), r["responsible_actor_id"],
                                 r["created_by"], r["created_at"], r["submitted_at"], r["approved_by"],
                                 r["approved_at"], r["reject_reason"], r["revision_of"], r["revision"])
                    for r in rows]

    def list_steps(self, *, plan_id: str = "", batch_id: str = "",
                   step_id: str = "") -> list[DisposalStep]:
        sql = "SELECT * FROM disposal_steps"
        clauses, params = [], []
        if plan_id:
            clauses.append("plan_id=?")
            params.append(plan_id)
        if batch_id:
            clauses.append("batch_id=?")
            params.append(batch_id)
        if step_id:
            clauses.append("step_id=?")
            params.append(step_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY plan_id, batch_id, sequence"
        with self.database.transaction() as conn:
            return [self._step_view(r) for r in conn.execute(sql, params)]

    def _step_view(self, r) -> DisposalStep:
        return DisposalStep(r["step_id"], r["plan_id"], r["batch_id"], r["sequence"], r["action"],
                            r["required_qty"], r["done_qty"], r["status"], r["from_location_type"],
                            r["from_location_ref"], r["to_location_type"], r["to_location_ref"],
                            r["responsible_actor_id"], r["counterparty_actor_id"], r["due_at"],
                            r["completed_at"], bool(r["irreversibly_closed"]),
                            r["closure_kind"], r["settled_at"], r["created_at"])

    # ------------------------------------------------------------------ 交接

    def _active_step(self, conn, step_id: str):
        step = conn.execute("SELECT * FROM disposal_steps WHERE step_id=?", (step_id,)).fetchone()
        if step is None:
            raise NotFoundError("处置步骤不存在")
        if step["status"] == STEP_SUPERSEDED:
            raise ConflictError("步骤已随旧版计划作废，晚到回执不能使其恢复")
        if step["irreversibly_closed"]:
            raise ConflictError("步骤已形成销毁/退款终态，事实不可撤销")
        return step

    def propose_handover(self, *, request_id: str, actor_id: str, step_id: str, quantity: int,
                         evidence_ref: str, occurred_at: str = "") -> Any:
        """责任方发起跨主体交接（提货/退仓/销毁见证）。货物在双方确认前不动账。"""

        payload = {"actor_id": actor_id, "step_id": step_id, "quantity": quantity,
                   "evidence_ref": evidence_ref, "occurred_at": occurred_at}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            step = self._active_step(conn, step_id)
            if step["action"] == ACTION_DISCOUNT:
                raise ValidationError("折价销售不发起交接，使用结算与对方确认")
            if step["action"] == ACTION_REPACK and step["from_location_type"] == LOCATION_REPACK_STATION:
                raise ValidationError("站内换包步骤通过扫描回执完成，不需要交接")
            if actor_id != step["responsible_actor_id"] and actor.role != "admin":
                raise PermissionDenied("只有步骤责任方可以发起交接")
            if not isinstance(quantity, int) or quantity <= 0:
                raise ValidationError("quantity 必须是正整数")
            if step["done_qty"] + quantity > step["required_qty"]:
                raise ValidationError("交接数量超过步骤剩余数量")
            evidence_ref = self._text(evidence_ref, "evidence_ref", 128)
            occurred_at = occurred_at or self._now()
            source_qty = conn.execute(
                "SELECT COALESCE(SUM(quantity),0) AS q FROM disposal_batch_stocks "
                "WHERE batch_id=? AND location_type=? AND location_ref=?",
                (step["batch_id"], step["from_location_type"], step["from_location_ref"]),
            ).fetchone()["q"]
            held = conn.execute(
                "SELECT COALESCE(SUM(quantity),0) AS q FROM disposal_batch_stocks "
                "WHERE batch_id=? AND location_type=? AND location_ref=? AND custodian_actor_id=?",
                (step["batch_id"], step["from_location_type"], step["from_location_ref"],
                 step["counterparty_actor_id"]),
            ).fetchone()["q"]
            pending_qty = conn.execute(
                "SELECT COALESCE(SUM(quantity),0) AS q FROM disposal_custody_events "
                "WHERE step_id=? AND status='pending'", (step_id,)).fetchone()["q"]
            in_transit_qty = conn.execute(
                "SELECT COALESCE(SUM(quantity),0) AS q FROM disposal_custody_events "
                "WHERE step_id=? AND status='in_transit'", (step_id,)).fetchone()["q"]
            outstanding = pending_qty + in_transit_qty
            # pending 的货仍在原保管方账上（尚未起运），in_transit 的货已经划出。
            if held < pending_qty + quantity or source_qty < pending_qty + quantity:
                raise ConflictError("货物已不在原保管方或数量不足，不能发起交接")
            # 已完成、待确认/在途和本次数量之和不得触碰冻结范围。
            available = self._available_qty(conn, step["batch_id"])
            if available < step["done_qty"] + outstanding + quantity:
                raise ConflictError("该数量处于风险冻结范围，禁止交接")
            from_actor = self._actor(conn, step["counterparty_actor_id"])
            to_actor = self._actor(conn, step["responsible_actor_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                event_id = uuid.uuid4().hex
                same_party = from_actor.actor_id == to_actor.actor_id
                conn.execute(
                    "INSERT INTO disposal_custody_events(event_id,step_id,batch_id,direction,quantity,"
                    "from_actor_id,to_actor_id,confirmed_by_from,confirmed_by_to,status,evidence_ref,"
                    "occurred_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    # 初始一律 pending；同一主体时双方确认位预置为 1，由落账逻辑统一置为 confirmed。
                    (event_id, step_id, step["batch_id"],
                     "destroy_witness" if step["action"] == ACTION_DESTROY else "handover",
                     quantity, from_actor.actor_id, to_actor.actor_id,
                     1 if same_party else 0, 1 if same_party else 0,
                     "pending", evidence_ref, occurred_at, self._now()),
                )
                self._apply_confirmation(conn, step, event_id, actor_id)
                final_status = conn.execute(
                    "SELECT status FROM disposal_custody_events WHERE event_id=?", (event_id,)
                ).fetchone()["status"]
                # 已发起但尚待对方确认时，步骤进入执行中（不覆盖已有的待确认状态）。
                if final_status == "pending":
                    conn.execute(
                        "UPDATE disposal_steps SET status=? WHERE step_id=? AND status=?",
                        (STEP_IN_PROGRESS, step_id, STEP_PENDING))
                append_event(conn, actor_id=actor_id, action="custody.proposed",
                             resource_type="custody_event", resource_id=event_id,
                             detail={"step_id": step_id, "quantity": quantity,
                                     "from": from_actor.actor_id, "to": to_actor.actor_id,
                                     "auto_confirmed": same_party}, occurred_at=self._now())
                return "custody_event", event_id, {"event_id": event_id, "status": final_status}

            return self._idempotent(conn, request_id=request_id, action="propose_handover",
                                    payload=payload, create=create)

    def confirm_custody(self, *, request_id: str, actor_id: str, event_id: str) -> Any:
        """交接一方确认。两方都确认后才移动数量；先到的回执先记账、等待另一方。"""

        payload = {"actor_id": actor_id, "event_id": event_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            event = conn.execute(
                "SELECT * FROM disposal_custody_events WHERE event_id=?", (event_id,)).fetchone()
            if event is None:
                raise NotFoundError("交接事件不存在")
            step = self._active_step(conn, event["step_id"])
            if event["status"] == "rejected":
                raise ConflictError("交接已被拒收，不能再确认")
            if event["status"] == "confirmed":
                return self._receipt_like(event_id, "already_confirmed")
            if actor_id not in (event["from_actor_id"], event["to_actor_id"]) and actor.role != "admin":
                raise PermissionDenied("只有交接双方可以确认")

            def create() -> tuple[str, str, dict[str, Any]]:
                if actor_id == event["from_actor_id"]:
                    conn.execute("UPDATE disposal_custody_events SET confirmed_by_from=1 WHERE event_id=?",
                                 (event_id,))
                if actor_id == event["to_actor_id"]:
                    conn.execute("UPDATE disposal_custody_events SET confirmed_by_to=1 WHERE event_id=?",
                                 (event_id,))
                self._apply_confirmation(conn, step, event_id, actor_id)
                append_event(conn, actor_id=actor_id, action="custody.confirmed",
                             resource_type="custody_event", resource_id=event_id,
                             detail={"step_id": event["step_id"], "party": actor_id},
                             occurred_at=self._now())
                refreshed = conn.execute(
                    "SELECT status FROM disposal_custody_events WHERE event_id=?", (event_id,)).fetchone()
                return "custody_event", event_id, {"event_id": event_id, "status": refreshed["status"]}

            return self._idempotent(conn, request_id=request_id, action="confirm_custody",
                                    payload=payload, create=create)

    def _receipt_like(self, event_id: str, status: str):
        from .models import WriteReceipt

        return WriteReceipt("", "custody_event", event_id, True)

    def _apply_confirmation(self, conn, step, event_id: str, actor_id: str) -> None:
        """双方齐备时落账：移动在途/入仓或执行销毁，并推进步骤状态。"""

        event = conn.execute("SELECT * FROM disposal_custody_events WHERE event_id=?",
                             (event_id,)).fetchone()
        if not (event["confirmed_by_from"] and event["confirmed_by_to"]):
            if event["confirmed_by_from"] and event["status"] == "pending":
                # 发出方已确认：先转入在途，账仍然平。
                self._adjust_stock(conn, event["batch_id"], step["from_location_type"],
                                   step["from_location_ref"], event["from_actor_id"], -event["quantity"])
                self._adjust_stock(conn, event["batch_id"], LOCATION_IN_TRANSIT, step["step_id"],
                                   event["to_actor_id"], event["quantity"])
                conn.execute("UPDATE disposal_custody_events SET status='in_transit' WHERE event_id=?",
                             (event_id,))
                self._set_step_status(conn, step, STEP_AWAITING)
            return
        if event["status"] == "confirmed":
            return
        prior_status = event["status"]
        if step["action"] == ACTION_DESTROY:
            if prior_status == "in_transit":
                self._adjust_stock(conn, event["batch_id"], LOCATION_IN_TRANSIT, step["step_id"],
                                   event["to_actor_id"], -event["quantity"])
            else:
                self._adjust_stock(conn, event["batch_id"], step["from_location_type"],
                                   step["from_location_ref"], event["from_actor_id"], -event["quantity"])
            conn.execute("UPDATE disposal_batches SET quantity=quantity-? WHERE batch_id=?",
                         (event["quantity"], event["batch_id"]))
            self._advance_done(conn, step, event["quantity"], irreversible=True, closure_kind="destroyed")
        else:
            if prior_status == "in_transit":
                self._adjust_stock(conn, event["batch_id"], LOCATION_IN_TRANSIT, step["step_id"],
                                   event["to_actor_id"], -event["quantity"])
            else:
                self._adjust_stock(conn, event["batch_id"], step["from_location_type"],
                                   step["from_location_ref"], event["from_actor_id"], -event["quantity"])
            self._adjust_stock(conn, event["batch_id"], step["to_location_type"],
                               step["to_location_ref"], event["to_actor_id"], event["quantity"])
            self._advance_done(conn, step, event["quantity"], irreversible=False)
        conn.execute("UPDATE disposal_custody_events SET status='confirmed' WHERE event_id=?",
                     (event_id,))

    def _advance_done(self, conn, step, qty: int, *, irreversible: bool, closure_kind: str = "") -> None:
        new_done = step["done_qty"] + qty
        if new_done > step["required_qty"]:
            raise ConflictError("步骤完成数量超过要求数量")
        now = self._now()
        if new_done == step["required_qty"]:
            conn.execute(
                "UPDATE disposal_steps SET done_qty=?, status=?, completed_at=?, "
                "irreversibly_closed=?, closure_kind=?, settled_at=? WHERE step_id=?",
                (new_done, STEP_DONE, now, 1 if irreversible else 0, closure_kind,
                 now if irreversible else "", step["step_id"]),
            )
        else:
            conn.execute("UPDATE disposal_steps SET done_qty=?, status=? WHERE step_id=?",
                         (new_done, STEP_IN_PROGRESS, step["step_id"]))

    def _set_step_status(self, conn, step, status: str) -> None:
        if step["status"] in (STEP_PENDING, STEP_IN_PROGRESS, STEP_AWAITING):
            conn.execute("UPDATE disposal_steps SET status=? WHERE step_id=? AND status!=?",
                         (status, step["step_id"], STEP_DONE))

    def reject_custody(self, *, request_id: str, actor_id: str, event_id: str, reason: str) -> Any:
        """拒收。货物一旦进入在途即不可拒收（须按新事件原路退回）。"""

        payload = {"actor_id": actor_id, "event_id": event_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            event = conn.execute("SELECT * FROM disposal_custody_events WHERE event_id=?",
                                 (event_id,)).fetchone()
            if event is None:
                raise NotFoundError("交接事件不存在")
            self._active_step(conn, event["step_id"])
            if event["status"] == "rejected":
                raise ConflictError("交接已经处于拒收状态")
            if event["status"] in ("in_transit", "confirmed"):
                raise ConflictError("货物已发出或已确认，不能拒收，须发起退回交接")
            if actor_id not in (event["from_actor_id"], event["to_actor_id"]) and actor.role != "admin":
                raise PermissionDenied("只有交接双方可以拒收")
            reason = self._text(reason, "reason", 300)

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE disposal_custody_events SET status='rejected', reject_reason=? WHERE event_id=?",
                    (reason, event_id))
                pending = conn.execute(
                    "SELECT COUNT(*) AS c FROM disposal_custody_events WHERE step_id=? AND status='pending'",
                    (event["step_id"],)).fetchone()["c"]
                if not pending:
                    step = conn.execute("SELECT * FROM disposal_steps WHERE step_id=?",
                                        (event["step_id"],)).fetchone()
                    self._set_step_status(conn, step, STEP_IN_PROGRESS if step["done_qty"] else STEP_PENDING)
                append_event(conn, actor_id=actor_id, action="custody.rejected",
                             resource_type="custody_event", resource_id=event_id,
                             detail={"step_id": event["step_id"], "reason": reason},
                             occurred_at=self._now())
                return "custody_event", event_id, {"event_id": event_id, "status": "rejected"}

            return self._idempotent(conn, request_id=request_id, action="reject_custody",
                                    payload=payload, create=create)

    def get_custody_event(self, event_id: str) -> CustodyEvent:
        with self.database.transaction() as conn:
            r = conn.execute("SELECT * FROM disposal_custody_events WHERE event_id=?",
                             (event_id,)).fetchone()
            if r is None:
                raise NotFoundError("交接事件不存在")
            return self._custody_view(conn, r)

    def _custody_view(self, conn, r) -> CustodyEvent:
        step = conn.execute("SELECT due_at FROM disposal_steps WHERE step_id=?",
                            (r["step_id"],)).fetchone()
        # 超过步骤截止时间仍未完成双方确认，即为晚到/逾期回执；已确认事实不受影响。
        late = r["status"] != "confirmed" and bool(step) and r["recorded_at"] > step["due_at"]
        return CustodyEvent(r["event_id"], r["step_id"], r["batch_id"], r["direction"],
                            r["quantity"], r["from_actor_id"], r["to_actor_id"],
                            bool(r["confirmed_by_from"]), bool(r["confirmed_by_to"]), r["status"],
                            r["evidence_ref"], r["occurred_at"], r["recorded_at"], late)

    # ------------------------------------------------------------------ 扫描回执

    def record_scan(self, *, request_id: str, actor_id: str, step_id: str, scan_type: str,
                    quantity: int, evidence_ref: str, occurred_at: str = "") -> Any:
        """登记现场扫描。扫描只作证据，不直接动账（站内换包除外）。

        重复扫描（同步骤/类型/证据编号）确定地标记为 duplicate，不再触发任何效果；
        步骤终结后才到的回执标记 late，不重开步骤。
        """

        payload = {"actor_id": actor_id, "step_id": step_id, "scan_type": scan_type,
                   "quantity": quantity, "evidence_ref": evidence_ref, "occurred_at": occurred_at}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            step = conn.execute("SELECT * FROM disposal_steps WHERE step_id=?", (step_id,)).fetchone()
            if step is None:
                raise NotFoundError("处置步骤不存在")
            if scan_type not in ACTIONS:
                raise ValidationError("scan_type 不在允许范围内")
            if scan_type != step["action"]:
                raise ValidationError("扫描类型必须与步骤动作一致")
            if not isinstance(quantity, int) or quantity < 0:
                raise ValidationError("quantity 必须是非负整数")
            evidence_ref = self._text(evidence_ref, "evidence_ref", 128)
            occurred_at = occurred_at or self._now()
            now = self._now()
            duplicate = conn.execute(
                "SELECT scan_id FROM disposal_scans WHERE step_id=? AND scan_type=? AND evidence_ref=? "
                "AND duplicate_of='' ORDER BY received_at LIMIT 1",
                (step_id, scan_type, evidence_ref)).fetchone()
            # 晚到回执：步骤已完成、已随改版作废，或登记时间已超过步骤截止时间。
            late = bool(step["completed_at"]) or step["status"] == STEP_SUPERSEDED or now > step["due_at"]

            def create() -> tuple[str, str, dict[str, Any]]:
                scan_id = uuid.uuid4().hex
                duplicate_of = duplicate["scan_id"] if duplicate else ""
                conn.execute(
                    "INSERT INTO disposal_scans(scan_id,step_id,batch_id,scan_type,quantity,"
                    "scanner_actor_id,evidence_ref,duplicate_of,late,occurred_at,received_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (scan_id, step_id, step["batch_id"], scan_type, quantity, actor_id, evidence_ref,
                     duplicate_of, 1 if late else 0, occurred_at, now),
                )
                # 站内换包扫描是内部作业，凭回执直接把货从换包站移入自有仓。
                if not duplicate_of and scan_type == ACTION_REPACK \
                        and step["from_location_type"] == LOCATION_REPACK_STATION \
                        and step["status"] in ACTIVE_STEP_STATUSES:
                    if quantity <= 0 or step["done_qty"] + quantity > step["required_qty"]:
                        raise ValidationError("换包扫描数量必须为正且不能超过步骤剩余数量")
                    available = self._available_qty(conn, step["batch_id"])
                    if available < step["done_qty"] + quantity:
                        raise ConflictError("该数量处于风险冻结范围，禁止换包")
                    self._adjust_stock(conn, step["batch_id"], LOCATION_REPACK_STATION,
                                       step["from_location_ref"], step["responsible_actor_id"],
                                       -quantity)
                    self._adjust_stock(conn, step["batch_id"], LOCATION_SELF_WAREHOUSE,
                                       step["to_location_ref"], step["responsible_actor_id"], quantity)
                    self._advance_done(conn, step, quantity, irreversible=False)
                append_event(conn, actor_id=actor_id, action="scan.recorded",
                             resource_type="scan", resource_id=scan_id,
                             detail={"step_id": step_id, "scan_type": scan_type, "quantity": quantity,
                                     "evidence_ref": evidence_ref, "duplicate_of": duplicate_of,
                                     "late": late}, occurred_at=now)
                return "scan", scan_id, {"scan_id": scan_id, "duplicate": bool(duplicate_of), "late": late}

            return self._idempotent(conn, request_id=request_id, action="record_scan",
                                    payload=payload, create=create)

    def list_scans(self, step_id: str = "") -> list[ScanRecord]:
        sql = "SELECT * FROM disposal_scans"
        params: list[Any] = []
        if step_id:
            sql += " WHERE step_id=?"
            params.append(step_id)
        sql += " ORDER BY received_at"
        with self.database.transaction() as conn:
            return [ScanRecord(r["scan_id"], r["step_id"], r["batch_id"], r["scan_type"], r["quantity"],
                               r["scanner_actor_id"], r["evidence_ref"], r["duplicate_of"],
                               r["occurred_at"], r["received_at"], bool(r["late"]))
                    for r in conn.execute(sql, params)]

    # ------------------------------------------------------------------ 折价结算

    def record_settlement(self, *, request_id: str, actor_id: str, step_id: str, amount: float,
                          currency: str, evidence_ref: str) -> Any:
        """登记折价退款/回款事实。一经记录即不可修改或删除。"""

        payload = {"actor_id": actor_id, "step_id": step_id, "amount": amount,
                   "currency": currency, "evidence_ref": evidence_ref}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            step = conn.execute("SELECT * FROM disposal_steps WHERE step_id=?", (step_id,)).fetchone()
            if step is None:
                raise NotFoundError("处置步骤不存在")
            if step["action"] != ACTION_DISCOUNT:
                raise ValidationError("只有折价步骤可以登记结算")
            existing = conn.execute("SELECT 1 FROM disposal_settlements WHERE step_id=?",
                                    (step_id,)).fetchone()
            if existing:
                raise ConflictError("结算事实已经存在，不得重复登记或修改")
            if not isinstance(amount, (int, float)) or amount < 0:
                raise ValidationError("amount 必须是非负数")
            currency = self._text(currency, "currency", 8)
            evidence_ref = self._text(evidence_ref, "evidence_ref", 128)

            def create() -> tuple[str, str, dict[str, Any]]:
                settlement_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO disposal_settlements(settlement_id,step_id,batch_id,amount,currency,"
                    "evidence_ref,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (settlement_id, step_id, step["batch_id"], float(amount), currency, evidence_ref,
                     actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="settlement.recorded",
                             resource_type="settlement", resource_id=settlement_id,
                             detail={"step_id": step_id, "amount": float(amount), "currency": currency},
                             occurred_at=self._now())
                return "settlement", settlement_id, {"settlement_id": settlement_id}

            return self._idempotent(conn, request_id=request_id, action="record_settlement",
                                    payload=payload, create=create)

    def confirm_discount(self, *, request_id: str, actor_id: str, step_id: str) -> Any:
        """渠道方确认折价处置：结算事实 + 折价扫描齐备后，扣减货物并形成不可撤销终态。"""

        payload = {"actor_id": actor_id, "step_id": step_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            step = self._active_step(conn, step_id)
            if step["action"] != ACTION_DISCOUNT:
                raise ValidationError("该步骤不是折价步骤")
            if actor_id != step["counterparty_actor_id"] and actor.role != "admin":
                raise PermissionDenied("折价处置必须由渠道保管方确认")
            settlement = conn.execute("SELECT * FROM disposal_settlements WHERE step_id=?",
                                      (step_id,)).fetchone()
            if settlement is None:
                raise ConflictError("折价步骤必须先登记退款/结算事实")
            scan = conn.execute(
                "SELECT 1 FROM disposal_scans WHERE step_id=? AND scan_type=? AND duplicate_of='' LIMIT 1",
                (step_id, ACTION_DISCOUNT)).fetchone()
            if scan is None:
                raise ConflictError("缺少折价销售扫描回执")
            held = conn.execute(
                "SELECT COALESCE(SUM(quantity),0) AS q FROM disposal_batch_stocks "
                "WHERE batch_id=? AND location_type=? AND location_ref=?",
                (step["batch_id"], step["from_location_type"], step["from_location_ref"]),
            ).fetchone()["q"]
            if held < step["required_qty"] - step["done_qty"]:
                raise ConflictError("现场库存数量不足，无法完成折价确认")
            if self._available_qty(conn, step["batch_id"]) < step["required_qty"] - step["done_qty"]:
                raise ConflictError("该数量处于风险冻结范围，禁止折价")

            def create() -> tuple[str, str, dict[str, Any]]:
                qty = step["required_qty"] - step["done_qty"]
                self._adjust_stock(conn, step["batch_id"], step["from_location_type"],
                                   step["from_location_ref"], step["counterparty_actor_id"], -qty)
                conn.execute("UPDATE disposal_batches SET quantity=quantity-? WHERE batch_id=?",
                             (qty, step["batch_id"]))
                self._advance_done(conn, step, qty, irreversible=True, closure_kind="discount_settled")
                conn.execute("UPDATE disposal_steps SET settled_at=? WHERE step_id=?",
                             (settlement["recorded_at"], step_id))
                append_event(conn, actor_id=actor_id, action="step.closed",
                             resource_type="step", resource_id=step_id,
                             detail={"batch_id": step["batch_id"], "closure_kind": "discount_settled",
                                     "quantity": qty, "settlement_id": settlement["settlement_id"]},
                             occurred_at=self._now())
                return "step", step_id, {"step_id": step_id, "status": STEP_DONE,
                                         "irreversibly_closed": True}

            return self._idempotent(conn, request_id=request_id, action="confirm_discount",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 管理视图

    def pending_actions(self, *, product_id: str = "", dealer_id: str = "") -> list[PendingAction]:
        sql = (
            "SELECT s.*, b.product_id, b.dealer_id AS batch_dealer FROM disposal_steps s "
            "JOIN disposal_batches b ON s.batch_id=b.batch_id WHERE s.status IN (?,?,?)")
        params: list[Any] = [STEP_PENDING, STEP_IN_PROGRESS, STEP_AWAITING]
        if product_id:
            sql += " AND b.product_id=?"
            params.append(product_id)
        if dealer_id:
            sql += " AND b.dealer_id=?"
            params.append(dealer_id)
        sql += " ORDER BY s.due_at, s.step_id"
        now = self._now()
        with self.database.transaction() as conn:
            result = []
            for r in conn.execute(sql, params):
                frozen_qty, whole = self._active_freeze_qty(conn, r["batch_id"])
                result.append(PendingAction(
                    r["step_id"], r["plan_id"], r["batch_id"], r["product_id"], r["batch_dealer"],
                    r["action"], r["status"], r["required_qty"], r["required_qty"] - r["done_qty"],
                    r["responsible_actor_id"], r["due_at"], r["due_at"] < now, whole or frozen_qty > 0))
            return result

    def overdue_actions(self, *, product_id: str = "", dealer_id: str = "") -> list[PendingAction]:
        return [a for a in self.pending_actions(product_id=product_id, dealer_id=dealer_id) if a.overdue]

    def loss_basis(self, *, product_id: str = "", dealer_id: str = "") -> list[LossBasisLine]:
        """损益依据：每个终态步骤对应的数量、结果类型、规则集口径与结算时间。"""

        sql = (
            "SELECT s.*, b.product_id, b.dealer_id AS batch_dealer, p.ruleset_id AS plan_ruleset, "
            "p.ruleset_version AS plan_ruleset_version, r.loss_basis_json "
            "FROM disposal_steps s JOIN disposal_batches b ON s.batch_id=b.batch_id "
            "JOIN disposal_plans p ON s.plan_id=p.plan_id "
            "JOIN disposal_rulesets r ON p.ruleset_id=r.ruleset_id "
            "WHERE s.irreversibly_closed=1")
        params: list[Any] = []
        if product_id:
            sql += " AND b.product_id=?"
            params.append(product_id)
        if dealer_id:
            sql += " AND b.dealer_id=?"
            params.append(dealer)
        sql += " ORDER BY s.settled_at, s.step_id"
        with self.database.transaction() as conn:
            result = []
            for r in conn.execute(sql, params):
                result.append(LossBasisLine(
                    r["batch_id"], r["product_id"], r["action"], ACTION_OUTCOMES[r["action"]],
                    r["done_qty"], json.loads(r["loss_basis_json"]), r["plan_ruleset"],
                    r["plan_ruleset_version"], r["plan_id"], r["step_id"], r["settled_at"]))
            return result

    def custody_chain(self, batch_id: str) -> dict[str, Any]:
        """完整保管链：登记、冻结、计划/步骤、交接确认、扫描、结算与守恒核对。"""

        with self.database.transaction() as conn:
            batch = self._batch(conn, batch_id)
            events: list[tuple[str, str, dict[str, Any]]] = []
            events.append((batch["created_at"], "batch_registered", {
                "batch_id": batch_id, "product_id": batch["product_id"],
                "quantity": batch["original_quantity"], "age_days": batch["age_days"],
                "package_state": batch["package_state"], "location_type": batch["location_type"],
                "location_ref": batch["location_ref"], "dealer_id": batch["dealer_id"],
                "custodian_actor_id": batch["custodian_actor_id"],
                "ruleset_version": batch["ruleset_version"]}))
            for f in conn.execute("SELECT * FROM disposal_freezes WHERE batch_id=? ORDER BY raised_at",
                                  (batch_id,)):
                events.append((f["raised_at"], "freeze_raised", {
                    "freeze_id": f["freeze_id"], "scope": f["scope"], "scope_ref": f["scope_ref"],
                    "affected_quantity": f["affected_quantity"], "reason": f["reason"],
                    "is_quality": bool(f["is_quality"]), "active": not f["lifted_at"]}))
                if f["lifted_at"]:
                    events.append((f["lifted_at"], "freeze_lifted", {"freeze_id": f["freeze_id"]}))
            plan_ids = [r["plan_id"] for r in conn.execute(
                "SELECT DISTINCT plan_id FROM disposal_plan_lines WHERE batch_id=? ORDER BY plan_id",
                (batch_id,))]
            for plan_id in plan_ids:
                p = self._plan(conn, plan_id)
                events.append((p["created_at"], "plan_created", {
                    "plan_id": plan_id, "status": p["status"], "revision": p["revision"],
                    "revision_of": p["revision_of"], "ruleset_version": p["ruleset_version"],
                    "approved_at": p["approved_at"]}))
            for s in conn.execute("SELECT * FROM disposal_steps WHERE batch_id=? ORDER BY created_at,sequence",
                                  (batch_id,)):
                events.append((s["created_at"], "step_generated", {
                    "step_id": s["step_id"], "plan_id": s["plan_id"], "sequence": s["sequence"],
                    "action": s["action"], "required_qty": s["required_qty"],
                    "from": f"{s['from_location_type']}:{s['from_location_ref']}",
                    "to": f"{s['to_location_type']}:{s['to_location_ref']}",
                    "responsible_actor_id": s["responsible_actor_id"],
                    "counterparty_actor_id": s["counterparty_actor_id"], "due_at": s["due_at"]}))
            for e in conn.execute("SELECT * FROM disposal_custody_events WHERE batch_id=? ORDER BY recorded_at",
                                  (batch_id,)):
                events.append((e["recorded_at"], "custody_" + e["direction"], {
                    "event_id": e["event_id"], "step_id": e["step_id"], "quantity": e["quantity"],
                    "from_actor_id": e["from_actor_id"], "to_actor_id": e["to_actor_id"],
                    "confirmed_by_from": bool(e["confirmed_by_from"]),
                    "confirmed_by_to": bool(e["confirmed_by_to"]), "status": e["status"],
                    "evidence_ref": e["evidence_ref"], "occurred_at": e["occurred_at"]}))
            for sc in conn.execute("SELECT * FROM disposal_scans WHERE batch_id=? ORDER BY received_at",
                                   (batch_id,)):
                events.append((sc["received_at"], "scan_recorded", {
                    "scan_id": sc["scan_id"], "step_id": sc["step_id"], "scan_type": sc["scan_type"],
                    "quantity": sc["quantity"], "evidence_ref": sc["evidence_ref"],
                    "duplicate_of": sc["duplicate_of"], "late": bool(sc["late"])}))
            for st in conn.execute("SELECT * FROM disposal_settlements WHERE batch_id=? ORDER BY recorded_at",
                                   (batch_id,)):
                events.append((st["recorded_at"], "settlement_recorded", {
                    "settlement_id": st["settlement_id"], "step_id": st["step_id"],
                    "amount": st["amount"], "currency": st["currency"],
                    "evidence_ref": st["evidence_ref"]}))
            for s in conn.execute(
                    "SELECT * FROM disposal_steps WHERE batch_id=? AND completed_at!='' ORDER BY completed_at",
                    (batch_id,)):
                events.append((s["completed_at"], "step_closed", {
                    "step_id": s["step_id"], "action": s["action"], "done_qty": s["done_qty"],
                    "irreversibly_closed": bool(s["irreversibly_closed"]),
                    "closure_kind": s["closure_kind"]}))
            events.sort(key=lambda item: (item[0], item[1]))
            conservation = self._conservation(conn, batch_id)
            return {"batch_id": batch_id, "product_id": batch["product_id"],
                    "ruleset_id": batch["ruleset_id"], "ruleset_version": batch["ruleset_version"],
                    "conservation": conservation,
                    "events": [{"at": at, "type": kind, "detail": detail} for at, kind, detail in events]}
