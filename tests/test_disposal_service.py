import unittest
from datetime import datetime, timedelta, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.disposal import DisposalService
from beverage_ops_foundation.disposal.service import DisposalService as DS
from beverage_ops_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

RULE = {"sell_age_days": 365, "return_age_days": 180,
        "relabel_age_days": 730, "destroy_age_days": 1095}


class DisposalCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.service = DisposalService(self.database, self.clock)
        self.base.register_organization(request_id="org1", actor_id="bootstrap",
                                        organization_id="o1", name="酒厂")
        self.base.register_actor(request_id="admin1", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_organization(request_id="org2", actor_id="a1",
                                        organization_id="o2", name="经销商")
        for rid, aid, name, org, role in [
            ("op1r", "op1", "酒厂运营", "o1", "operator"),
            ("rev1r", "rev1", "审批员", "o1", "reviewer"),
            ("au1r", "au1", "审计员", "o1", "auditor"),
            ("op2r", "op2", "经销运营", "o2", "operator"),
        ]:
            self.base.register_actor(request_id=rid, actor_id="a1", new_actor_id=aid,
                                     display_name=name, role=role, organization_id=org)
        self.service.publish_rule(request_id="rule1", actor_id="a1", rule_id="r1",
                                  payload=RULE)

    def tearDown(self):
        self.database.close()

    def iso(self, days):
        return (self.clock.now() - timedelta(days=days)).isoformat().replace("+00:00", "Z")

    def batch(self, request_id="b1r", batch_id="b1", **overrides):
        values = dict(
            actor_id="op1", batch_id=batch_id, product_id="p1", product_name="金沙酒",
            quantity=100, unit="瓶", unit_cost=50.0, produced_at=self.iso(430),
            inbound_at=self.iso(400), package_status="intact", location_type="channel",
            location_org_id="o2", location_name="济南门店", responsible_party="赵六",
            rule_id="r1", distributor_id="o2",
        )
        values.update(overrides)
        return self.service.register_batch(request_id=request_id, **values)


class RuleTest(DisposalCase):
    def test_rule_versions_are_immutable_and_increment(self):
        self.service.publish_rule(request_id="rule2", actor_id="a1", rule_id="r9",
                                  payload={"note": "v1"})
        self.service.publish_rule(request_id="rule3", actor_id="a1", rule_id="r9",
                                  payload={"note": "v2"})
        self.assertEqual(1, self.service.get_rule("r9", 1).version)
        self.assertEqual("v1", self.service.get_rule("r9", 1).payload["note"])
        self.assertEqual(2, self.service.get_rule("r9").version)

    def test_operator_cannot_publish_rule(self):
        with self.assertRaises(PermissionDenied):
            self.service.publish_rule(request_id="x", actor_id="op1", rule_id="r2",
                                      payload={"note": "x"})

    def test_decision_matrix(self):
        def decide(age, package, location):
            return DS.evaluate_decision(RULE, age_days=age, package_status=package,
                                        location_type=location)["decision"]
        self.assertEqual("destroy", decide(10, "damaged", "self_owned"))
        self.assertEqual("destroy", decide(1200, "intact", "self_owned"))
        self.assertEqual("return", decide(100, "intact", "channel"))
        self.assertEqual("sell", decide(100, "intact", "self_owned"))
        self.assertEqual("relabel", decide(400, "faded", "self_owned"))
        self.assertEqual("discount", decide(400, "intact", "channel"))
        # 渠道褪色标签超过退仓期：不能折价销售不合规标签，兜底销毁。
        self.assertEqual("destroy", decide(400, "faded", "channel"))
        # 自有库超销售期但未到换标上限：临期折价。
        self.assertEqual("discount", decide(400, "intact", "self_owned"))


class BatchTest(DisposalCase):
    def test_batch_computes_age_and_pins_rule_version(self):
        self.batch()
        view = self.service.get_batch("b1")
        self.assertEqual(400, view.age_days)
        self.assertEqual(1, view.rule_version)
        self.assertEqual("o2", view.location_org_id)
        # 规则发布新版本后，老批次仍锁定旧版本。
        self.service.publish_rule(request_id="rule2", actor_id="a1", rule_id="r1",
                                  payload={**RULE, "sell_age_days": 30})
        self.assertEqual(1, self.service.get_batch("b1").rule_version)

    def test_batch_rejects_future_inbound(self):
        with self.assertRaises(ValidationError):
            self.batch(request_id="bad", batch_id="bad",
                       inbound_at=self.iso(-1), produced_at=self.iso(1))

    def test_unknown_rule_rejected(self):
        with self.assertRaises(NotFoundError):
            self.batch(request_id="bad", batch_id="bad", rule_id="missing")

    def test_duplicate_request_replays_receipt(self):
        first = self.batch(request_id="dup")
        second = self.batch(request_id="dup")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)


