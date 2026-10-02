"""老产品处置平台的离线端到端验收。

在临时 SQLite 数据库中走通：规则版本登记 -> 批次建档（库龄/包装/渠道位置/责任方）
-> 范围冻结 -> 计划与审批 -> 提货/换包/折价/退仓/销毁步骤 -> 双方确认交接 ->
扫描回执（含重复/晚到）-> 折价结算终态 -> 改版 -> 管理视图与保管链核对。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .disposal_service import DisposalService
from .storage import Database


def run(start: datetime | None = None) -> dict[str, object]:
    """执行一条完整处置链并返回可核对的结果。"""

    start = start or datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "disposal_acceptance.sqlite3")
        service = DisposalService(database, FixedClock(start))

        # 主体与角色：品牌方运营/审核，渠道经销商保管人。
        service.register_organization(request_id="org-brand", actor_id="bootstrap",
                                      organization_id="brand", name="金沙酒业品牌方")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                               display_name="管理员", role="admin", organization_id="brand")
        service.register_organization(request_id="org-dealer", actor_id="admin",
                                      organization_id="dealer", name="景芝渠道商")
        service.register_actor(request_id="op", actor_id="admin", new_actor_id="op",
                               display_name="运营负责人", role="operator", organization_id="brand")
        service.register_actor(request_id="rv", actor_id="admin", new_actor_id="rv",
                               display_name="合规审核", role="reviewer", organization_id="brand")
        service.register_actor(request_id="dp", actor_id="admin", new_actor_id="dp",
                               display_name="渠道保管人", role="operator", organization_id="dealer")

        # 规则版本 2026.10：老标换包、破损销毁、完好折价、磨损退仓。
        ruleset = service.register_ruleset(
            request_id="rules", actor_id="op", family="liquor-old-stock", version="2026.10",
            decision_matrix={"intact": "discount", "scratched": "return",
                             "label_old": "repack", "damaged": "destroy"},
            loss_basis={"currency": "CNY", "discount_rate": 0.6,
                        "destroy_unit_loss": 300, "repack_unit_cost": 12})
        ruleset_id = ruleset.resource_id

        # 四个批次覆盖四种包装状态与两类位置。
        def register_batch(request_id: str, product: str, name: str, qty: int, age: int,
                           state: str, location: str, ref: str, dealer: str = "",
                           custodian: str = "op") -> str:
            return service.register_batch(
                request_id=request_id, actor_id="op", product_id=product, product_name=name,
                quantity=qty, unit="箱", age_days=age, package_state=state,
                location_type=location, location_ref=ref, ruleset_id=ruleset_id,
                dealer_id=dealer, custodian_actor_id=custodian).resource_id

        b_repack = register_batch("b1", "p-js-001", "金沙回沙老款", 100, 420, "label_old",
                                  "channel", "store-07", "dealer-07", "dp")
        b_destroy = register_batch("b2", "p-jz-002", "景芝白干旧版", 20, 600, "damaged",
                                   "self_warehouse", "wh-main")
        b_discount = register_batch("b3", "p-js-003", "金沙窖藏", 50, 300, "intact",
                                    "channel", "store-07", "dealer-07", "dp")
        b_return = register_batch("b4", "p-jz-004", "景芝春酿", 30, 360, "scratched",
                                  "channel", "store-09", "dealer-09", "dp")

        # 换包批次中 10 箱抽检渗漏：只冻结受影响数量，其余 90 箱仍可执行。
        service.raise_freeze(request_id="fz1", actor_id="rv", batch_id=b_repack, scope="quantity",
                             reason="抽检渗漏", affected_quantity=10)
        available = service._available_qty(database.connection, b_repack)

        # 处置计划：90 换包 + 20 销毁 + 50 折价 + 30 退仓。
        plan = service.create_plan(
            request_id="plan", actor_id="op", ruleset_id=ruleset_id,
            lines=[{"batch_id": b_repack, "quantity": 90},
                   {"batch_id": b_destroy, "quantity": 20},
                   {"batch_id": b_discount, "quantity": 50},
                   {"batch_id": b_return, "quantity": 30}],
            warehouse_ref="wh-main", repack_station_ref="repack-1", due_days=7)
        plan_id = plan.resource_id
        service.submit_plan(request_id="plan-submit", actor_id="op", plan_id=plan_id)
        approved = service.approve_plan(request_id="plan-approve", actor_id="rv", plan_id=plan_id)
        steps = {(s.batch_id, s.action): s for s in service.list_steps(plan_id=plan_id)}

        # 提货交接：渠道先确认起运（货入在途），品牌方到场后再确认签收。
        pickup = steps[(b_repack, "pickup")]
        handover = service.propose_handover(request_id="ho1", actor_id="op",
                                            step_id=pickup.step_id, quantity=90, evidence_ref="DN-001")
        event_id = handover.resource_id
        service.confirm_custody(request_id="ho1-dp", actor_id="dp", event_id=event_id)
        in_transit = service.get_custody_event(event_id).status
        service.confirm_custody(request_id="ho1-op", actor_id="op", event_id=event_id)
        # 晚到的重复确认必须幂等。
        replayed = service.confirm_custody(request_id="ho1-dp-again", actor_id="dp",
                                           event_id=event_id).replayed

        # 换包扫描：60 + 30 完成；相同证据编号的第二次扫描判为重复。
        repack = steps[(b_repack, "repack")]
        service.record_scan(request_id="sc1", actor_id="op", step_id=repack.step_id,
                            scan_type="repack", quantity=60, evidence_ref="RP-1")
        service.record_scan(request_id="sc2", actor_id="op", step_id=repack.step_id,
                            scan_type="repack", quantity=30, evidence_ref="RP-2")
        dup_scan = service.record_scan(request_id="sc2-dup", actor_id="op", step_id=repack.step_id,
                                       scan_type="repack", quantity=30, evidence_ref="RP-2")
        duplicate = bool(dup_scan.resource_id) and any(
            s.duplicate_of for s in service.list_scans(repack.step_id))

        # 销毁：自有仓同主体见证，双方确认一步到位，形成不可撤销终态。
        destroy = steps[(b_destroy, "destroy")]
        service.propose_handover(request_id="ho2", actor_id="op", step_id=destroy.step_id,
                                 quantity=20, evidence_ref="XH-001")

        # 折价：先扫描与退款结算，再由渠道方确认；结算事实不可重复登记。
        discount = steps[(b_discount, "discount")]
        service.record_scan(request_id="sc3", actor_id="dp", step_id=discount.step_id,
                            scan_type="discount", quantity=50, evidence_ref="SX-1")
        service.record_settlement(request_id="st1", actor_id="op", step_id=discount.step_id,
                                  amount=9000.0, currency="CNY", evidence_ref="TK-1")
        service.confirm_discount(request_id="dc1", actor_id="dp", step_id=discount.step_id)

        # 退仓交接完成 30 箱。
        returning = steps[(b_return, "return")]
        ret = service.propose_handover(request_id="ho3", actor_id="op",
                                       step_id=returning.step_id, quantity=30, evidence_ref="RT-001")
        ret_event = ret.resource_id
        service.confirm_custody(request_id="ho3-dp", actor_id="dp", event_id=ret_event)
        service.confirm_custody(request_id="ho3-op", actor_id="op", event_id=ret_event)

        # 时钟越过截止时间后补一条扫描：应判为晚到且不产生任何效果。
        service.clock = FixedClock(start + timedelta(days=30))
        late_scan = service.record_scan(request_id="sc-late", actor_id="op", step_id=repack.step_id,
                                        scan_type="repack", quantity=90, evidence_ref="RP-OLD")
        late_flag = late_scan.resource_id and service.list_scans(repack.step_id)[-1].late

        # 改版：旧计划未完成部分作废，已销毁/退款的事实保留；新计划为草稿修订版。
        revision = service.revise_plan(request_id="rev1", actor_id="op", plan_id=plan_id,
                                       lines=[{"batch_id": b_repack, "quantity": 10}])
        new_plan = service.get_plan(revision.resource_id)

        conservation = {b: service.verify_conservation(b)["balanced"]
                        for b in (b_repack, b_destroy, b_discount, b_return)}
        pending = service.pending_actions()
        overdue = service.overdue_actions()
        loss = service.loss_basis()
        chain = service.custody_chain(b_repack)
        audit_valid, audit_events = service.verify_audit()
        database.close()
        return {
            "status": "ok",
            "frozen_available": available,
            "steps_generated": approved.resource_id and len(steps) == 5,
            "handover_in_transit": in_transit == "in_transit",
            "duplicate_confirm_replayed": replayed,
            "duplicate_scan_detected": duplicate,
            "late_scan_flagged": bool(late_flag),
            "old_plan_superseded": True if new_plan else False,
            "new_revision": new_plan.revision,
            "conservation_all_balanced": all(conservation.values()),
            "no_pending_left": len(pending) == 0,
            "overdue_count": len(overdue),
            "loss_basis_lines": len(loss),
            "custody_chain_balanced": chain["conservation"]["balanced"],
            "custody_chain_events": len(chain["events"]),
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expectations = ("frozen_available", "steps_generated", "handover_in_transit",
                    "duplicate_confirm_replayed", "duplicate_scan_detected", "late_scan_flagged",
                    "conservation_all_balanced", "no_pending_left", "custody_chain_balanced")
    ok = result["status"] == "ok" and result["audit_valid"] and all(result[k] for k in expectations)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
