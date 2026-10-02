import unittest
from datetime import datetime, timedelta, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.disposal_service import (
    DisposalService,
    LOCATION_CHANNEL,
    LOCATION_IN_TRANSIT,
    LOCATION_SELF_WAREHOUSE,
    PLAN_APPROVED,
    PLAN_DRAFT,
    PLAN_SUPERSEDED,
    STEP_AWAITING,
    STEP_DONE,
    STEP_IN_PROGRESS,
    STEP_PENDING,
    STEP_SUPERSEDED,
)
from beverage_ops_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from beverage_ops_foundation.storage import Database

MATRIX = {"intact": "discount", "scratched": "return",
          "label_old": "repack", "damaged": "destroy"}
LOSS = {"currency": "CNY", "discount_rate": 0.6, "destroy_unit_loss": 300}


class DisposalTestCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.service = DisposalService(self.database, FixedClock(self.start))
        self.service.register_organization(request_id="o1", actor_id="bootstrap",
                                           organization_id="brand", name="品牌方")
        self.service.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin",
                                    display_name="管理员", role="admin", organization_id="brand")
        self.service.register_organization(request_id="o2", actor_id="admin",
                                           organization_id="dealer", name="渠道商")
        self.service.register_actor(request_id="a-op", actor_id="admin", new_actor_id="op",
                                    display_name="运营", role="operator", organization_id="brand")
        self.service.register_actor(request_id="a-rv", actor_id="admin", new_actor_id="rv",
                                    display_name="审核", role="reviewer", organization_id="brand")
        self.service.register_actor(request_id="a-dp", actor_id="admin", new_actor_id="dp",
                                    display_name="渠道保管", role="operator", organization_id="dealer")
        self.ruleset_id = self.service.register_ruleset(
            request_id="rs", actor_id="op", family="liquor", version="2026.10",
            decision_matrix=MATRIX, loss_basis=LOSS).resource_id

    def tearDown(self):
        self.database.close()

    def batch(self, request_id="batch", *, product="p1", qty=100, state="label_old",
              location=LOCATION_CHANNEL, ref="store-1", dealer="d-1", custodian="dp"):
        return self.service.register_batch(
            request_id=request_id, actor_id="op", product_id=product, product_name="老款酒",
            quantity=qty, unit="箱", age_days=400, package_state=state,
            location_type=location, location_ref=ref, ruleset_id=self.ruleset_id,
            dealer_id=dealer, custodian_actor_id=custodian).resource_id

    def plan(self, lines, request_id="plan"):
        return self.service.create_plan(
            request_id=request_id, actor_id="op", ruleset_id=self.ruleset_id,
            lines=lines, warehouse_ref="wh-1", repack_station_ref="rp-1").resource_id

    def approve(self, plan_id, submit_request="ps", approve_request="pa"):
        self.service.submit_plan(request_id=submit_request, actor_id="op", plan_id=plan_id)
        self.service.approve_plan(request_id=approve_request, actor_id="rv", plan_id=plan_id)

    def steps(self, plan_id):
        return self.service.list_steps(plan_id=plan_id)

    # ------------------------------------------------------------ 规则与批次

    def test_ruleset_version_is_immutable(self):
        with self.assertRaises(ConflictError):
            self.service.register_ruleset(
                request_id="rs2", actor_id="op", family="liquor", version="2026.10",
                decision_matrix={**MATRIX, "intact": "return"}, loss_basis=LOSS)

    def test_ruleset_same_content_replays(self):
        again = self.service.register_ruleset(
            request_id="rs-again", actor_id="op", family="liquor", version="2026.10",
            decision_matrix=MATRIX, loss_basis=LOSS)
        self.assertTrue(again.replayed)
        self.assertEqual(self.ruleset_id, again.resource_id)

    def test_batch_snapshot_records_age_package_location_ruleset_and_custodian(self):
        batch_id = self.batch()
        batch = self.service.get_batch(batch_id)
        self.assertEqual(400, batch.age_days)
        self.assertEqual("label_old", batch.package_state)
        self.assertEqual(LOCATION_CHANNEL, batch.location_type)
        self.assertEqual("d-1", batch.dealer_id)
        self.assertEqual("dp", batch.custodian_actor_id)
        self.assertEqual("2026.10", batch.ruleset_version)

    def test_channel_batch_requires_dealer(self):
        with self.assertRaises(ValidationError):
            self.batch("bx", dealer="")

    # ------------------------------------------------------------ 计划与审批

    def test_steps_exist_only_after_approval(self):
        batch_id = self.batch()
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 100}])
        self.assertEqual([], self.steps(plan_id))
        with self.assertRaises(ConflictError):
            self.service.approve_plan(request_id="pa", actor_id="rv", plan_id=plan_id)
        self.approve(plan_id)
        actions = [s.action for s in self.steps(plan_id)]
        self.assertEqual(["pickup", "repack"], actions)

    def test_creator_cannot_approve_own_plan(self):
        batch_id = self.batch(qty=10)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.service.submit_plan(request_id="ps", actor_id="op", plan_id=plan_id)
        with self.assertRaises(PermissionDenied):
            self.service.approve_plan(request_id="pa", actor_id="op", plan_id=plan_id)

    def test_action_matrix_generates_expected_steps(self):
        cases = {
            "intact": (["discount"],),
            "scratched": (["return"],),
            "label_old": (["pickup", "repack"],),
            "damaged": (["destroy"],),
        }
        for index, (state, expected) in enumerate(cases.items()):
            batch_id = self.batch(f"b{index}", product=f"p{index}", qty=10, state=state)
            plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}], request_id=f"p{index}")
            self.approve(plan_id, f"ps{index}", f"pa{index}")
            self.assertEqual(expected[0], [s.action for s in self.steps(plan_id)])

    def test_draft_plan_is_editable_submitted_plan_is_not(self):
        batch_id = self.batch(qty=50)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 50}])
        self.service.update_plan(request_id="pu", actor_id="op", plan_id=plan_id,
                                 lines=[{"batch_id": batch_id, "quantity": 40}])
        self.service.submit_plan(request_id="ps", actor_id="op", plan_id=plan_id)
        with self.assertRaises(ConflictError):
            self.service.update_plan(request_id="pu2", actor_id="op", plan_id=plan_id,
                                     lines=[{"batch_id": batch_id, "quantity": 30}])

    def test_plan_quantity_cannot_exceed_stock_and_double_booking_is_blocked(self):
        batch_id = self.batch(qty=10)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}], request_id="p1")
        self.approve(plan_id, "ps1", "pa1")
        plan2 = self.plan([{"batch_id": batch_id, "quantity": 10}], request_id="p2")
        self.service.submit_plan(request_id="ps2", actor_id="op", plan_id=plan2)
        with self.assertRaises(ConflictError):
            self.service.approve_plan(request_id="pa2", actor_id="rv", plan_id=plan2)

    def test_split_locations_can_be_planned_explicitly_after_partial_completion(self):
        batch_id = self.batch(qty=100)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 90}], request_id="plan-1")
        self.approve(plan_id, "ps1", "pa1")
        pickup, repack = self.steps(plan_id)
        event_id = self.service.propose_handover(
            request_id="h1", actor_id="op", step_id=pickup.step_id,
            quantity=90, evidence_ref="DN-1").resource_id
        self.service.confirm_custody(request_id="h1-dp", actor_id="dp", event_id=event_id)
        self.service.confirm_custody(request_id="h1-op", actor_id="op", event_id=event_id)
        self.service.record_scan(request_id="sc1", actor_id="op", step_id=repack.step_id,
                                 scan_type="repack", quantity=90, evidence_ref="RP-1")
        # 现在 90 箱在自有仓、10 箱仍在渠道。指定不存在位置应失败。
        with self.assertRaises(ValidationError):
            self.plan([{"batch_id": batch_id, "quantity": 10,
                        "location_type": "channel", "location_ref": "store-x"}], request_id="plan-bad")
        # 指定渠道位置可以为剩余 10 箱排退仓。
        plan2 = self.plan([{"batch_id": batch_id, "quantity": 10, "action": "return",
                           "location_type": "channel", "location_ref": "store-1"}],
                          request_id="plan-2")
        self.approve(plan2, "ps2", "pa2")
        step = self.steps(plan2)[0]
        self.assertEqual("return", step.action)
        self.assertEqual(LOCATION_CHANNEL, step.from_location_type)
        self.assertEqual("store-1", step.from_location_ref)

    # ------------------------------------------------------------ 交接与守恒

    def _pickup_plan(self, qty=100):
        batch_id = self.batch(qty=qty)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": qty}])
        self.approve(plan_id)
        pickup = self.steps(plan_id)[0]
        return batch_id, pickup

    def test_handover_requires_both_parties_and_conserves_quantity(self):
        batch_id, pickup = self._pickup_plan()
        event_id = self.service.propose_handover(
            request_id="h1", actor_id="op", step_id=pickup.step_id,
            quantity=100, evidence_ref="DN-1").resource_id
        self.assertEqual(STEP_IN_PROGRESS, self.service.list_steps(step_id=pickup.step_id)[0].status)
        # 渠道（发出方）确认起运：货入在途，数量仍守恒。
        self.service.confirm_custody(request_id="h1-dp", actor_id="dp", event_id=event_id)
        event = self.service.get_custody_event(event_id)
        self.assertEqual("in_transit", event.status)
        self.assertEqual(STEP_AWAITING, self.service.list_steps(step_id=pickup.step_id)[0].status)
        conservation = self.service.verify_conservation(batch_id)
        self.assertTrue(conservation["balanced"])
        in_transit = self.database.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) q FROM disposal_batch_stocks WHERE location_type=?",
            (LOCATION_IN_TRANSIT,)).fetchone()["q"]
        self.assertEqual(100, in_transit)
        # 品牌方签收入库前，货物不应出现在换包站。
        station = self.database.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) q FROM disposal_batch_stocks "
            "WHERE location_type='repack_station'").fetchone()["q"]
        self.assertEqual(0, station)
        self.service.confirm_custody(request_id="h1-op", actor_id="op", event_id=event_id)
        self.assertEqual("confirmed", self.service.get_custody_event(event_id).status)
        self.assertEqual(STEP_DONE, self.service.list_steps(step_id=pickup.step_id)[0].status)
        self.assertTrue(self.service.verify_conservation(batch_id)["balanced"])

    def test_unrelated_actor_cannot_confirm_handover(self):
        _, pickup = self._pickup_plan(10)
        event_id = self.service.propose_handover(
            request_id="h1", actor_id="op", step_id=pickup.step_id,
            quantity=10, evidence_ref="DN-1").resource_id
        with self.assertRaises(PermissionDenied):
            self.service.confirm_custody(request_id="hx", actor_id="rv", event_id=event_id)

    def test_reject_before_dispatch_releases_step(self):
        _, pickup = self._pickup_plan(10)
        event_id = self.service.propose_handover(
            request_id="h1", actor_id="op", step_id=pickup.step_id,
            quantity=10, evidence_ref="DN-1").resource_id
        self.service.reject_custody(request_id="rj", actor_id="dp", event_id=event_id, reason="缺货")
        self.assertEqual(STEP_PENDING, self.service.list_steps(step_id=pickup.step_id)[0].status)

    def test_reject_after_dispatch_is_blocked(self):
        _, pickup = self._pickup_plan(10)
        event_id = self.service.propose_handover(
            request_id="h1", actor_id="op", step_id=pickup.step_id,
            quantity=10, evidence_ref="DN-1").resource_id
        self.service.confirm_custody(request_id="h1-dp", actor_id="dp", event_id=event_id)
        with self.assertRaises(ConflictError):
            self.service.reject_custody(request_id="rj", actor_id="dp",
                                        event_id=event_id, reason="反悔")

    def test_partial_handover_then_second_batch_conserves(self):
        batch_id, pickup = self._pickup_plan(100)
        e1 = self.service.propose_handover(
            request_id="h1", actor_id="op", step_id=pickup.step_id,
            quantity=60, evidence_ref="DN-1").resource_id
        self.service.confirm_custody(request_id="h1-dp", actor_id="dp", event_id=e1)
        self.service.confirm_custody(request_id="h1-op", actor_id="op", event_id=e1)
        step = self.service.list_steps(step_id=pickup.step_id)[0]
        self.assertEqual(STEP_IN_PROGRESS, step.status)
        self.assertEqual(60, step.done_qty)
        e2 = self.service.propose_handover(
            request_id="h2", actor_id="op", step_id=pickup.step_id,
            quantity=40, evidence_ref="DN-2").resource_id
        self.service.confirm_custody(request_id="h2-dp", actor_id="dp", event_id=e2)
        self.service.confirm_custody(request_id="h2-op", actor_id="op", event_id=e2)
        self.assertEqual(STEP_DONE, self.service.list_steps(step_id=pickup.step_id)[0].status)
        self.assertTrue(self.service.verify_conservation(batch_id)["balanced"])

    def test_handover_cannot_exceed_remaining_quantity(self):
        _, pickup = self._pickup_plan(10)
        with self.assertRaises(ValidationError):
            self.service.propose_handover(
                request_id="h1", actor_id="op", step_id=pickup.step_id,
                quantity=11, evidence_ref="DN-1")

    # ------------------------------------------------------------ 冻结

    def test_partial_freeze_only_blocks_affected_quantity(self):
        batch_id = self.batch(qty=100)
        self.service.raise_freeze(request_id="f1", actor_id="rv", batch_id=batch_id,
                                  scope="quantity", reason="渗漏", affected_quantity=10)
        view = self.service.get_batch(batch_id)
        self.assertTrue(view.frozen)
        self.assertEqual("partial", view.frozen_scope)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 91}], request_id="p-bad")
        self.service.submit_plan(request_id="ps-bad", actor_id="op", plan_id=plan_id)
        with self.assertRaises(ConflictError):
            self.service.approve_plan(request_id="pa-bad", actor_id="rv", plan_id=plan_id)
        plan_ok = self.plan([{"batch_id": batch_id, "quantity": 90}], request_id="p-ok")
        self.approve(plan_ok, "ps-ok", "pa-ok")
        self.assertEqual(2, len(self.steps(plan_ok)))

    def test_whole_batch_freeze_blocks_everything_until_lifted(self):
        batch_id = self.batch(qty=10)
        self.service.raise_freeze(request_id="f1", actor_id="rv", batch_id=batch_id,
                                  scope="batch", reason="标签合规调查")
        self.assertEqual(0, self.service._available_qty(self.database.connection, batch_id))
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.service.submit_plan(request_id="ps", actor_id="op", plan_id=plan_id)
        with self.assertRaises(ConflictError):
            self.service.approve_plan(request_id="pa", actor_id="rv", plan_id=plan_id)
        freeze_id = self.service.list_freezes(batch_id)[0].freeze_id
        self.service.lift_freeze(request_id="lf", actor_id="rv", freeze_id=freeze_id)
        self.service.approve_plan(request_id="pa2", actor_id="rv", plan_id=plan_id)
        self.assertEqual(PLAN_APPROVED, self.service.get_plan(plan_id).status)

    def test_freeze_during_awaiting_handover_blocks_remainder_but_keeps_in_transit(self):
        batch_id, pickup = self._pickup_plan(100)
        event_id = self.service.propose_handover(
            request_id="h1", actor_id="op", step_id=pickup.step_id,
            quantity=60, evidence_ref="DN-1").resource_id
        self.service.confirm_custody(request_id="h1-dp", actor_id="dp", event_id=event_id)
        # 在途 60 不受影响；对剩余 40 全部冻结后，不能再发起交接。
        self.service.raise_freeze(request_id="f1", actor_id="rv", batch_id=batch_id,
                                  scope="quantity", reason="新增质量预警", affected_quantity=40)
        self.assertTrue(self.service.verify_conservation(batch_id)["balanced"])
        with self.assertRaises(ConflictError):
            self.service.propose_handover(
                request_id="h2", actor_id="op", step_id=pickup.step_id,
                quantity=10, evidence_ref="DN-2")

    # ------------------------------------------------------------ 终态保护

    def test_destroyed_fact_cannot_be_revoked(self):
        batch_id = self.batch("b1", product="p1", qty=20, state="damaged",
                              location=LOCATION_SELF_WAREHOUSE, ref="wh-1", dealer="",
                              custodian="op")
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 20}], request_id="p1")
        self.approve(plan_id, "ps1", "pa1")
        destroy = self.steps(plan_id)[0]
        self.service.propose_handover(request_id="h1", actor_id="op",
                                      step_id=destroy.step_id, quantity=20, evidence_ref="XH-1")
        self.assertEqual(STEP_DONE, self.service.list_steps(step_id=destroy.step_id)[0].status)
        # 已销毁步骤拒绝任何新交接。
        with self.assertRaises(ConflictError):
            self.service.propose_handover(request_id="h2", actor_id="op",
                                          step_id=destroy.step_id, quantity=1, evidence_ref="XH-2")
        # 计划改版不能让销毁数量回来。
        with self.assertRaises(ValidationError):
            self.service.revise_plan(request_id="rv1", actor_id="op", plan_id=plan_id,
                                     lines=[{"batch_id": batch_id, "quantity": 20}])
        self.assertEqual(0, self.service.get_batch(batch_id).quantity)
        self.assertTrue(self.service.verify_conservation(batch_id)["balanced"])
        loss = self.service.loss_basis(product_id="p1")
        self.assertEqual(1, len(loss))
        self.assertEqual("destroyed", loss[0].outcome_type)
        self.assertEqual("2026.10", loss[0].ruleset_version)

    def test_settlement_is_write_once(self):
        batch_id = self.batch(qty=10, state="intact")
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.approve(plan_id)
        step = self.steps(plan_id)[0]
        self.service.record_scan(request_id="s1", actor_id="dp", step_id=step.step_id,
                                 scan_type="discount", quantity=10, evidence_ref="SX-1")
        self.service.record_settlement(request_id="st1", actor_id="op", step_id=step.step_id,
                                       amount=1000.0, currency="CNY", evidence_ref="TK-1")
        with self.assertRaises(ConflictError):
            self.service.record_settlement(request_id="st2", actor_id="op", step_id=step.step_id,
                                           amount=1.0, currency="CNY", evidence_ref="TK-2")
        self.service.confirm_discount(request_id="dc1", actor_id="dp", step_id=step.step_id)
        done = self.service.list_steps(step_id=step.step_id)[0]
        self.assertTrue(done.irreversibly_closed)
        self.assertEqual("discount_settled", done.closure_kind)

    def test_discount_requires_scan_and_settlement(self):
        batch_id = self.batch(qty=10, state="intact")
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.approve(plan_id)
        step = self.steps(plan_id)[0]
        with self.assertRaises(ConflictError):
            self.service.confirm_discount(request_id="dc", actor_id="dp", step_id=step.step_id)

    # ------------------------------------------------------------ 改版

    def test_revision_supersedes_pending_steps_but_keeps_facts(self):
        batch_id = self.batch(qty=100)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 100}])
        self.approve(plan_id)
        pickup = self.steps(plan_id)[0]
        event_id = self.service.propose_handover(
            request_id="h1", actor_id="op", step_id=pickup.step_id,
            quantity=100, evidence_ref="DN-1").resource_id
        self.service.confirm_custody(request_id="h1-dp", actor_id="dp", event_id=event_id)
        # 在途时改版必须被阻止。
        with self.assertRaises(ConflictError):
            self.service.revise_plan(request_id="rv-bad", actor_id="op", plan_id=plan_id,
                                     lines=[{"batch_id": batch_id, "quantity": 100}])
        self.service.confirm_custody(request_id="h1-op", actor_id="op", event_id=event_id)
        repack = self.steps(plan_id)[1]
        self.service.record_scan(request_id="sc1", actor_id="op", step_id=repack.step_id,
                                 scan_type="repack", quantity=100, evidence_ref="RP-1")
        new_plan_id = self.service.revise_plan(
            request_id="rv-ok", actor_id="op", plan_id=plan_id,
            lines=[{"batch_id": batch_id, "quantity": 100}]).resource_id
        self.assertEqual(PLAN_SUPERSEDED, self.service.get_plan(plan_id).status)
        self.assertEqual(2, self.service.get_plan(new_plan_id).revision)
        self.assertEqual(PLAN_DRAFT, self.service.get_plan(new_plan_id).status)
        # 已完成的步骤不被撤销。
        statuses = {s.action: s.status for s in self.steps(plan_id)}
        self.assertEqual(STEP_DONE, statuses["pickup"])
        self.assertEqual(STEP_DONE, statuses["repack"])
        self.assertTrue(self.service.verify_conservation(batch_id)["balanced"])

    def test_late_scan_on_superseded_step_has_no_effect(self):
        batch_id = self.batch(qty=10)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.approve(plan_id)
        new_plan_id = self.service.revise_plan(
            request_id="rv", actor_id="op", plan_id=plan_id,
            lines=[{"batch_id": batch_id, "quantity": 10}]).resource_id
        self.assertEqual(STEP_SUPERSEDED, self.steps(plan_id)[0].status)
        self.service.record_scan(request_id="sc-late", actor_id="op",
                                 step_id=self.steps(plan_id)[0].step_id,
                                 scan_type="pickup", quantity=10, evidence_ref="OLD-1")
        scan = self.service.list_scans(self.steps(plan_id)[0].step_id)[0]
        self.assertTrue(scan.late)
        self.assertTrue(scan.duplicate_of == "")
        # 晚到回执不重开步骤、不移动货物。
        self.assertEqual(STEP_SUPERSEDED, self.steps(plan_id)[0].status)
        self.assertTrue(self.service.verify_conservation(batch_id)["balanced"])
        self.assertTrue(new_plan_id)

    # ------------------------------------------------------------ 扫描回执

    def test_duplicate_scan_is_flagged_and_idempotent(self):
        batch_id = self.batch(qty=10)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.approve(plan_id)
        repack = self.steps(plan_id)[1]
        # 先把货提到换包站。
        pickup = self.steps(plan_id)[0]
        event_id = self.service.propose_handover(
            request_id="h1", actor_id="op", step_id=pickup.step_id,
            quantity=10, evidence_ref="DN-1").resource_id
        self.service.confirm_custody(request_id="h1-dp", actor_id="dp", event_id=event_id)
        self.service.confirm_custody(request_id="h1-op", actor_id="op", event_id=event_id)
        self.service.record_scan(request_id="sc1", actor_id="op", step_id=repack.step_id,
                                 scan_type="repack", quantity=10, evidence_ref="RP-1")
        self.service.record_scan(request_id="sc2", actor_id="op", step_id=repack.step_id,
                                 scan_type="repack", quantity=10, evidence_ref="RP-1")
        scans = self.service.list_scans(repack.step_id)
        self.assertEqual("", scans[0].duplicate_of)
        self.assertEqual(scans[0].scan_id, scans[1].duplicate_of)
        self.assertEqual(STEP_DONE, self.service.list_steps(step_id=repack.step_id)[0].status)

    def test_scan_type_must_match_step(self):
        batch_id = self.batch(qty=10, state="damaged", location=LOCATION_SELF_WAREHOUSE,
                              ref="wh-1", dealer="", custodian="op")
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.approve(plan_id)
        with self.assertRaises(ValidationError):
            self.service.record_scan(request_id="sc", actor_id="op",
                                     step_id=self.steps(plan_id)[0].step_id,
                                     scan_type="repack", quantity=10, evidence_ref="X")

    # ------------------------------------------------------------ 管理视图

    def test_pending_actions_filter_by_product_and_dealer(self):
        b1 = self.batch("b1", product="prod-A", qty=10, state="intact")
        b2 = self.batch("b2", product="prod-B", qty=10, state="intact", ref="store-2", dealer="d-2")
        p1 = self.plan([{"batch_id": b1, "quantity": 10}], request_id="p1")
        p2 = self.plan([{"batch_id": b2, "quantity": 10}], request_id="p2")
        self.approve(p1, "ps1", "pa1")
        self.approve(p2, "ps2", "pa2")
        self.assertEqual(1, len(self.service.pending_actions(product_id="prod-A")))
        self.assertEqual(1, len(self.service.pending_actions(dealer_id="d-2")))
        self.assertEqual(2, len(self.service.pending_actions()))

    def test_overdue_is_driven_by_due_date(self):
        batch_id = self.batch(qty=10, state="intact")
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.approve(plan_id)
        self.assertEqual([], self.service.overdue_actions())
        self.service.clock = FixedClock(self.start + timedelta(days=8))
        overdue = self.service.overdue_actions()
        self.assertEqual(1, len(overdue))
        self.assertTrue(overdue[0].overdue)

    def test_custody_chain_covers_full_lifecycle(self):
        batch_id = self.batch(qty=10)
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.approve(plan_id)
        chain = self.service.custody_chain(batch_id)
        kinds = {e["type"] for e in chain["events"]}
        self.assertIn("batch_registered", kinds)
        self.assertIn("plan_created", kinds)
        self.assertIn("step_generated", kinds)
        self.assertTrue(chain["conservation"]["balanced"])
        self.assertEqual("2026.10", chain["ruleset_version"])

    def test_audit_chain_stays_valid(self):
        batch_id = self.batch(qty=10, state="damaged", location=LOCATION_SELF_WAREHOUSE,
                              ref="wh-1", dealer="", custodian="op")
        plan_id = self.plan([{"batch_id": batch_id, "quantity": 10}])
        self.approve(plan_id)
        destroy = self.steps(plan_id)[0]
        self.service.propose_handover(request_id="h1", actor_id="op",
                                      step_id=destroy.step_id, quantity=10, evidence_ref="XH-1")
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)


if __name__ == "__main__":
    unittest.main()