class FreezeTest(DisposalCase):
    def test_freeze_scopes_only_requested_quantity(self):
        self.batch(quantity=100)
        self.service.freeze_batch(request_id="fz1", actor_id="op1", batch_id="b1",
                                  quantity=10, reason="标签风险", scope_note="仅 LOT-A")
        view = self.service.get_batch("b1")
        self.assertEqual(10, view.frozen_qty)
        self.assertEqual(90, view.available_qty)
        self.service.freeze_batch(request_id="fz2", actor_id="op1", batch_id="b1",
                                  quantity=5, reason="质量风险", scope_note="仅 LOT-B")
        self.assertEqual(15, self.service.get_batch("b1").frozen_qty)

    def test_freeze_cannot_exceed_on_hand(self):
        self.batch(quantity=10)
        with self.assertRaises(ConflictError):
            self.service.freeze_batch(request_id="fz", actor_id="op1", batch_id="b1",
                                      quantity=11, reason="x", scope_note="x")

    def test_freeze_blocks_plan_but_remainder_is_plannable(self):
        self.batch(quantity=100)
        self.service.freeze_batch(request_id="fz", actor_id="op1", batch_id="b1",
                                  quantity=60, reason="标签风险", scope_note="LOT-A")
        with self.assertRaises(ConflictError):
            self.service.create_plan(request_id="p1", actor_id="op1", plan_id="p1",
                                     title="t", lines=[{"batch_id": "b1", "decision": "discount",
                                                        "quantity": 41, "unit_price": 1,
                                                        "target_org_id": "o1"}])
        ok = self.service.create_plan(request_id="p2", actor_id="op1", plan_id="p2",
                                      title="t", lines=[{"batch_id": "b1", "decision": "discount",
                                                        "quantity": 40, "unit_price": 1,
                                                        "target_org_id": "o1"}])
        self.assertFalse(ok.replayed)

    def test_freeze_release_is_idempotent_once(self):
        self.batch(quantity=10)
        receipt = self.service.freeze_batch(request_id="fz", actor_id="op1", batch_id="b1",
                                            quantity=5, reason="x", scope_note="x")
        freeze_id = receipt.resource_id
        self.service.release_freeze(request_id="rl", actor_id="op1", freeze_id=freeze_id,
                                    reason="复检合格")
        with self.assertRaises(ConflictError):
            self.service.release_freeze(request_id="rl2", actor_id="op1",
                                        freeze_id=freeze_id, reason="再次解除")


