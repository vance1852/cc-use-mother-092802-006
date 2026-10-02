"""老产品处置域的领域服务。

在基础库的权限、事务与哈希审计边界内实现：
- 批次台账（库龄、包装、渠道位置、规则版本、责任方）
- 风险范围内冻结
- 处置计划起草/审批/改版，审批后生成可执行步骤
- 跨主体提货、折价、退仓的双方确认交接与数量守恒
- 换包产出、销毁凭证、退款等不可被普通修订撤销的事实
- 未完成动作、责任逾期、损益依据与完整保管链查询
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable

from ..audit import append_event, canonical_json, digest
from ..clock import Clock, SystemClock
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..models import Actor
from ..storage import Database
from .models import Batch, DisposalReceipt, Freeze, RuleVersion
from .storage import ensure_disposal_schema

DECISIONS = frozenset({"sell", "relabel", "discount", "return", "destroy"})
INTERNAL_STEP_TYPES = frozenset({"relabel", "destroy"})
HANDOFF_STEP_TYPES = frozenset({"pickup", "discount", "return_to_warehouse"})
PACKAGE_STATUSES = frozenset({"intact", "faded", "damaged"})
LOCATION_TYPES = frozenset({"self_owned", "channel", "warehouse", "disposal_site"})

# 决策 -> 步骤类型
STEP_TYPE_OF = {
    "sell": "pickup",
    "relabel": "relabel",
    "discount": "discount",
    "return": "return_to_warehouse",
    "destroy": "destroy",
}
# 交接步骤货物到达后的场所类型
TARGET_LOCATION_OF = {
    "pickup": "channel",
    "discount": "channel",
    "return_to_warehouse": "warehouse",
}
_ACTIVE_PLAN_STATES = ("draft", "approved")


class DisposalService:
    """协调处置域的权限、幂等、守恒与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        ensure_disposal_schema(database)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _identifier(self, value: Any, field: str) -> str:
        value = str(value or "").strip()
        if not value or len(value) > 64:
            raise ValidationError(f"{field} 格式无效")
        return value

    def _int(self, value: Any, field: str, *, minimum: int = 1) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if value < minimum:
            raise ValidationError(f"{field} 不能小于 {minimum}")
        return value

    def _iso_datetime(self, value: Any, field: str) -> datetime:
        text = str(value or "").strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _org_exists(self, connection, org_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM organizations WHERE organization_id=?", (org_id,)
        ).fetchone() is not None

    def _same_org_or_admin(self, actor: Actor, org_id: str) -> None:
        if actor.role != "admin" and actor.organization_id != org_id:
            raise PermissionDenied("只能由所属主体的操作者执行")

    def _replay_if_seen(self, connection, request_id: str, action: str,
                        payload: dict[str, Any]) -> DisposalReceipt | None:
        """已处理过的 request_id 直接回放原结果，保证整单重试优先于重复扫描判定。"""

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM disposal_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return DisposalReceipt(request_id, row["resource_type"], row["resource_id"], True,
                               json.loads(row["response_json"]))

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> DisposalReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM disposal_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return DisposalReceipt(request_id, row["resource_type"], row["resource_id"], True,
                                   json.loads(row["response_json"]))
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO disposal_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return DisposalReceipt(request_id, resource_type, resource_id, False, response)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _ledger(self, connection, *, event_type: str, batch_id: str, delta: int,
                org_id: str, actor_id: str, step_id: str | None = None,
                plan_id: str | None = None, plan_version: int | None = None,
                evidence: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO disposal_custody_ledger(ledger_id,event_type,batch_id,quantity_delta,"
            "step_id,plan_id,plan_version,org_id,actor_id,evidence_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, event_type, batch_id, delta, step_id, plan_id, plan_version,
             org_id, actor_id, canonical_json(evidence or {}), self._now()),
        )

    # ------------------------------------------------------------- 规则版本

    def publish_rule(self, *, request_id: str, actor_id: str, rule_id: str,
                     payload: dict[str, Any]) -> DisposalReceipt:
        """发布一个不可变的规则新版本，版本号在 rule_id 内严格递增。"""

        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        body = {"actor_id": actor_id, "rule_id": rule_id, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            rule_id = self._identifier(rule_id, "rule_id")
            for key in ("sell_age_days", "relabel_age_days",
                        "return_age_days", "destroy_age_days"):
                if key in payload:
                    self._int(payload[key], key, minimum=0)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS v FROM disposal_rule_versions WHERE rule_id=?",
                    (rule_id,),
                ).fetchone()
                version = row["v"] + 1
                payload_hash = digest(payload)
                connection.execute(
                    "INSERT INTO disposal_rule_versions(rule_id,version,payload_json,payload_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (rule_id, version, canonical_json(payload), payload_hash, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="disposal.rule.published",
                            resource_type="disposal_rule", resource_id=rule_id,
                            detail={"version": version, "payload_hash": payload_hash})
                return "disposal_rule", f"{rule_id}:{version}", {"rule_id": rule_id, "version": version}

            return self._idempotent(connection, request_id=request_id, action="publish_disposal_rule",
                                    payload=body, create=create)

    def get_rule(self, rule_id: str, version: int | None = None) -> RuleVersion:
        rule_id = self._identifier(rule_id, "rule_id")
        connection = self.database.connection
        if version is None:
            row = connection.execute(
                "SELECT * FROM disposal_rule_versions WHERE rule_id=? ORDER BY version DESC LIMIT 1",
                (rule_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM disposal_rule_versions WHERE rule_id=? AND version=?",
                (rule_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return RuleVersion(row["rule_id"], row["version"], json.loads(row["payload_json"]),
                           row["payload_hash"], row["created_by"], row["created_at"])

    def _rule_payload(self, connection, rule_id: str, version: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json FROM disposal_rule_versions WHERE rule_id=? AND version=?",
            (rule_id, version),
        ).fetchone()
        if row is None:
            raise ValidationError(f"批次引用的规则版本 {rule_id}:{version} 不存在")
        return json.loads(row["payload_json"])

    def _latest_rule_version(self, connection, rule_id: str) -> int:
        row = connection.execute(
            "SELECT MAX(version) AS v FROM disposal_rule_versions WHERE rule_id=?", (rule_id,)
        ).fetchone()
        if row["v"] is None:
            raise NotFoundError("规则不存在，请先发布规则版本")
        return row["v"]

    @staticmethod
    def evaluate_decision(rule_payload: dict[str, Any], *, age_days: int,
                          package_status: str, location_type: str) -> dict[str, str | int]:
        """按规则返回建议处置及理由；同时供计划行做合规校验。

        判定顺序：破损/超寿 → 渠道可退期内退回 → 自有库褪色换标 → 完好且在销售期正常销售
        → 渠道超退期包装完好折价清理 → 自有库临期折价 → 兜底销毁。
        渠道褪色标签货物超过退仓期后既不能退回换标也不能折价销售，只能销毁。
        """

        sell_age = int(rule_payload.get("sell_age_days", 365))
        relabel_age = int(rule_payload.get("relabel_age_days", 730))
        return_age = int(rule_payload.get("return_age_days", 180))
        destroy_age = int(rule_payload.get("destroy_age_days", 1095))
        if package_status == "damaged":
            return {"decision": "destroy", "reason": "包装破损不可再售"}
        if age_days >= destroy_age:
            return {"decision": "destroy", "reason": f"库龄 {age_days} 天达到销毁阈值 {destroy_age} 天"}
        if location_type == "channel" and age_days <= return_age:
            return {"decision": "return", "reason": "渠道库存且在可退仓库龄内"}
        if location_type == "self_owned" and package_status == "faded" \
                and age_days <= relabel_age:
            return {"decision": "relabel", "reason": "标签褪色且在换标库龄内"}
        if package_status == "intact" and age_days <= sell_age:
            if location_type == "channel":
                return {"decision": "discount", "reason": "渠道库存超出可退仓期，折价清理"}
            return {"decision": "sell", "reason": "包装完好且在正常销售库龄内"}
        if package_status == "intact" and age_days <= relabel_age:
            return {"decision": "discount", "reason": "自有库存超出正常销售库龄，临期折价"}
        return {"decision": "destroy", "reason": "不符合继续销售、换标或折价条件"}

    def _decision_allowed(self, connection, decision: str, batch_row, age_days: int) -> None:
        rule_payload = self._rule_payload(connection, batch_row["rule_id"], batch_row["rule_version"])
        recommended = self.evaluate_decision(
            rule_payload, age_days=age_days,
            package_status=batch_row["package_status"],
            location_type=batch_row["location_type"],
        )["decision"]
        # 销毁始终是兜底允许项；其余决策必须与规则判定一致，避免凭主观绕过规则版本。
        if decision != "destroy" and decision != recommended:
            raise ValidationError(
                f"决策 {decision} 不符合规则 {batch_row['rule_id']}:"
                f"{batch_row['rule_version']} 的判定（建议 {recommended}）"
            )

    # --------------------------------------------------------------- 批次台账

    def register_batch(self, *, request_id: str, actor_id: str, batch_id: str, product_id: str,
                       product_name: str, quantity: int, unit: str, produced_at: str,
                       inbound_at: str, package_status: str, location_type: str,
                       location_org_id: str, location_name: str, responsible_party: str,
                       rule_id: str, distributor_id: str | None = None,
                       unit_cost: float | None = None, parent_batch_id: str | None = None,
                       is_legacy: bool = True) -> DisposalReceipt:
        body = {"actor_id": actor_id, "batch_id": batch_id, "product_id": product_id,
                "quantity": quantity, "rule_id": rule_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch_id = self._identifier(batch_id, "batch_id")
            product_id = self._text(product_id, "product_id", 80)
            product_name = self._text(product_name, "product_name")
            quantity = self._int(quantity, "quantity")
            unit = self._text(unit, "unit", 16)
            produced = self._iso_datetime(produced_at, "produced_at")
            inbound = self._iso_datetime(inbound_at, "inbound_at")
            if produced > inbound:
                raise ValidationError("produced_at 不能晚于 inbound_at")
            if inbound > self.clock.now():
                raise ValidationError("inbound_at 不能晚于当前时间")
            if package_status not in PACKAGE_STATUSES:
                raise ValidationError("package_status 不被支持")
            if location_type not in LOCATION_TYPES:
                raise ValidationError("location_type 不被支持")
            location_org_id = self._identifier(location_org_id, "location_org_id")
            if not self._org_exists(connection, location_org_id):
                raise NotFoundError("库位经营主体不存在")
            location_name = self._text(location_name, "location_name")
            responsible_party = self._text(responsible_party, "responsible_party")
            rule_id = self._identifier(rule_id, "rule_id")
            rule_version = self._latest_rule_version(connection, rule_id)
            if distributor_id is not None:
                distributor_id = self._identifier(distributor_id, "distributor_id")
                if not self._org_exists(connection, distributor_id):
                    raise NotFoundError("经销商主体不存在")
            if parent_batch_id is not None and connection.execute(
                "SELECT 1 FROM disposal_batches WHERE batch_id=?", (parent_batch_id,)
            ).fetchone() is None:
                raise NotFoundError("父批次不存在")
            if unit_cost is not None and (not isinstance(unit_cost, (int, float))
                                          or isinstance(unit_cost, bool) or unit_cost < 0):
                raise ValidationError("unit_cost 必须是非负数字")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO disposal_batches(batch_id,product_id,product_name,quantity,unit,"
                        "unit_cost,produced_at,inbound_at,package_status,location_type,location_org_id,"
                        "location_name,distributor_id,responsible_party,rule_id,rule_version,"
                        "parent_batch_id,is_legacy,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, product_id, product_name, quantity, unit, unit_cost,
                         produced_at, inbound_at, package_status, location_type, location_org_id,
                         location_name, distributor_id, responsible_party, rule_id, rule_version,
                         parent_batch_id, 1 if is_legacy else 0, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号已经存在") from exc
                self._ledger(connection, event_type="registered", batch_id=batch_id, delta=quantity,
                             org_id=location_org_id, actor_id=actor_id)
                self._audit(connection, actor_id=actor_id, action="disposal.batch.registered",
                            resource_type="disposal_batch", resource_id=batch_id,
                            detail={"product_id": product_id, "quantity": quantity,
                                    "rule_id": rule_id, "rule_version": rule_version,
                                    "package_status": package_status,
                                    "location_type": location_type,
                                    "location_org_id": location_org_id,
                                    "parent_batch_id": parent_batch_id})
                return "disposal_batch", batch_id, {"batch_id": batch_id, "rule_version": rule_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_disposal_batch", payload=body, create=create)

    def _age_days(self, connection, inbound_at: str) -> int:
        inbound = datetime.fromisoformat(inbound_at.replace("Z", "+00:00"))
        return max(0, (self.clock.now() - inbound).days)

    def _frozen_qty(self, connection, batch_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM disposal_freezes WHERE batch_id=? AND status='active'",
            (batch_id,),
        ).fetchone()
        return row["q"]

    def _on_hand(self, connection, batch_id: str) -> int:
        """当前账面在场数量 = 保管台账全部事件之和。

        根批次由 registered 事件入账，派生批次由 received/relabel_in 事件入账，
        任何一方都不再叠加批次表的初始数量，避免重复计算。
        """

        row = connection.execute(
            "SELECT COALESCE(SUM(quantity_delta),0) AS d FROM disposal_custody_ledger "
            "WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        return row["d"]

    def _in_transit_qty(self, connection, batch_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM disposal_steps "
            "WHERE batch_id=? AND status='in_transit'",
            (batch_id,),
        ).fetchone()
        return row["q"]

    def _free_qty(self, connection, batch_row, exclude_line_id: str | None = None,
                  release_line_ids: set[str] | None = None,
                  extra_allocations: dict[str, int] | None = None) -> int:
        """未冻结、未被有效计划行（扣除已终结数量）占用的可规划数量。

        exclude_line_id：本行自身（审批复核时）。
        release_line_ids：改版时整个旧版本释放掉的行。
        extra_allocations：同一请求内已通过校验、尚未落库的新行占用量。
        """

        query = (
            "SELECT COALESCE(SUM(l.quantity - l.finalized_qty),0) AS q "
            "FROM disposal_plan_lines l JOIN disposal_plans p "
            "ON p.plan_id=l.plan_id AND p.plan_version=l.plan_version "
            f"WHERE l.batch_id=? AND p.status IN {_ACTIVE_PLAN_STATES}"
        )
        params: list[Any] = [batch_row["batch_id"]]
        excluded = set(release_line_ids or ())
        if exclude_line_id:
            excluded.add(exclude_line_id)
        if excluded:
            query += " AND l.line_id NOT IN (%s)" % ",".join("?" for _ in excluded)
            params.extend(sorted(excluded))
        reserved = connection.execute(query, params).fetchone()["q"]
        allocated = (extra_allocations or {}).get(batch_row["batch_id"], 0)
        return self._on_hand(connection, batch_row["batch_id"]) \
            - self._frozen_qty(connection, batch_row["batch_id"]) - reserved - allocated

    def _batch_view(self, connection, row) -> Batch:
        frozen = self._frozen_qty(connection, row["batch_id"])
        in_transit = self._in_transit_qty(connection, row["batch_id"])
        on_hand = self._on_hand(connection, row["batch_id"])
        return Batch(
            row["batch_id"], row["product_id"], row["product_name"], row["quantity"],
            row["unit"], row["unit_cost"], row["produced_at"], row["inbound_at"],
            row["package_status"], row["location_type"], row["location_org_id"],
            row["location_name"], row["distributor_id"], row["responsible_party"],
            row["rule_id"], row["rule_version"], row["parent_batch_id"],
            bool(row["is_legacy"]), row["created_by"], row["created_at"],
            age_days=self._age_days(connection, row["inbound_at"]),
            frozen_qty=frozen, in_transit_qty=in_transit,
            available_qty=on_hand - frozen - in_transit,
        )

    def get_batch(self, batch_id: str) -> Batch:
        row = self.database.connection.execute(
            "SELECT * FROM disposal_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return self._batch_view(self.database.connection, row)

    def list_batches(self, *, product_id: str | None = None,
                     distributor_id: str | None = None) -> list[Batch]:
        query = "SELECT * FROM disposal_batches WHERE 1=1"
        params: list[Any] = []
        if product_id:
            query += " AND product_id=?"
            params.append(product_id)
        if distributor_id:
            query += " AND distributor_id=?"
            params.append(distributor_id)
        query += " ORDER BY inbound_at, batch_id"
        connection = self.database.connection
        return [self._batch_view(connection, row)
                for row in connection.execute(query, params)]

    # ----------------------------------------------------------------- 冻结

    def freeze_batch(self, *, request_id: str, actor_id: str, batch_id: str, quantity: int,
                     reason: str, scope_note: str) -> DisposalReceipt:
        """仅冻结指定数量；可冻结已计划但未发运的货物，不能冻结在途货物。"""

        body = {"actor_id": actor_id, "batch_id": batch_id, "quantity": quantity, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            batch = connection.execute(
                "SELECT * FROM disposal_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("批次不存在")
            quantity = self._int(quantity, "quantity")
            reason = self._text(reason, "reason")
            scope_note = self._text(scope_note, "scope_note")
            present = self._on_hand(connection, batch_id) \
                - self._frozen_qty(connection, batch_id) \
                - self._in_transit_qty(connection, batch_id)
            if quantity > present:
                raise ConflictError(
                    f"冻结数量超过当前在场可冻结数量 {present}（在途货物不受本冻结影响）"
                )

            def create() -> tuple[str, str, dict[str, Any]]:
                freeze_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO disposal_freezes(freeze_id,batch_id,quantity,reason,scope_note,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                    (freeze_id, batch_id, quantity, reason, scope_note, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="disposal.freeze.created",
                            resource_type="disposal_freeze", resource_id=freeze_id,
                            detail={"batch_id": batch_id, "quantity": quantity,
                                    "reason": reason, "scope_note": scope_note})
                return "disposal_freeze", freeze_id, {"freeze_id": freeze_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_disposal_batch", payload=body, create=create)

    def release_freeze(self, *, request_id: str, actor_id: str, freeze_id: str,
                       reason: str) -> DisposalReceipt:
        body = {"actor_id": actor_id, "freeze_id": freeze_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            freeze = connection.execute(
                "SELECT * FROM disposal_freezes WHERE freeze_id=?", (freeze_id,)
            ).fetchone()
            if freeze is None:
                raise NotFoundError("冻结记录不存在")
            if freeze["status"] != "active":
                raise ConflictError("冻结已解除，不能重复解除")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE disposal_freezes SET status='released',released_by=?,released_at=?,"
                    "release_reason=? WHERE freeze_id=?",
                    (actor_id, self._now(), reason, freeze_id),
                )
                self._audit(connection, actor_id=actor_id, action="disposal.freeze.released",
                            resource_type="disposal_freeze", resource_id=freeze_id,
                            detail={"batch_id": freeze["batch_id"], "quantity": freeze["quantity"],
                                    "reason": reason})
                return "disposal_freeze", freeze_id, {"freeze_id": freeze_id, "status": "released"}

            return self._idempotent(connection, request_id=request_id,
                                    action="release_disposal_freeze", payload=body, create=create)

    def list_freezes(self, batch_id: str) -> list[Freeze]:
        rows = self.database.connection.execute(
            "SELECT * FROM disposal_freezes WHERE batch_id=? ORDER BY created_at", (batch_id,)
        )
        return [Freeze(r["freeze_id"], r["batch_id"], r["quantity"], r["reason"], r["scope_note"],
                       r["status"], r["created_by"], r["created_at"], r["released_by"],
                       r["released_at"], r["release_reason"]) for r in rows]

    # ------------------------------------------------------------- 处置计划

    def create_plan(self, *, request_id: str, actor_id: str, plan_id: str, title: str,
                    lines: list[dict[str, Any]]) -> DisposalReceipt:
        body = {"actor_id": actor_id, "plan_id": plan_id, "title": title, "lines": lines}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if not isinstance(lines, list) or not lines:
                raise ValidationError("lines 必须是非空数组")
            plan_id = self._identifier(plan_id, "plan_id")
            title = self._text(title, "title")
            if connection.execute(
                "SELECT 1 FROM disposal_plans WHERE plan_id=?", (plan_id,)
            ).fetchone() is not None:
                raise ConflictError("计划编号已经存在，请使用改版接口")
            normalized: list[dict[str, Any]] = []
            allocations: dict[str, int] = {}
            for index, raw in enumerate(lines):
                item = self._normalize_plan_line(connection, raw, index,
                                                  allocations=allocations)
                normalized.append(item)
                allocations[item["batch_id"]] = allocations.get(item["batch_id"], 0) \
                    + item["quantity"]

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "INSERT INTO disposal_plans(plan_id,plan_version,status,title,created_by,"
                    "created_at) VALUES(?,1,'draft',?,?,?)",
                    (plan_id, title, actor_id, now),
                )
                line_ids = []
                for item in normalized:
                    line_id = uuid.uuid4().hex
                    line_ids.append(line_id)
                    connection.execute(
                        "INSERT INTO disposal_plan_lines(line_id,plan_id,plan_version,origin_line_id,"
                        "batch_id,decision,quantity,unit_price,due_at,target_org_id,"
                        "target_location_name,note,rule_id,rule_version,snapshot_json) "
                        "VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (line_id, plan_id, item["origin_line_id"], item["batch_id"],
                         item["decision"], item["quantity"], item["unit_price"], item["due_at"],
                         item["target_org_id"], item["target_location_name"], item["note"],
                         item["rule_id"], item["rule_version"], canonical_json(item["snapshot"])),
                    )
                self._audit(connection, actor_id=actor_id, action="disposal.plan.created",
                            resource_type="disposal_plan", resource_id=plan_id,
                            detail={"plan_version": 1, "lines": len(normalized)})
                response = {"plan_id": plan_id, "plan_version": 1, "status": "draft",
                            "line_ids": line_ids}
                return "disposal_plan", plan_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="create_disposal_plan", payload=body, create=create)

    def _normalize_plan_line(self, connection, raw: dict[str, Any], index: int,
                             origin_line_id: str | None = None,
                             release_line_ids: set[str] | None = None,
                             allocations: dict[str, int] | None = None) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError(f"lines[{index}] 必须是对象")
        batch_id = self._identifier(raw.get("batch_id"), f"lines[{index}].batch_id")
        batch = connection.execute(
            "SELECT * FROM disposal_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFoundError(f"lines[{index}] 引用的批次不存在")
        decision = str(raw.get("decision", "")).strip()
        if decision not in DECISIONS:
            raise ValidationError(f"lines[{index}].decision 不被支持")
        quantity = self._int(raw.get("quantity"), f"lines[{index}].quantity")
        age_days = self._age_days(connection, batch["inbound_at"])
        self._decision_allowed(connection, decision, batch, age_days)
        free = self._free_qty(connection, batch, exclude_line_id=origin_line_id,
                              release_line_ids=release_line_ids,
                              extra_allocations=allocations)
        if quantity > free:
            raise ConflictError(
                f"批次 {batch_id} 可规划数量不足：申请 {quantity}，可用 {free}"
            )
        unit_price = raw.get("unit_price")
        if unit_price is not None:
            if not isinstance(unit_price, (int, float)) or isinstance(unit_price, bool) \
                    or unit_price < 0:
                raise ValidationError(f"lines[{index}].unit_price 必须是非负数字")
        if decision == "discount" and unit_price is None:
            raise ValidationError(f"lines[{index}] 折价处置必须提供 unit_price")
        if decision in ("sell", "discount", "return"):
            target_org_id = self._identifier(raw.get("target_org_id"),
                                             f"lines[{index}].target_org_id")
            if not self._org_exists(connection, target_org_id):
                raise NotFoundError(f"lines[{index}] 目标主体不存在")
            if target_org_id == batch["location_org_id"]:
                raise ValidationError(f"lines[{index}] 交接目标主体不能与当前所在主体相同")
        else:
            target_org_id = None
        target_location_name = str(raw.get("target_location_name") or "").strip() or None
        due_at = None
        if raw.get("due_at"):
            due = self._iso_datetime(raw.get("due_at"), f"lines[{index}].due_at")
            due_at = due.isoformat().replace("+00:00", "Z")
        note = str(raw.get("note") or "").strip()
        snapshot = {
            "product_id": batch["product_id"],
            "product_name": batch["product_name"],
            "age_days": age_days,
            "package_status": batch["package_status"],
            "location_type": batch["location_type"],
            "location_org_id": batch["location_org_id"],
            "location_name": batch["location_name"],
            "distributor_id": batch["distributor_id"],
            "responsible_party": batch["responsible_party"],
            "unit_cost": batch["unit_cost"],
            "rule_id": batch["rule_id"],
            "rule_version": batch["rule_version"],
        }
        return {"batch_id": batch_id, "decision": decision, "quantity": quantity,
                "unit_price": unit_price, "due_at": due_at, "target_org_id": target_org_id,
                "target_location_name": target_location_name, "note": note[:500],
                "rule_id": batch["rule_id"], "rule_version": batch["rule_version"],
                "snapshot": snapshot, "origin_line_id": origin_line_id}

    def _current_plan(self, connection, plan_id: str):
        row = connection.execute(
            "SELECT * FROM disposal_plans WHERE plan_id=? ORDER BY plan_version DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("处置计划不存在")
        return row

    def approve_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> DisposalReceipt:
        body = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            plan = self._current_plan(connection, plan_id)
            if plan["status"] != "draft":
                raise ConflictError(f"计划当前状态为 {plan['status']}，不能审批")
            if plan["created_by"] == actor_id:
                raise PermissionDenied("审批人与起草人不能为同一操作者")
            plan_id = plan["plan_id"]
            version = plan["plan_version"]
            line_rows = connection.execute(
                "SELECT * FROM disposal_plan_lines WHERE plan_id=? AND plan_version=?",
                (plan_id, version),
            ).fetchall()
            # 审批时再次校验：起草后可能新增了冻结或竞争计划。
            for line in line_rows:
                batch = connection.execute(
                    "SELECT * FROM disposal_batches WHERE batch_id=?", (line["batch_id"],)
                ).fetchone()
                if self._free_qty(connection, batch, exclude_line_id=line["line_id"]) \
                        < line["quantity"]:
                    raise ConflictError(
                        f"批次 {line['batch_id']} 在审批前出现冻结或数量占用，无法审批，请改版"
                    )

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE disposal_plans SET status='approved',approved_by=?,approved_at=? "
                    "WHERE plan_id=? AND plan_version=?",
                    (actor_id, now, plan_id, version),
                )
                steps = []
                for line in line_rows:
                    batch = connection.execute(
                        "SELECT * FROM disposal_batches WHERE batch_id=?", (line["batch_id"],)
                    ).fetchone()
                    step_type = STEP_TYPE_OF[line["decision"]]
                    if step_type in HANDOFF_STEP_TYPES:
                        target_org = line["target_org_id"]
                        target_location = line["target_location_name"] or ""
                    else:
                        target_org = batch["location_org_id"]
                        target_location = batch["location_name"]
                    step_id = uuid.uuid4().hex
                    steps.append(step_id)
                    connection.execute(
                        "INSERT INTO disposal_steps(step_id,plan_id,plan_version,line_id,batch_id,"
                        "sequence,step_type,status,quantity,source_org_id,target_org_id,"
                        "target_location_name,responsible_party,unit_price,due_at,created_at) "
                        "VALUES(?,?,?,?,?,1,?,'pending',?,?,?,?,?,?,?,?)",
                        (step_id, plan_id, version, line["line_id"], line["batch_id"], step_type,
                         line["quantity"], batch["location_org_id"], target_org, target_location,
                         batch["responsible_party"], line["unit_price"], line["due_at"], now),
                    )
                    self._audit(connection, actor_id=actor_id, action="disposal.step.generated",
                                resource_type="disposal_step", resource_id=step_id,
                                detail={"plan_id": plan_id, "plan_version": version,
                                        "line_id": line["line_id"], "batch_id": line["batch_id"],
                                        "step_type": step_type, "quantity": line["quantity"]})
                self._audit(connection, actor_id=actor_id, action="disposal.plan.approved",
                            resource_type="disposal_plan", resource_id=plan_id,
                            detail={"plan_version": version, "steps": len(steps)})
                response = {"plan_id": plan_id, "plan_version": version, "status": "approved",
                            "step_ids": steps}
                return "disposal_plan", plan_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_disposal_plan", payload=body, create=create)

    def revise_plan(self, *, request_id: str, actor_id: str, plan_id: str, reason: str,
                    lines: list[dict[str, Any]]) -> DisposalReceipt:
        """计划改版：存在在途或已终结事实时拒绝；旧版本整体作废、步骤作废。"""

        body = {"actor_id": actor_id, "plan_id": plan_id, "reason": reason, "lines": lines}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if not isinstance(lines, list) or not lines:
                raise ValidationError("lines 必须是非空数组")
            plan = self._current_plan(connection, plan_id)
            plan_id = plan["plan_id"]
            old_version = plan["plan_version"]
            if plan["status"] == "completed":
                raise ConflictError("计划已全部完成，不能改版")
            if plan["status"] == "superseded":
                raise ConflictError("该版本已作废")
            blocking = connection.execute(
                "SELECT step_id,status FROM disposal_steps WHERE plan_id=? AND plan_version=? "
                "AND status IN ('in_transit','completed')",
                (plan_id, old_version),
            ).fetchall()
            if blocking:
                statuses = ", ".join(f"{r['step_id']}={r['status']}" for r in blocking)
                raise ConflictError(f"存在在途或已终结步骤，普通改版不能撤销：{statuses}")
            reason = self._text(reason, "reason")

            old_line_rows = connection.execute(
                "SELECT * FROM disposal_plan_lines WHERE plan_id=? AND plan_version=?",
                (plan_id, old_version),
            ).fetchall()
            old_lines = {r["line_id"]: r for r in old_line_rows}
            # 改版整体作废旧版本：旧版本的全部行占用在校验时先行释放。
            release_line_ids = set(old_lines)
            normalized: list[dict[str, Any]] = []
            allocations: dict[str, int] = {}
            for index, raw in enumerate(lines):
                origin = str(raw.get("origin_line_id") or "").strip() or None
                if origin is not None and origin not in old_lines:
                    raise ValidationError(f"lines[{index}].origin_line_id 不属于当前计划版本")
                item = self._normalize_plan_line(
                    connection, raw, index, origin,
                    release_line_ids=release_line_ids, allocations=allocations)
                normalized.append(item)
                allocations[item["batch_id"]] = allocations.get(item["batch_id"], 0) \
                    + item["quantity"]

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                new_version = old_version + 1
                connection.execute(
                    "UPDATE disposal_plans SET status='superseded',superseded_at=? "
                    "WHERE plan_id=? AND plan_version=?",
                    (now, plan_id, old_version),
                )
                connection.execute(
                    "UPDATE disposal_steps SET status='superseded' WHERE plan_id=? "
                    "AND plan_version=? AND status='pending'",
                    (plan_id, old_version),
                )
                connection.execute(
                    "INSERT INTO disposal_plans(plan_id,plan_version,status,title,created_by,"
                    "created_at,revision_reason) VALUES(?,?,'draft',?,?,?,?)",
                    (plan_id, new_version, plan["title"], actor_id, now, reason),
                )
                line_ids = []
                for item in normalized:
                    line_id = uuid.uuid4().hex
                    line_ids.append(line_id)
                    connection.execute(
                        "INSERT INTO disposal_plan_lines(line_id,plan_id,plan_version,origin_line_id,"
                        "batch_id,decision,quantity,unit_price,due_at,target_org_id,"
                        "target_location_name,note,rule_id,rule_version,snapshot_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (line_id, plan_id, new_version, item["origin_line_id"], item["batch_id"],
                         item["decision"], item["quantity"], item["unit_price"], item["due_at"],
                         item["target_org_id"], item["target_location_name"], item["note"],
                         item["rule_id"], item["rule_version"], canonical_json(item["snapshot"])),
                    )
                self._audit(connection, actor_id=actor_id, action="disposal.plan.revised",
                            resource_type="disposal_plan", resource_id=plan_id,
                            detail={"old_version": old_version, "new_version": new_version,
                                    "reason": reason, "lines": len(normalized)})
                response = {"plan_id": plan_id, "plan_version": new_version, "status": "draft",
                            "superseded_version": old_version, "line_ids": line_ids}
                return "disposal_plan", plan_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="revise_disposal_plan", payload=body, create=create)

    def _plan_detail(self, connection, plan) -> dict[str, Any]:
        plan_id, version = plan["plan_id"], plan["plan_version"]
        lines = []
        for row in connection.execute(
            "SELECT * FROM disposal_plan_lines WHERE plan_id=? AND plan_version=?",
            (plan_id, version),
        ):
            lines.append({
                "line_id": row["line_id"], "origin_line_id": row["origin_line_id"],
                "batch_id": row["batch_id"], "decision": row["decision"],
                "quantity": row["quantity"], "unit_price": row["unit_price"],
                "due_at": row["due_at"], "target_org_id": row["target_org_id"],
                "target_location_name": row["target_location_name"], "note": row["note"],
                "rule_id": row["rule_id"], "rule_version": row["rule_version"],
                "snapshot": json.loads(row["snapshot_json"]),
                "finalized_qty": row["finalized_qty"],
            })
        steps = [self._step_dict(row) for row in connection.execute(
            "SELECT * FROM disposal_steps WHERE plan_id=? AND plan_version=? ORDER BY sequence,step_id",
            (plan_id, version),
        )]
        return {"plan_id": plan_id, "plan_version": version, "status": plan["status"],
                "title": plan["title"], "created_by": plan["created_by"],
                "created_at": plan["created_at"], "approved_by": plan["approved_by"],
                "approved_at": plan["approved_at"], "revision_reason": plan["revision_reason"],
                "lines": lines, "steps": steps}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            plan = self._current_plan(connection, plan_id)
            return self._plan_detail(connection, plan)

    # ------------------------------------------------------------- 执行步骤

    def _step_dict(self, row) -> dict[str, Any]:
        return {
            "step_id": row["step_id"], "plan_id": row["plan_id"],
            "plan_version": row["plan_version"], "line_id": row["line_id"],
            "batch_id": row["batch_id"], "sequence": row["sequence"],
            "step_type": row["step_type"], "status": row["status"],
            "quantity": row["quantity"], "source_org_id": row["source_org_id"],
            "target_org_id": row["target_org_id"],
            "target_location_name": row["target_location_name"],
            "responsible_party": row["responsible_party"], "unit_price": row["unit_price"],
            "due_at": row["due_at"], "output_batch_id": row["output_batch_id"],
            "evidence": json.loads(row["evidence_json"]),
            "completed_by": row["completed_by"], "completed_at": row["completed_at"],
            "created_at": row["created_at"],
        }

    def get_step(self, step_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM disposal_steps WHERE step_id=?", (step_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("步骤不存在")
        return self._step_dict(row)

    def _find_scan(self, connection, scan_code: str):
        return connection.execute(
            "SELECT * FROM disposal_handoff_confirmations WHERE scan_code=?", (scan_code,)
        ).fetchone()

    def _duplicate_response(self, *, step_id: str, scan_code: str, status: str,
                            output_batch_id: str | None) -> dict[str, Any]:
        return {"step_id": step_id, "status": status, "duplicate_scan": True,
                "scan_code": scan_code, "result": "ignored",
                "output_batch_id": output_batch_id}

    def confirm_handoff(self, *, request_id: str, actor_id: str, step_id: str,
                        party_role: str, scan_code: str | None = None,
                        note: str | None = None) -> DisposalReceipt:
        """跨主体交接确认：dispatch 发货、receipt 收货，双方齐备且数量一致才终结。"""

        body = {"actor_id": actor_id, "step_id": step_id, "party_role": party_role,
                "scan_code": scan_code}
        with self.database.transaction(immediate=True) as connection:
            replayed = self._replay_if_seen(connection, request_id,
                                            "confirm_disposal_handoff", body)
            if replayed is not None:
                return replayed
            actor = self._actor(connection, actor_id)
            if party_role not in ("dispatch", "receipt"):
                raise ValidationError("party_role 必须是 dispatch 或 receipt")
            step = connection.execute(
                "SELECT * FROM disposal_steps WHERE step_id=?", (step_id,)
            ).fetchone()
            if step is None:
                raise NotFoundError("步骤不存在")
            if step["step_type"] not in HANDOFF_STEP_TYPES:
                raise ValidationError("该步骤不是跨主体交接步骤")
            plan = connection.execute(
                "SELECT * FROM disposal_plans WHERE plan_id=? AND plan_version=?",
                (step["plan_id"], step["plan_version"]),
            ).fetchone()
            scan_code = self._text(scan_code, "scan_code", 80) if scan_code else None
            note = str(note or "").strip()[:500]

            # 重复扫描：同一扫描码再次上报到同一步骤同一角色，确定性忽略，不产生任何变动。
            if scan_code:
                scanned = self._find_scan(connection, scan_code)
                if scanned is not None:
                    if scanned["step_id"] == step_id and scanned["party_role"] == party_role:
                        response = self._duplicate_response(
                            step_id=step_id, scan_code=scan_code, status=step["status"],
                            output_batch_id=step["output_batch_id"])
                        request_id = self._identifier(request_id, "request_id")
                        connection.execute(
                            "INSERT OR IGNORE INTO disposal_receipts(request_id,action,"
                            "payload_hash,resource_type,resource_id,response_json,created_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (request_id, "confirm_disposal_handoff", digest(body),
                             "disposal_step", step_id, canonical_json(response), self._now()),
                        )
                        return DisposalReceipt(request_id, "disposal_step", step_id, False, response)
                    raise ConflictError("scan_code 已用于其他交接动作，疑似重复扫描")

            if plan["status"] == "superseded" or step["status"] == "superseded":
                # 晚到回执落在从未发运即被改版作废的步骤上：确定性拒绝。
                raise ConflictError(
                    f"步骤所属计划已改版作废（当前版本请重新审批），晚到回执不予接受："
                    f"{step['plan_id']} v{step['plan_version']}"
                )
            if party_role == "dispatch":
                self._same_org_or_admin(actor, step["source_org_id"])
                if step["status"] == "in_transit":
                    raise ConflictError("货物已发运，不能重复发货确认")
                if step["status"] == "completed":
                    raise ConflictError("交接已完成，不能重复发货确认")
                batch = connection.execute(
                    "SELECT * FROM disposal_batches WHERE batch_id=?", (step["batch_id"],)
                ).fetchone()
                free = self._free_qty(connection, batch, exclude_line_id=step["line_id"])
                if free < step["quantity"]:
                    raise ConflictError("批次存在新增冻结或数量占用，不能发运；请先走计划改版")
            else:
                self._same_org_or_admin(actor, step["target_org_id"])
                if step["status"] == "pending":
                    # 早到/晚到但系统内无发货事实的回执：不予记账。
                    raise ConflictError("尚未记录发货确认，回执不能先行入账")
                if step["status"] == "completed":
                    raise ConflictError("交接已完成，不能重复收货确认")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "INSERT INTO disposal_handoff_confirmations(step_id,party_role,actor_id,org_id,"
                    "scan_code,note,confirmed_at) VALUES(?,?,?,?,?,?,?)",
                    (step_id, party_role, actor_id, actor.organization_id, scan_code, note, now),
                )
                response: dict[str, Any] = {"step_id": step_id, "party_role": party_role}
                if party_role == "dispatch":
                    connection.execute(
                        "UPDATE disposal_steps SET status='in_transit' WHERE step_id=?", (step_id,)
                    )
                    response.update(status="in_transit", result="accepted")
                    self._audit(connection, actor_id=actor_id,
                                action="disposal.handoff.dispatched",
                                resource_type="disposal_step", resource_id=step_id,
                                detail={"plan_id": step["plan_id"],
                                        "plan_version": step["plan_version"],
                                        "batch_id": step["batch_id"],
                                        "quantity": step["quantity"], "scan_code": scan_code})
                else:
                    output_batch_id = self._settle_handoff(connection, actor, step, now, note,
                                                           scan_code)
                    response.update(status="completed", result="completed",
                                    output_batch_id=output_batch_id)
                return "disposal_step", step_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_disposal_handoff", payload=body, create=create)

    def _settle_handoff(self, connection, actor: Actor, step, now: str, note: str,
                        scan_code: str | None) -> str:
        """收货入账：源批次扣减与新保管批次增加在同一事务内守恒完成。"""

        qty = step["quantity"]
        source = connection.execute(
            "SELECT * FROM disposal_batches WHERE batch_id=?", (step["batch_id"],)
        ).fetchone()
        target_location_type = TARGET_LOCATION_OF[step["step_type"]]
        output_batch_id = uuid.uuid4().hex
        distributor_id = step["target_org_id"] if target_location_type == "channel" else None
        connection.execute(
            "INSERT INTO disposal_batches(batch_id,product_id,product_name,quantity,unit,unit_cost,"
            "produced_at,inbound_at,package_status,location_type,location_org_id,location_name,"
            "distributor_id,responsible_party,rule_id,rule_version,parent_batch_id,is_legacy,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (output_batch_id, source["product_id"], source["product_name"], qty, source["unit"],
             source["unit_cost"], source["produced_at"], now, source["package_status"],
             target_location_type, step["target_org_id"],
             step["target_location_name"] or source["location_name"], distributor_id,
             step["target_org_id"], source["rule_id"],
             self._latest_rule_version(connection, source["rule_id"]),
             source["batch_id"], 1, actor.actor_id, now),
        )
        event_type = {
            "pickup": "picked_up",
            "discount": "discounted",
            "return_to_warehouse": "returned",
        }[step["step_type"]]
        self._ledger(connection, event_type=event_type, batch_id=source["batch_id"], delta=-qty,
                     org_id=source["location_org_id"], actor_id=actor.actor_id,
                     step_id=step["step_id"], plan_id=step["plan_id"],
                     plan_version=step["plan_version"],
                     evidence={"counterparty_batch_id": output_batch_id, "scan_code": scan_code})
        self._ledger(connection, event_type="received", batch_id=output_batch_id, delta=qty,
                     org_id=step["target_org_id"], actor_id=actor.actor_id,
                     step_id=step["step_id"], plan_id=step["plan_id"],
                     plan_version=step["plan_version"],
                     evidence={"counterparty_batch_id": source["batch_id"], "scan_code": scan_code})
        connection.execute(
            "UPDATE disposal_steps SET status='completed',completed_by=?,completed_at=?,"
            "output_batch_id=?,evidence_json=? WHERE step_id=?",
            (actor.actor_id, now, output_batch_id,
             canonical_json({"handoff_note": note, "receipt_scan_code": scan_code}), step["step_id"]),
        )
        connection.execute(
            "UPDATE disposal_plan_lines SET finalized_qty=finalized_qty+? WHERE line_id=?",
            (qty, step["line_id"]),
        )
        self._audit(connection, actor_id=actor.actor_id, action="disposal.handoff.received",
                    resource_type="disposal_step", resource_id=step["step_id"],
                    detail={"plan_id": step["plan_id"], "plan_version": step["plan_version"],
                            "source_batch_id": source["batch_id"],
                            "output_batch_id": output_batch_id, "quantity": qty,
                            "scan_code": scan_code})
        self._maybe_complete_plan(connection, step["plan_id"], step["plan_version"], actor.actor_id)
        return output_batch_id

    def execute_step(self, *, request_id: str, actor_id: str, step_id: str,
                     evidence: dict[str, Any] | None = None) -> DisposalReceipt:
        """执行本方内部步骤：换包或销毁。销毁必须提交凭证，事实不可逆。"""

        evidence = evidence or {}
        if not isinstance(evidence, dict):
            raise ValidationError("evidence 必须是对象")
        body = {"actor_id": actor_id, "step_id": step_id, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            step = connection.execute(
                "SELECT * FROM disposal_steps WHERE step_id=?", (step_id,)
            ).fetchone()
            if step is None:
                raise NotFoundError("步骤不存在")
            if step["step_type"] not in INTERNAL_STEP_TYPES:
                raise ValidationError("该步骤必须走跨主体交接确认")
            plan = connection.execute(
                "SELECT * FROM disposal_plans WHERE plan_id=? AND plan_version=?",
                (step["plan_id"], step["plan_version"]),
            ).fetchone()
            if plan["status"] == "superseded" or step["status"] == "superseded":
                raise ConflictError("步骤已随计划改版作废")
            if step["status"] != "pending":
                raise ConflictError(f"步骤状态为 {step['status']}，不能重复执行")
            self._same_org_or_admin(actor, step["source_org_id"])
            # 审批后可能新增范围冻结：被冻结数量不得换包或销毁，必须先解冻或走改版。
            source = connection.execute(
                "SELECT * FROM disposal_batches WHERE batch_id=?", (step["batch_id"],)
            ).fetchone()
            free = self._free_qty(connection, source, exclude_line_id=step["line_id"])
            if free < step["quantity"]:
                raise ConflictError("批次存在新增冻结或数量占用，不能执行；请先解除冻结或走计划改版")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                qty = step["quantity"]
                if step["step_type"] == "destroy":
                    certificate = self._text(evidence.get("certificate_no"),
                                             "evidence.certificate_no", 80)
                    witness = self._text(evidence.get("witness"), "evidence.witness", 80)
                    method = self._text(evidence.get("method"), "evidence.method", 80)
                    final_evidence = {"certificate_no": certificate, "witness": witness,
                                      "method": method}
                    self._ledger(connection, event_type="destroyed", batch_id=source["batch_id"],
                                 delta=-qty, org_id=source["location_org_id"],
                                 actor_id=actor.actor_id, step_id=step_id,
                                 plan_id=step["plan_id"], plan_version=step["plan_version"],
                                 evidence=final_evidence)
                    self._audit(connection, actor_id=actor.actor_id,
                                action="disposal.step.destroyed",
                                resource_type="disposal_step", resource_id=step_id,
                                detail={"plan_id": step["plan_id"],
                                        "plan_version": step["plan_version"],
                                        "batch_id": source["batch_id"], "quantity": qty,
                                        **final_evidence})
                else:
                    output_batch_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO disposal_batches(batch_id,product_id,product_name,quantity,unit,"
                        "unit_cost,produced_at,inbound_at,package_status,location_type,location_org_id,"
                        "location_name,distributor_id,responsible_party,rule_id,rule_version,"
                        "parent_batch_id,is_legacy,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                        (output_batch_id, source["product_id"], source["product_name"], qty,
                         source["unit"], source["unit_cost"], source["produced_at"], now, "intact",
                         source["location_type"], source["location_org_id"], source["location_name"],
                         source["distributor_id"], source["responsible_party"], source["rule_id"],
                         self._latest_rule_version(connection, source["rule_id"]),
                         source["batch_id"], actor.actor_id, now),
                    )
                    self._ledger(connection, event_type="relabel_out",
                                 batch_id=source["batch_id"], delta=-qty,
                                 org_id=source["location_org_id"], actor_id=actor.actor_id,
                                 step_id=step_id, plan_id=step["plan_id"],
                                 plan_version=step["plan_version"],
                                 evidence={"counterparty_batch_id": output_batch_id})
                    self._ledger(connection, event_type="relabel_in",
                                 batch_id=output_batch_id, delta=qty,
                                 org_id=source["location_org_id"], actor_id=actor.actor_id,
                                 step_id=step_id, plan_id=step["plan_id"],
                                 plan_version=step["plan_version"],
                                 evidence={"counterparty_batch_id": source["batch_id"]})
                    final_evidence = {"relabel_note": str(evidence.get("note") or "")[:500]}
                    self._audit(connection, actor_id=actor.actor_id,
                                action="disposal.step.relabeled",
                                resource_type="disposal_step", resource_id=step_id,
                                detail={"plan_id": step["plan_id"],
                                        "plan_version": step["plan_version"],
                                        "source_batch_id": source["batch_id"],
                                        "output_batch_id": output_batch_id, "quantity": qty})
                output = output_batch_id if step["step_type"] == "relabel" else None
                connection.execute(
                    "UPDATE disposal_steps SET status='completed',completed_by=?,completed_at=?,"
                    "output_batch_id=?,evidence_json=? WHERE step_id=?",
                    (actor.actor_id, now, output, canonical_json(final_evidence), step_id),
                )
                connection.execute(
                    "UPDATE disposal_plan_lines SET finalized_qty=finalized_qty+? WHERE line_id=?",
                    (qty, step["line_id"]),
                )
                self._maybe_complete_plan(connection, step["plan_id"], step["plan_version"],
                                          actor.actor_id)
                response = {"step_id": step_id, "status": "completed",
                            "output_batch_id": output, "result": "completed"}
                return "disposal_step", step_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="execute_disposal_step", payload=body, create=create)

    def _maybe_complete_plan(self, connection, plan_id: str, version: int, actor_id: str) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM disposal_steps WHERE plan_id=? AND plan_version=? "
            "AND status<>'completed'",
            (plan_id, version),
        ).fetchone()
        if row["c"] == 0:
            connection.execute(
                "UPDATE disposal_plans SET status='completed' WHERE plan_id=? AND plan_version=?",
                (plan_id, version),
            )
            self._audit(connection, actor_id=actor_id, action="disposal.plan.completed",
                        resource_type="disposal_plan", resource_id=plan_id,
                        detail={"plan_version": version})

    def record_refund(self, *, request_id: str, actor_id: str, step_id: str, amount: float,
                      reason: str, evidence: dict[str, Any] | None = None) -> DisposalReceipt:
        """对已完成销售类步骤记录退款；该事实只追加，任何计划改版都无法撤销。"""

        evidence = evidence or {}
        body = {"actor_id": actor_id, "step_id": step_id, "amount": amount, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            step = connection.execute(
                "SELECT * FROM disposal_steps WHERE step_id=?", (step_id,)
            ).fetchone()
            if step is None:
                raise NotFoundError("步骤不存在")
            if step["status"] != "completed":
                raise ConflictError("只能对已完成的步骤登记退款")
            if step["step_type"] not in ("discount", "pickup"):
                raise ValidationError("只有提货或折价销售步骤可以登记退款")
            if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
                raise ValidationError("amount 必须是正数")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                data = json.loads(step["evidence_json"])
                refunds = list(data.get("refunds", []))
                record = {"amount": float(amount), "reason": reason, "by": actor.actor_id,
                          "at": self._now(), "evidence": evidence}
                refunds.append(record)
                data["refunds"] = refunds
                connection.execute(
                    "UPDATE disposal_steps SET evidence_json=? WHERE step_id=?",
                    (canonical_json(data), step_id),
                )
                self._ledger(connection, event_type="refunded", batch_id=step["batch_id"],
                             delta=0, org_id=step["source_org_id"], actor_id=actor.actor_id,
                             step_id=step_id, plan_id=step["plan_id"],
                             plan_version=step["plan_version"],
                             evidence={"amount": float(amount), "reason": reason})
                self._audit(connection, actor_id=actor.actor_id,
                            action="disposal.refund.recorded",
                            resource_type="disposal_step", resource_id=step_id,
                            detail={"plan_id": step["plan_id"],
                                    "plan_version": step["plan_version"],
                                    "amount": float(amount), "reason": reason})
                return "disposal_step", step_id, {"step_id": step_id, "refund_recorded": True,
                                                  "refund_count": len(refunds)}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_disposal_refund", payload=body, create=create)

    # ------------------------------------------------------------- 管理查询

    def open_actions(self, *, product_id: str | None = None, distributor_id: str | None = None,
                     overdue_only: bool = False) -> dict[str, Any]:
        """按产品或经销商查看未完成动作与责任逾期。"""

        connection = self.database.connection
        now = self._now()
        query = (
            "SELECT s.*, b.product_id, b.product_name, b.distributor_id "
            "FROM disposal_steps s JOIN disposal_batches b ON b.batch_id=s.batch_id "
            "JOIN disposal_plans p ON p.plan_id=s.plan_id AND p.plan_version=s.plan_version "
            "WHERE s.status IN ('pending','in_transit')"
        )
        params: list[Any] = []
        if product_id:
            query += " AND b.product_id=?"
            params.append(product_id)
        if distributor_id:
            query += " AND (b.distributor_id=? OR s.target_org_id=?)"
            params.extend([distributor_id, distributor_id])
        items: list[dict[str, Any]] = []
        overdue_ids: list[str] = []
        for row in connection.execute(query + " ORDER BY s.due_at, s.created_at", params):
            item = self._step_dict(row)
            item["product_id"] = row["product_id"]
            item["product_name"] = row["product_name"]
            overdue = bool(item["due_at"]) and item["due_at"] < now
            item["overdue"] = overdue
            if overdue:
                item["overdue_days"] = max(
                    1, (self.clock.now() - self._iso_datetime(item["due_at"], "due_at")).days)
                overdue_ids.append(item["step_id"])
            if not overdue_only or overdue:
                items.append(item)
        # 待审批的草案计划同样属于未完成动作，并随产品/经销商条件过滤。
        draft_query = (
            "SELECT p.* FROM disposal_plans p WHERE p.status='draft' AND p.plan_version=("
            "SELECT MAX(plan_version) FROM disposal_plans q WHERE q.plan_id=p.plan_id)"
        )
        draft_params: list[Any] = []
        if product_id or distributor_id:
            draft_query += (
                " AND EXISTS (SELECT 1 FROM disposal_plan_lines l "
                "JOIN disposal_batches b ON b.batch_id=l.batch_id "
                "WHERE l.plan_id=p.plan_id AND l.plan_version=p.plan_version"
            )
            conditions = []
            if product_id:
                conditions.append("b.product_id=?")
                draft_params.append(product_id)
            if distributor_id:
                conditions.append("(b.distributor_id=? OR l.target_org_id=?)")
                draft_params.extend([distributor_id, distributor_id])
            draft_query += " AND (" + " OR ".join(conditions) + "))"
        pending_approvals = [{"plan_id": r["plan_id"], "plan_version": r["plan_version"],
                              "title": r["title"], "created_by": r["created_by"],
                              "created_at": r["created_at"]}
                             for r in connection.execute(draft_query, draft_params)]
        return {"items": items, "count": len(items), "overdue_count": len(overdue_ids),
                "pending_approvals": pending_approvals}

    def profit_loss_basis(self, *, product_id: str | None = None,
                          distributor_id: str | None = None) -> dict[str, Any]:
        """汇总已终结步骤的损益依据：销毁成本、销售回款、退款。"""

        connection = self.database.connection
        query = (
            "SELECT s.*, b.product_id, b.product_name, b.unit_cost AS batch_cost, "
            "b.distributor_id FROM disposal_steps s "
            "JOIN disposal_batches b ON b.batch_id=s.batch_id WHERE s.status='completed'"
        )
        params: list[Any] = []
        if product_id:
            query += " AND b.product_id=?"
            params.append(product_id)
        if distributor_id:
            query += " AND (b.distributor_id=? OR s.target_org_id=?)"
            params.extend([distributor_id, distributor_id])
        rows_out: list[dict[str, Any]] = []
        totals = {"cost_amount": 0.0, "revenue_amount": 0.0, "refund_amount": 0.0,
                  "destroyed_quantity": 0, "sold_quantity": 0}
        for row in connection.execute(query + " ORDER BY s.completed_at", params):
            unit_cost = row["batch_cost"] or 0.0
            qty = row["quantity"]
            refunds = json.loads(row["evidence_json"]).get("refunds", [])
            refund_amount = round(sum(float(r["amount"]) for r in refunds), 2)
            # 退仓与换包只改变保管位置，不产生损益；销毁计库存损失；销售计收入与成本。
            if row["step_type"] == "destroy":
                cost = round(unit_cost * qty, 2)
                revenue = 0.0
                basis = "destruction_certificate"
            elif row["step_type"] in ("discount", "pickup"):
                cost = round(unit_cost * qty, 2)
                revenue = round((row["unit_price"] or 0.0) * qty, 2)
                basis = "discounted_sale"
            elif row["step_type"] == "return_to_warehouse":
                cost = 0.0
                revenue = 0.0
                basis = "channel_return"
            else:
                cost = 0.0
                revenue = 0.0
                basis = "relabel"
            rows_out.append({
                "step_id": row["step_id"], "plan_id": row["plan_id"],
                "plan_version": row["plan_version"], "product_id": row["product_id"],
                "product_name": row["product_name"], "step_type": row["step_type"],
                "quantity": qty, "unit_cost": unit_cost, "unit_price": row["unit_price"],
                "cost_amount": cost, "revenue_amount": revenue,
                "refund_amount": refund_amount,
                "net_amount": round(revenue - cost - refund_amount, 2),
                "basis": basis, "evidence": json.loads(row["evidence_json"]),
                "completed_at": row["completed_at"],
                "distributor_id": row["distributor_id"],
            })
            totals["cost_amount"] = round(totals["cost_amount"] + cost, 2)
            totals["revenue_amount"] = round(totals["revenue_amount"] + revenue, 2)
            totals["refund_amount"] = round(totals["refund_amount"] + refund_amount, 2)
            if row["step_type"] == "destroy":
                totals["destroyed_quantity"] += qty
            if row["step_type"] in ("discount", "pickup"):
                totals["sold_quantity"] += qty
        totals["net_amount"] = round(
            totals["revenue_amount"] - totals["cost_amount"] - totals["refund_amount"], 2)
        return {"items": rows_out, "totals": totals}

    def custody_chain(self, batch_id: str) -> dict[str, Any]:
        """返回批次自入库起的完整保管链：血缘、台账、交接、冻结。"""

        connection = self.database.connection
        batch = connection.execute(
            "SELECT * FROM disposal_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFoundError("批次不存在")
        # 沿 parent_batch_id 上溯到根批次。
        lineage = [self._batch_view(connection, batch)]
        current = batch
        while current["parent_batch_id"]:
            parent = connection.execute(
                "SELECT * FROM disposal_batches WHERE batch_id=?",
                (current["parent_batch_id"],),
            ).fetchone()
            if parent is None:
                break
            lineage.append(self._batch_view(connection, parent))
            current = parent
        lineage.reverse()
        lineage_ids = [item.batch_id for item in lineage]
        events = []
        for row in connection.execute(
            "SELECT * FROM disposal_custody_ledger WHERE batch_id IN (%s) "
            "ORDER BY created_at, ledger_id" % ",".join("?" for _ in lineage_ids),
            lineage_ids,
        ):
            events.append({"kind": "ledger", "event_type": row["event_type"],
                           "batch_id": row["batch_id"], "quantity_delta": row["quantity_delta"],
                           "step_id": row["step_id"], "org_id": row["org_id"],
                           "actor_id": row["actor_id"], "evidence": json.loads(row["evidence_json"]),
                           "at": row["created_at"]})
        step_ids = [e["step_id"] for e in events if e["step_id"]]
        if step_ids:
            placeholders = ",".join("?" for _ in step_ids)
            for row in connection.execute(
                f"SELECT * FROM disposal_handoff_confirmations WHERE step_id IN ({placeholders}) "
                "ORDER BY confirmed_at", step_ids,
            ):
                events.append({"kind": "handoff_confirmation", "step_id": row["step_id"],
                               "party_role": row["party_role"], "actor_id": row["actor_id"],
                               "org_id": row["org_id"], "scan_code": row["scan_code"],
                               "note": row["note"], "at": row["confirmed_at"]})
        for b in lineage_ids:
            for row in connection.execute(
                "SELECT * FROM disposal_freezes WHERE batch_id=? ORDER BY created_at", (b,)
            ):
                events.append({"kind": "freeze", "freeze_id": row["freeze_id"],
                               "batch_id": row["batch_id"], "quantity": row["quantity"],
                               "reason": row["reason"], "scope_note": row["scope_note"],
                               "status": row["status"], "actor_id": row["created_by"],
                               "at": row["created_at"]})
        events.sort(key=lambda item: (item["at"], item["kind"]))
        return {"batch_id": batch_id,
                "lineage": [item.__dict__ for item in lineage],
                "events": events}

    def verify_conservation(self) -> dict[str, Any]:
        """核对每个批次台账不为负、每个步骤出入数量守恒、销毁均有凭证。"""

        connection = self.database.connection
        issues: list[dict[str, Any]] = []
        for batch in connection.execute("SELECT * FROM disposal_batches"):
            balance = connection.execute(
                "SELECT COALESCE(SUM(quantity_delta),0) AS balance FROM disposal_custody_ledger "
                "WHERE batch_id=?",
                (batch["batch_id"],),
            ).fetchone()["balance"]
            if balance < 0:
                issues.append({"type": "negative_balance", "batch_id": batch["batch_id"],
                               "balance": balance})
        for step in connection.execute("SELECT * FROM disposal_steps WHERE status='completed'"):
            ledger = connection.execute(
                "SELECT event_type,quantity_delta,evidence_json FROM disposal_custody_ledger "
                "WHERE step_id=?", (step["step_id"],)
            ).fetchall()
            if step["step_type"] == "destroy":
                destroyed = [r for r in ledger if r["event_type"] == "destroyed"]
                if len(destroyed) != 1 or destroyed[0]["quantity_delta"] != -step["quantity"]:
                    issues.append({"type": "destroy_quantity_mismatch",
                                   "step_id": step["step_id"]})
                if not json.loads(step["evidence_json"]).get("certificate_no"):
                    issues.append({"type": "destroy_without_certificate",
                                   "step_id": step["step_id"]})
            else:
                total = sum(r["quantity_delta"] for r in ledger)
                if total != 0:
                    issues.append({"type": "quantity_not_conserved",
                                   "step_id": step["step_id"], "ledger_sum": total})
                if step["step_type"] in HANDOFF_STEP_TYPES:
                    confirms = connection.execute(
                        "SELECT COUNT(*) AS c FROM disposal_handoff_confirmations WHERE step_id=?",
                        (step["step_id"],)
                    ).fetchone()["c"]
                    if confirms != 2:
                        issues.append({"type": "handoff_not_dual_confirmed",
                                       "step_id": step["step_id"], "confirmations": confirms})
        return {"valid": not issues, "issues": issues,
                "batch_count": connection.execute(
                    "SELECT COUNT(*) AS c FROM disposal_batches").fetchone()["c"],
                "completed_step_count": connection.execute(
                    "SELECT COUNT(*) AS c FROM disposal_steps WHERE status='completed'"
                ).fetchone()["c"]}
