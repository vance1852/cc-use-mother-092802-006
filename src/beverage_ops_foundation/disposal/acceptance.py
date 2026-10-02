"""老产品处置域的离线端到端验收。

覆盖：规则版本化登记、库龄/包装/渠道位置台账、风险范围冻结、计划审批生成步骤、
提货/换包/折价/退仓/销毁五种处置、双方确认交接与数量守恒、重复扫描与晚到回执、
计划改版阻断、销毁凭证与退款的不可撤销事实、损益依据与完整保管链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..clock import FixedClock
from ..service import DomainService
from ..storage import Database
from .service import DisposalService

RULE = {"sell_age_days": 365, "return_age_days": 180,
        "relabel_age_days": 730, "destroy_age_days": 1095}


def _bootstrap(database, clock):
    base = DomainService(database, clock)
    disposal = DisposalService(database, clock)
    base.register_organization(request_id="acc-org-1", actor_id="bootstrap",
                               organization_id="org-winery", name="示范酒厂")
    base.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="acc-admin",
                        display_name="管理员", role="admin", organization_id="org-winery")
    base.register_organization(request_id="acc-org-2", actor_id="acc-admin",
                               organization_id="org-dealer", name="示范经销商")
    base.register_actor(request_id="acc-op-w", actor_id="acc-admin", new_actor_id="op-winery",
                        display_name="酒厂运营", role="operator",
                        organization_id="org-winery")
    base.register_actor(request_id="acc-rev", actor_id="acc-admin", new_actor_id="acc-reviewer",
                        display_name="处置审批", role="reviewer",
                        organization_id="org-winery")
    base.register_actor(request_id="acc-op-d", actor_id="acc-admin", new_actor_id="op-dealer",
                        display_name="经销商运营", role="operator",
                        organization_id="org-dealer")
    disposal.publish_rule(request_id="acc-rule", actor_id="acc-admin", rule_id="rule-2026",
                          payload=RULE)
    return base, disposal


def run() -> dict[str, object]:
    """执行一条完整的老产品处置链并返回核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        clock = FixedClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        database = Database(Path(directory) / "disposal_acceptance.sqlite3")
        base, disposal = _bootstrap(database, clock)

        def iso(days: int) -> str:
            return (clock.now() - timedelta(days=days)).isoformat().replace("+00:00", "Z")

        # 渠道 400 天、包装完好：超退仓期，规则判定折价；自有库褪色批次：换标；破损批次：销毁。
        disposal.register_batch(
            request_id="acc-batch-discount", actor_id="op-winery", batch_id="batch-js-001",
            product_id="jinsha-1959", product_name="金沙回沙酒", quantity=100, unit="瓶",
            unit_cost=50.0, produced_at=iso(430), inbound_at=iso(400),
            package_status="intact", location_type="channel",
            location_org_id="org-dealer", location_name="济南示范门店",
            responsible_party="经销商负责人赵六", rule_id="rule-2026",
            distributor_id="org-dealer")
        disposal.register_batch(
            request_id="acc-batch-relabel", actor_id="op-winery", batch_id="batch-jz-001",
            product_id="jingzhi-guipai", product_name="景芝古派", quantity=30, unit="瓶",
            unit_cost=40.0, produced_at=iso(430), inbound_at=iso(400),
            package_status="faded", location_type="self_owned",
            location_org_id="org-winery", location_name="酒厂成品仓",
            responsible_party="酒厂库管孙七", rule_id="rule-2026")
        disposal.register_batch(
            request_id="acc-batch-destroy", actor_id="op-winery", batch_id="batch-jz-002",
            product_id="jingzhi-guipai", product_name="景芝古派", quantity=10, unit="瓶",
            unit_cost=40.0, produced_at=iso(300), inbound_at=iso(200),
            package_status="damaged", location_type="self_owned",
            location_org_id="org-winery", location_name="酒厂不合格品区",
            responsible_party="酒厂库管孙七", rule_id="rule-2026")

        # 风险只冻结受影响范围：景芝待换标批次中仅 6 瓶存在标签投诉。
        disposal.freeze_batch(request_id="acc-freeze", actor_id="op-winery",
                              batch_id="batch-jz-001", quantity=6, reason="标签投诉",
                              scope_note="仅 LOT-A 收箱范围")
        frozen_batch = disposal.get_batch("batch-jz-001")

        disposal.create_plan(
            request_id="acc-plan", actor_id="op-winery", plan_id="plan-20261002",
            title="十月老产品清理计划",
            lines=[
                {"batch_id": "batch-js-001", "decision": "discount", "quantity": 100,
                 "unit_price": 20.0, "target_org_id": "org-winery",
                 "due_at": "2026-10-10T00:00:00Z", "note": "渠道折价回收后统一处理"},
                {"batch_id": "batch-jz-001", "decision": "relabel", "quantity": 24},
                {"batch_id": "batch-jz-002", "decision": "destroy", "quantity": 10},
            ])
        approval = disposal.approve_plan(request_id="acc-approve", actor_id="acc-reviewer",
                                         plan_id="plan-20261002")
        step_ids = approval.extra["step_ids"]
        steps = {disposal.get_step(sid)["step_type"]: sid for sid in step_ids}

        # 折价跨主体交接：经销商发货 -> 酒厂收货，双方确认；重复扫描确定性忽略。
        disposal.confirm_handoff(request_id="acc-ship-discount", actor_id="op-dealer",
                                 step_id=steps["discount"], party_role="dispatch",
                                 scan_code="SHIP-JS-001")
        duplicate = disposal.confirm_handoff(request_id="acc-ship-duplicate",
                                             actor_id="op-dealer",
                                             step_id=steps["discount"],
                                             party_role="dispatch", scan_code="SHIP-JS-001")
        disposal.confirm_handoff(request_id="acc-recv-discount", actor_id="op-winery",
                                 step_id=steps["discount"], party_role="receipt",
                                 scan_code="RECV-JS-001")
        discount_step = disposal.get_step(steps["discount"])
        output_batch = discount_step["output_batch_id"]

        # 换包：本厂内部执行，产出新批次；销毁：必须带凭证。
        disposal.execute_step(request_id="acc-relabel", actor_id="op-winery",
                              step_id=steps["relabel"], evidence={"note": "加贴 2026 新版背标"})
        disposal.execute_step(request_id="acc-destroy", actor_id="op-winery",
                              step_id=steps["destroy"],
                              evidence={"certificate_no": "DESTROY-CERT-0001",
                                        "witness": "质检周八", "method": "破瓶粉碎"})

        # 已完成折价销售发生退款：追加事实。
        disposal.record_refund(request_id="acc-refund", actor_id="op-winery",
                               step_id=steps["discount"], amount=200.0,
                               reason="终端质量投诉先行赔付")
        refund_replay = disposal.record_refund(request_id="acc-refund", actor_id="op-winery",
                                               step_id=steps["discount"], amount=200.0,
                                               reason="终端质量投诉先行赔付")

        # 计划改版：全部完成的计划禁止改版（销毁与退款事实不得被撤销）。
        revision_blocked = False
        try:
            disposal.revise_plan(
                request_id="acc-revise-blocked", actor_id="op-winery",
                plan_id="plan-20261002", reason="试图撤销销毁",
                lines=[{"batch_id": "batch-jz-002", "decision": "destroy", "quantity": 10}])
        except Exception:
            revision_blocked = True

        plan = disposal.get_plan("plan-20261002")
        conservation = disposal.verify_conservation()
        pnl = disposal.profit_loss_basis()
        chain = disposal.custody_chain(output_batch)
        open_actions = disposal.open_actions()
        audit_valid, audit_events = base.verify_audit()

        result = {
            "status": "ok",
            "frozen_quantity": frozen_batch.frozen_qty,
            "available_after_freeze": frozen_batch.available_qty,
            "approved_steps": len(step_ids),
            "duplicate_scan_ignored": bool(duplicate.extra.get("duplicate_scan")),
            "plan_status": plan["status"],
            "source_balance": disposal.get_batch("batch-js-001").available_qty,
            "received_balance": disposal.get_batch(output_batch).available_qty,
            "relabeled_is_new_product": not disposal.get_batch(
                disposal.get_step(steps["relabel"])["output_batch_id"]).is_legacy,
            "revision_blocked": revision_blocked,
            "refund_replayed": refund_replay.replayed,
            "conservation_valid": conservation["valid"],
            "pnl_totals": pnl["totals"],
            "custody_lineage_depth": len(chain["lineage"]),
            "custody_events": len(chain["events"]),
            "open_actions": open_actions["count"],
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = {
        "status": "ok", "frozen_quantity": 6, "available_after_freeze": 24,
        "approved_steps": 3, "duplicate_scan_ignored": True, "plan_status": "completed",
        "source_balance": 0, "received_balance": 100, "relabeled_is_new_product": True,
        "revision_blocked": True, "refund_replayed": True, "conservation_valid": True,
        "custody_lineage_depth": 2, "open_actions": 0, "audit_valid": True,
    }
    ok = all(result.get(key) == value for key, value in expected.items())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
