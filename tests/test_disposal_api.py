import unittest

from beverage_ops_foundation.api import route
from beverage_ops_foundation.disposal import DisposalService
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database


class DisposalApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.disposal = DisposalService(self.database, self.service.clock)
        self.service.register_organization(request_id="o1", actor_id="bootstrap",
                                           organization_id="o1", name="酒厂")
        self.service.register_actor(request_id="a1r", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_organization(request_id="o2", actor_id="a1",
                                           organization_id="o2", name="经销商")
        self.service.register_actor(request_id="op1r", actor_id="a1", new_actor_id="op1",
                                    display_name="酒厂运营", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rev1r", actor_id="a1", new_actor_id="rev1",
                                    display_name="审批", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="op2r", actor_id="a1", new_actor_id="op2",
                                    display_name="经销运营", role="operator", organization_id="o2")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor}, disposal=self.disposal)

    def test_full_flow_through_http_routes(self):
        status, payload = self.call("POST", "/disposal/rules", {
            "request_id": "rule1", "rule_id": "r1",
            "payload": {"sell_age_days": 365, "return_age_days": 180,
                        "relabel_age_days": 730, "destroy_age_days": 1095}}, actor="a1")
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/disposal/batches", {
            "request_id": "b1r", "batch_id": "b1",
            "product_id": "p1", "product_name": "金沙酒", "quantity": 100, "unit": "瓶",
            "unit_cost": 50.0, "produced_at": "2025-08-01T00:00:00Z",
            "inbound_at": "2025-09-01T00:00:00Z", "package_status": "intact",
            "location_type": "channel", "location_org_id": "o2",
            "location_name": "济南门店", "responsible_party": "赵六",
            "rule_id": "r1", "distributor_id": "o2"})
        self.assertEqual(201, status)

        status, payload = self.call("GET", "/disposal/batches?product_id=p1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))

        # 渠道 2025-09-01 入库到 2026-10-02 约 396 天 > 退仓期 180，应折价。
        status, payload = self.call("POST", "/disposal/plans", {
            "request_id": "p1", "plan_id": "plan1", "title": "清理",
            "lines": [{"batch_id": "b1", "decision": "discount", "quantity": 100,
                       "unit_price": 20.0, "target_org_id": "o1"}]})
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/disposal/plans/approve", {
            "request_id": "ap1", "plan_id": "plan1"}, actor="rev1")
        self.assertEqual(201, status)
        step_id = payload["step_ids"][0]

        status, payload = self.call("POST", "/disposal/steps/handoff", {
            "request_id": "ship", "step_id": step_id,
            "party_role": "dispatch", "scan_code": "S1"}, actor="op2")
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/disposal/steps/handoff", {
            "request_id": "recv", "step_id": step_id,
            "party_role": "receipt", "scan_code": "S2"})
        self.assertEqual(201, status)
        self.assertIn("output_batch_id", payload)

        status, payload = self.call("GET", "/disposal/open-actions?distributor_id=o2")
        self.assertEqual(200, status)
        self.assertEqual(0, payload["count"])

        status, payload = self.call("GET", "/disposal/profit-loss")
        self.assertEqual(200, status)
        self.assertEqual(2000.0, payload["totals"]["revenue_amount"])

        status, payload = self.call("GET", "/disposal/custody-chain/b1")
        self.assertEqual(200, status)
        self.assertGreaterEqual(len(payload["events"]), 3)

        status, payload = self.call("GET", "/disposal/conservation")
        self.assertEqual(200, status)
        self.assertTrue(payload["valid"])

    def test_missing_actor_is_rejected(self):
        status, payload = self.call("POST", "/disposal/batches", {
            "request_id": "x", "batch_id": "b1", "product_id": "p1",
            "product_name": "金沙酒", "quantity": 100, "unit": "瓶",
            "produced_at": "2025-08-01T00:00:00Z", "inbound_at": "2025-09-01T00:00:00Z",
            "package_status": "intact", "location_type": "channel",
            "location_org_id": "o2", "location_name": "济南门店",
            "responsible_party": "赵六", "rule_id": "r1",
            "distributor_id": "o2"}, actor="")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_unknown_disposal_route_404(self):
        status, payload = self.call("GET", "/disposal/nothing")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_idempotent_replay_returns_200(self):
        self.call("POST", "/disposal/rules", {
            "request_id": "r1", "rule_id": "r9", "payload": {"note": "v1"}}, actor="a1")
        status, payload = self.call("POST", "/disposal/rules", {
            "request_id": "r1", "rule_id": "r9", "payload": {"note": "v1"}}, actor="a1")
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])


if __name__ == "__main__":
    unittest.main()