class PlanTest(DisposalCase):
    def test_plan_line_decision_must_match_rule(self):
        # 渠道 400 天应折价，正常销售会被规则拒绝（销毁兜底除外）。
        self.batch()
        with self.assertRaises(ValidationError):
            self.service.create_plan(request_id="p1", actor_id="op1", plan_id="p1",
                                     title="t", lines=[{"batch_id": "b1", "decision": "sell",
                                                        "quantity": 1, "unit_price": 80,
                                                        "target_org_id": "o1"}])

    def test_discount_requires_unit_price(self):
        self.batch()
        with self.assertRaises(ValidationError):
            self.service.create_plan(request_id="p1", actor_id="op1", plan_id="p1",
                                     title="t", lines=[{"batch_id": "b1", "decision": "discount",
                                                        "quantity": 1,
                                                        "target_org_id": "o1"}])

    def test_approver_must_differ_from_drafter(self):
        self.batch()
        self.service.create_plan(request_id="p1", actor_id="op1", plan_id="p1", title="t",
                                 lines=[{"batch_id": "b1", "decision": "discount",
                                         "quantity": 1, "unit_price": 1,
                                         "target_org_id": "o1"}])
        with self.assertRaises(PermissionDenied):
            self.service.approve_plan(request_id="ap", actor_id="op1", plan_id="p1")

    def test_auditor_cannot_create_plan(self):
        self.batch()
        with self.assertRaises(PermissionDenied):
            self.service.create_plan(request_id="p1", actor_id="au1", plan_id="p1",
                                     title="t", lines=[])

    def test_approve_generates_one_step_per_line(self):
        self.batch()
        self.service.create_plan(request_id="p1", actor_id="op1", plan_id="p1", title="t",
                                 lines=[{"batch_id": "b1", "decision": "discount",
                                         "quantity": 100, "unit_price": 20,
                                         "target_org_id": "o1"}])
        receipt = self.service.approve_plan(request_id="ap", actor_id="rev1", plan_id="p1")
        self.assertEqual(1, len(receipt.extra["step_ids"]))
        detail = self.service.get_plan("p1")
        self.assertEqual("approved", detail["status"])
        self.assertEqual("discount", detail["steps"][0]["step_type"])

    def test_revision_supersedes_pending_steps(self):
        self.batch()
        self.service.create_plan(request_id="p1", actor_id="op1", plan_id="p1", title="t",
                                 lines=[{"batch_id": "b1", "decision": "discount",
                                         "quantity": 100, "unit_price": 20,
                                         "target_org_id": "o1"}])
        self.service.approve_plan(request_id="ap", actor_id="rev1", plan_id="p1")
        old_step = self.service.get_plan("p1")["steps"][0]["step_id"]
        revised = self.service.revise_plan(request_id="rv", actor_id="op1", plan_id="p1",
                                           reason="数量调整",
                                           lines=[{"batch_id": "b1", "decision": "discount",
                                                   "quantity": 50, "unit_price": 18,
                                                   "target_org_id": "o1"}])
        self.assertEqual(2, revised.extra["plan_version"])
        self.assertEqual("superseded", self.service.get_step(old_step)["status"])
        # 新版本仍是草案，重新审批前不生成步骤。
        self.assertEqual(0, len(self.service.get_plan("p1")["steps"]))
        self.service.approve_plan(request_id="ap2", actor_id="rev1", plan_id="p1")
        detail = self.service.get_plan("p1")
        self.assertEqual(2, detail["plan_version"])
        self.assertEqual(1, len(detail["steps"]))


