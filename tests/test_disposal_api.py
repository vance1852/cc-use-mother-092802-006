import unittest

from beverage_ops_foundation.api import route
from beverage_ops_foundation.disposal_service import DisposalService
from beverage_ops_foundation.storage import Database


def call(service, method, path, body=None, actor=""):
    return route(service, method, path, body, {"X-Actor-Id": actor})


class DisposalApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DisposalService(self.database)
        call(self.service, "POST", "/organizations",
             {"request_id": "org", "organization_id": "brand", "name": "品牌方"}, "bootstrap")
        call(self.service, "POST", "/actors",
             {"request_id": "admin", "new_actor_id": "admin", "display_name": "管理员",
              "role": "admin", "organization_id": "brand"}, "bootstrap")
        call(self.service, "POST", "/organizations",
             {"request_id": "org2", "organization_id": "dealer", "name": "渠道商"}, "admin")
        for request_id, actor_id, name, role, org in (
                ("op-id", "op", "运营", "operator", "brand"),
                ("rv-id", "rv", "审核", "reviewer", "brand"),
                ("dp-id", "dp", "渠道保管", "operator", "dealer")):
            call(self.service, "POST", "/actors",
                 {"request_id": request_id, "new_actor_id": actor_id, "display_name": name,
                  "role": role, "organization_id": org}, "admin")
        status, payload = call(self.service, "POST", "/disposal/rulesets", {
            "request_id": "rs", "family": "liquor", "version": "2026.10",
            "decision_matrix": {"intact": "discount", "scratched": "return",
                                 "label_old": "repack", "damaged": "destroy"},
            "loss_basis": {"currency": "CNY", "discount_rate": 0.6}}, "op")
        self.assertEqual(201, status)
        self.ruleset_id = payload["resource_id"]

    def tearDown(self):
        self.database.close()

    def _batch(self, state="label_old", qty=10, ref="store-1", dealer="d-1",
               location="channel", product="p1", custodian="dp", request_id="batch"):
        status, payload = call(self.service, "POST", "/disposal/batches", {
            "request_id": request_id, "product_id": product, "product_name": "老款酒",
            "quantity": qty, "unit": "箱", "age_days": 400, "package_state": state,
            "location_type": location, "location_ref": ref, "ruleset_id": self.ruleset_id,
            "dealer_id": dealer, "custodian_actor_id": custodian}, "op")
        self.assertEqual(201, status, payload)
        return payload["resource_id"]

    def _approved_plan(self):
        batch_id = self._batch()
        status, payload = call(self.service, "POST", "/disposal/plans", {
            "request_id": "plan", "ruleset_id": self.ruleset_id,
            "lines": [{"batch_id": batch_id, "quantity": 10}],
            "warehouse_ref": "wh-1", "repack_station_ref": "rp-1"}, "op")
        self.assertEqual(201, status)
        plan_id = payload["resource_id"]
        self.assertEqual(200, call(self.service, "POST", "/disposal/plan-submissions",
                                   {"request_id": "ps", "plan_id": plan_id}, "op")[0])
        self.assertEqual(201, call(self.service, "POST", "/disposal/plan-approvals",
                                   {"request_id": "pa", "plan_id": plan_id}, "rv")[0])
        return batch_id, plan_id

    def test_unknown_disposal_route_is_404(self):
        status, payload = call(self.service, "GET", "/disposal/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_full_flow_over_http(self):
        batch_id, plan_id = self._approved_plan()
        status, payload = call(self.service, "GET", f"/disposal/steps?plan_id={plan_id}")
        self.assertEqual(200, status)
        pickup_id, repack_id = [i["step_id"] for i in payload["items"]]

        status, payload = call(self.service, "POST", "/disposal/handovers", {
            "request_id": "h1", "step_id": pickup_id, "quantity": 10,
            "evidence_ref": "DN-1"}, "op")
        self.assertEqual(201, status)
        event_id = payload["resource_id"]
        self.assertEqual(200, call(self.service, "POST", "/disposal/handover-confirmations",
                                   {"request_id": "h1-dp", "event_id": event_id}, "dp")[0])
        self.assertEqual(200, call(self.service, "POST", "/disposal/handover-confirmations",
                                   {"request_id": "h1-op", "event_id": event_id}, "op")[0])
        status, payload = call(self.service, "POST", "/disposal/scans", {
            "request_id": "sc1", "step_id": repack_id, "scan_type": "repack",
            "quantity": 10, "evidence_ref": "RP-1"}, "op")
        self.assertEqual(201, status)
        self.assertFalse(payload["duplicate"])

        status, payload = call(self.service, "GET", f"/disposal/conservation?batch_id={batch_id}")
        self.assertEqual(200, status)
        self.assertTrue(payload["balanced"])

        status, payload = call(self.service, "GET", f"/disposal/custody-chain?batch_id={batch_id}")
        self.assertEqual(200, status)
        self.assertTrue(payload["conservation"]["balanced"])
        self.assertIn("events", payload)

    def test_duplicate_scan_reports_flag(self):
        _, plan_id = self._approved_plan()
        steps = call(self.service, "GET", f"/disposal/steps?plan_id={plan_id}")[1]["items"]
        pickup_id, repack_id = steps[0]["step_id"], steps[1]["step_id"]
        event_id = call(self.service, "POST", "/disposal/handovers", {
            "request_id": "h1", "step_id": pickup_id, "quantity": 10, "evidence_ref": "DN-1"}, "op")[1]["resource_id"]
        call(self.service, "POST", "/disposal/handover-confirmations",
             {"request_id": "h1-dp", "event_id": event_id}, "dp")
        call(self.service, "POST", "/disposal/handover-confirmations",
             {"request_id": "h1-op", "event_id": event_id}, "op")
        call(self.service, "POST", "/disposal/scans",
             {"request_id": "sc1", "step_id": repack_id, "scan_type": "repack",
              "quantity": 10, "evidence_ref": "RP-1"}, "op")
        status, payload = call(self.service, "POST", "/disposal/scans", {
            "request_id": "sc2", "step_id": repack_id, "scan_type": "repack",
            "quantity": 10, "evidence_ref": "RP-1"}, "op")
        self.assertEqual(201, status)
        self.assertTrue(payload["duplicate"])

    def test_discount_flow_and_loss_basis_query(self):
        batch_id = self._batch(state="intact")
        plan_id = call(self.service, "POST", "/disposal/plans", {
            "request_id": "plan", "ruleset_id": self.ruleset_id,
            "lines": [{"batch_id": batch_id, "quantity": 10}],
            "warehouse_ref": "wh-1", "repack_station_ref": "rp-1"}, "op")[1]["resource_id"]
        call(self.service, "POST", "/disposal/plan-submissions",
             {"request_id": "ps", "plan_id": plan_id}, "op")
        call(self.service, "POST", "/disposal/plan-approvals",
             {"request_id": "pa", "plan_id": plan_id}, "rv")
        step_id = call(self.service, "GET", f"/disposal/steps?plan_id={plan_id}")[1]["items"][0]["step_id"]
        call(self.service, "POST", "/disposal/scans",
             {"request_id": "sc", "step_id": step_id, "scan_type": "discount",
              "quantity": 10, "evidence_ref": "SX-1"}, "dp")
        call(self.service, "POST", "/disposal/settlements",
             {"request_id": "st", "step_id": step_id, "amount": 1200.0,
              "currency": "CNY", "evidence_ref": "TK-1"}, "op")
        status, payload = call(self.service, "POST", "/disposal/discount-confirmations",
                               {"request_id": "dc", "step_id": step_id}, "dp")
        self.assertEqual(200, status)
        self.assertTrue(payload["irreversibly_closed"])
        status, payload = call(self.service, "GET", f"/disposal/loss-basis?product_id=p1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("resale", payload["items"][0]["outcome_type"])

    def test_pending_actions_filter_by_dealer(self):
        self._batch(product="p1")
        self._batch(state="intact", ref="store-2", dealer="d-2", product="p2",
                    request_id="batch-2")
        plans = call(self.service, "GET", "/disposal/batches")[1]["items"]
        for index, batch in enumerate(plans):
            plan_id = call(self.service, "POST", "/disposal/plans", {
                "request_id": f"plan{index}", "ruleset_id": self.ruleset_id,
                "lines": [{"batch_id": batch["batch_id"], "quantity": 10}],
                "warehouse_ref": "wh-1", "repack_station_ref": "rp-1"}, "op")[1]["resource_id"]
            call(self.service, "POST", "/disposal/plan-submissions",
                 {"request_id": f"ps{index}", "plan_id": plan_id}, "op")
            call(self.service, "POST", "/disposal/plan-approvals",
                 {"request_id": f"pa{index}", "plan_id": plan_id}, "rv")
        status, payload = call(self.service, "GET", "/disposal/pending-actions?dealer_id=d-2")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("d-2", payload["items"][0]["dealer_id"])

    def test_freeze_endpoint_blocks_scope_only(self):
        batch_id = self._batch(qty=10)
        status, payload = call(self.service, "POST", "/disposal/freezes", {
            "request_id": "fz", "batch_id": batch_id, "scope": "quantity",
            "reason": "渗漏", "affected_quantity": 3}, "rv")
        self.assertEqual(201, status)
        batch = call(self.service, "GET", f"/disposal/batch?batch_id={batch_id}")[1]
        self.assertTrue(batch["frozen"])
        self.assertEqual("partial", batch["frozen_scope"])
        status, payload = call(self.service, "GET", f"/disposal/freezes?batch_id={batch_id}")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))

    def test_health_reports_audit(self):
        status, payload = call(self.service, "GET", "/health")
        self.assertEqual(200, status)
        self.assertTrue(payload["audit_valid"])


if __name__ == "__main__":
    unittest.main()