class ExecutionTest(DisposalCase):
    def _approved_discount(self, quantity=100):
        self.batch(quantity=quantity)
        self.service.create_plan(request_id="p1", actor_id="op1", plan_id="p1", title="t",
                                 lines=[{"batch_id": "b1", "decision": "discount",
                                         "quantity": quantity, "unit_price": 20,
                                         "target_org_id": "o1"}])
        self.service.approve_plan(request_id="ap", actor_id="rev1", plan_id="p1")
        return self.service.get_plan("p1")["steps"][0]["step_id"]

    def test_dual_confirmation_conserves_quantity(self):
        step_id = self._approved_discount()
        with self.assertRaises(ConflictError):
            # 收货先行不能入账。
            self.service.confirm_handoff(request_id="early", actor_id="op1", step_id=step_id,
                                         party_role="receipt", scan_code="E")
        self.service.confirm_handoff(request_id="ship", actor_id="op2", step_id=step_id,
                                     party_role="dispatch", scan_code="S1")
        with self.assertRaises(PermissionDenied):
            # 收货必须由目标主体确认。
            self.service.confirm_handoff(request_id="recv-x", actor_id="op2",
                                         step_id=step_id, party_role="receipt", scan_code="S2")
        receipt = self.service.confirm_handoff(request_id="recv", actor_id="op1",
                                               step_id=step_id, party_role="receipt",
                                               scan_code="S2")
        output = receipt.extra["output_batch_id"]
        self.assertEqual(0, self.service.get_batch("b1").available_qty)
        self.assertEqual(100, self.service.get_batch(output).available_qty)
        self.assertEqual("completed", self.service.get_plan("p1")["status"])

    def test_duplicate_scan_ignored_but_reused_code_rejected(self):
        step_id = self._approved_discount()
        self.service.confirm_handoff(request_id="ship", actor_id="op2", step_id=step_id,
                                     party_role="dispatch", scan_code="SCAN-X")
        dup = self.service.confirm_handoff(request_id="ship-dup", actor_id="op2",
                                           step_id=step_id, party_role="dispatch",
                                           scan_code="SCAN-X")
        self.assertTrue(dup.extra["duplicate_scan"])
        with self.assertRaises(ConflictError):
            self.service.confirm_handoff(request_id="ship-other", actor_id="op1",
                                         step_id=step_id, party_role="receipt",
                                         scan_code="SCAN-X")

    def test_late_receipt_after_revision_is_rejected(self):
        step_id = self._approved_discount()
        self.service.revise_plan(request_id="rv", actor_id="op1", plan_id="p1",
                                 reason="暂缓",
                                 lines=[{"batch_id": "b1", "decision": "discount",
                                         "quantity": 100, "unit_price": 20,
                                         "target_org_id": "o1"}])
        with self.assertRaises(ConflictError):
            self.service.confirm_handoff(request_id="late", actor_id="op2",
                                         step_id=step_id, party_role="dispatch",
                                         scan_code="LATE")

    def test_revision_blocked_once_in_transit(self):
        step_id = self._approved_discount()
        self.service.confirm_handoff(request_id="ship", actor_id="op2", step_id=step_id,
                                     party_role="dispatch", scan_code="S1")
        with self.assertRaises(ConflictError):
            self.service.revise_plan(request_id="rv", actor_id="op1", plan_id="p1",
                                     reason="在途改版",
                                     lines=[{"batch_id": "b1", "decision": "discount",
                                             "quantity": 100, "unit_price": 20,
                                             "target_org_id": "o1"}])

    def test_destroy_requires_certificate_and_cannot_revise_after(self):
        self.batch(request_id="bdr", batch_id="bd", quantity=10,
                   package_status="damaged", location_type="self_owned",
                   location_org_id="o1", location_name="不合格品区",
                   distributor_id=None)
        self.service.create_plan(request_id="p2", actor_id="op1", plan_id="p2", title="t",
                                 lines=[{"batch_id": "bd", "decision": "destroy",
                                         "quantity": 10}])
        self.service.approve_plan(request_id="ap2", actor_id="rev1", plan_id="p2")
        step_id = self.service.get_plan("p2")["steps"][0]["step_id"]
        with self.assertRaises(ValidationError):
            self.service.execute_step(request_id="ex", actor_id="op1", step_id=step_id,
                                      evidence={"note": "无凭证"})
        self.service.execute_step(request_id="ex2", actor_id="op1", step_id=step_id,
                                  evidence={"certificate_no": "CERT-1", "witness": "周八",
                                            "method": "粉碎"})
        with self.assertRaises(ConflictError):
            self.service.revise_plan(request_id="rv", actor_id="op1", plan_id="p2",
                                     reason="撤销销毁",
                                     lines=[{"batch_id": "bd", "decision": "destroy",
                                             "quantity": 10}])

    def test_refund_is_append_only_fact(self):
        step_id = self._approved_discount()
        self.service.confirm_handoff(request_id="ship", actor_id="op2", step_id=step_id,
                                     party_role="dispatch", scan_code="S1")
        self.service.confirm_handoff(request_id="recv", actor_id="op1", step_id=step_id,
                                     party_role="receipt", scan_code="S2")
        self.service.record_refund(request_id="rf1", actor_id="op1", step_id=step_id,
                                   amount=100.0, reason="投诉")
        self.service.record_refund(request_id="rf2", actor_id="op1", step_id=step_id,
                                   amount=50.0, reason="二次赔付")
        evidence = self.service.get_step(step_id)["evidence"]
        self.assertEqual(2, len(evidence["refunds"]))
        with self.assertRaises(ConflictError):
            self.service.revise_plan(request_id="rv", actor_id="op1", plan_id="p1",
                                     reason="撤销退款",
                                     lines=[{"batch_id": "b1", "decision": "discount",
                                             "quantity": 100, "unit_price": 20,
                                             "target_org_id": "o1"}])

    def test_freeze_after_approval_blocks_internal_execution(self):
        self.batch(request_id="brr", batch_id="br", quantity=30, package_status="faded",
                   location_type="self_owned", location_org_id="o1",
                   location_name="厂仓", distributor_id=None)
        self.service.create_plan(request_id="p3", actor_id="op1", plan_id="p3", title="t",
                                 lines=[{"batch_id": "br", "decision": "relabel",
                                         "quantity": 30}])
        self.service.approve_plan(request_id="ap3", actor_id="rev1", plan_id="p3")
        step_id = self.service.get_plan("p3")["steps"][0]["step_id"]
        # 审批后出现标签风险，冻结全部范围：执行必须被拦下。
        self.service.freeze_batch(request_id="fz3", actor_id="op1", batch_id="br",
                                  quantity=30, reason="新增标签风险", scope_note="全部")
        with self.assertRaises(ConflictError):
            self.service.execute_step(request_id="ex", actor_id="op1", step_id=step_id,
                                      evidence={"note": "换标"})
        self.service.release_freeze(request_id="rl3", actor_id="op1",
                                    freeze_id=self.service.list_freezes("br")[0].freeze_id,
                                    reason="复检通过")
        receipt = self.service.execute_step(request_id="ex2", actor_id="op1",
                                            step_id=step_id, evidence={"note": "换标"})
        self.assertFalse(receipt.replayed)

    def test_relabel_output_is_non_legacy_batch(self):
        self.batch(request_id="brr", batch_id="br", quantity=30, package_status="faded",
                   location_type="self_owned", location_org_id="o1",
                   location_name="厂仓", distributor_id=None)
        self.service.create_plan(request_id="p3", actor_id="op1", plan_id="p3", title="t",
                                 lines=[{"batch_id": "br", "decision": "relabel",
                                         "quantity": 30}])
        self.service.approve_plan(request_id="ap3", actor_id="rev1", plan_id="p3")
        step_id = self.service.get_plan("p3")["steps"][0]["step_id"]
        receipt = self.service.execute_step(request_id="ex", actor_id="op1", step_id=step_id,
                                            evidence={"note": "换标"})
        output = receipt.extra["output_batch_id"]
        self.assertFalse(self.service.get_batch(output).is_legacy)
        self.assertEqual(0, self.service.get_batch("br").available_qty)


class ManagementQueryTest(DisposalCase):
    def test_open_actions_and_overdue(self):
        self.batch()
        self.service.create_plan(request_id="p1", actor_id="op1", plan_id="p1", title="t",
                                 lines=[{"batch_id": "b1", "decision": "discount",
                                         "quantity": 100, "unit_price": 20,
                                         "target_org_id": "o1",
                                         "due_at": "2026-10-01T00:00:00Z"}])
        self.service.approve_plan(request_id="ap", actor_id="rev1", plan_id="p1")
        result = self.service.open_actions(product_id="p1")
        self.assertEqual(1, result["count"])
        self.assertEqual(1, result["overdue_count"])
        self.assertTrue(result["items"][0]["overdue"])
        by_dealer = self.service.open_actions(distributor_id="o2")
        self.assertEqual(1, by_dealer["count"])

    def test_profit_loss_and_custody_chain(self):
        self.batch(quantity=100)
        self.service.create_plan(request_id="p1", actor_id="op1", plan_id="p1", title="t",
                                 lines=[{"batch_id": "b1", "decision": "discount",
                                         "quantity": 100, "unit_price": 20,
                                         "target_org_id": "o1"}])
        self.service.approve_plan(request_id="ap", actor_id="rev1", plan_id="p1")
        step_id = self.service.get_plan("p1")["steps"][0]["step_id"]
        self.service.confirm_handoff(request_id="ship", actor_id="op2", step_id=step_id,
                                     party_role="dispatch", scan_code="S1")
        self.service.confirm_handoff(request_id="recv", actor_id="op1", step_id=step_id,
                                     party_role="receipt", scan_code="S2")
        pnl = self.service.profit_loss_basis()
        self.assertEqual(5000.0, pnl["totals"]["cost_amount"])
        self.assertEqual(2000.0, pnl["totals"]["revenue_amount"])
        chain = self.service.custody_chain(self.service.get_step(step_id)["output_batch_id"])
        self.assertEqual(2, len(chain["lineage"]))
        kinds = {event["kind"] for event in chain["events"]}
        self.assertIn("handoff_confirmation", kinds)
        self.assertIn("ledger", kinds)

    def test_conservation_report(self):
        report = self.service.verify_conservation()
        self.assertTrue(report["valid"])
        self.assertEqual([], report["issues"])


if __name__ == "__main__":
    unittest.main()
