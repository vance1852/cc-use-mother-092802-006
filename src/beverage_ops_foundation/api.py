"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .disposal_service import DisposalService
from .service import DomainService
from .storage import Database


def _dataclass_items(items):
    return [item.__dict__ for item in items]


def disposal_route(service: DisposalService, method: str, path: str, body: dict[str, Any],
                   query: dict[str, list[str]]) -> tuple[int, dict[str, Any]] | None:
    """处置平台路由；返回 None 表示不属于处置平台路径。"""

    if not path.startswith("/disposal"):
        return None
    actor_id = body.pop("__actor_id", "")
    if method == "POST":
        if path == "/disposal/rulesets":
            receipt = service.register_ruleset(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if path == "/disposal/batches":
            receipt = service.register_batch(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if path == "/disposal/freezes":
            receipt = service.raise_freeze(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if path == "/disposal/freeze-lifts":
            receipt = service.lift_freeze(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if path == "/disposal/plans":
            receipt = service.create_plan(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if path == "/disposal/plan-updates":
            receipt = service.update_plan(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if path == "/disposal/plan-submissions":
            receipt = service.submit_plan(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if path == "/disposal/plan-approvals":
            receipt = service.approve_plan(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if path == "/disposal/plan-rejections":
            receipt = service.reject_plan(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if path == "/disposal/plan-revisions":
            receipt = service.revise_plan(actor_id=actor_id, **body)
            return 201, receipt.__dict__
        if path == "/disposal/handovers":
            receipt = service.propose_handover(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if path == "/disposal/handover-confirmations":
            receipt = service.confirm_custody(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if path == "/disposal/handover-rejections":
            receipt = service.reject_custody(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if path == "/disposal/scans":
            receipt = service.record_scan(actor_id=actor_id, **body)
            scan = next((s for s in service.list_scans(body.get("step_id", ""))
                         if s.scan_id == receipt.resource_id), None)
            payload = {**receipt.__dict__,
                       "duplicate": bool(scan and scan.duplicate_of),
                       "late": bool(scan and scan.late)}
            return 200 if receipt.replayed else 201, payload
        if path == "/disposal/settlements":
            receipt = service.record_settlement(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if path == "/disposal/discount-confirmations":
            receipt = service.confirm_discount(actor_id=actor_id, **body)
            step = next((s for s in service.list_steps(step_id=body.get("step_id", ""))
                         if s.step_id == receipt.resource_id), None)
            payload = {**receipt.__dict__, "status": step.status if step else "done",
                       "irreversibly_closed": bool(step and step.irreversibly_closed)}
            return 200, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    if method == "GET":
        def q(name: str, default: str = "") -> str:
            return query.get(name, [default])[0]
        if path == "/disposal/batches":
            return 200, {"items": _dataclass_items(
                service.list_batches(product_id=q("product_id"), dealer_id=q("dealer_id")))}
        if path == "/disposal/batch":
            return 200, service.get_batch(q("batch_id")).__dict__
        if path == "/disposal/freezes":
            include_lifted = q("include_lifted") in ("1", "true", "yes")
            return 200, {"items": _dataclass_items(
                service.list_freezes(q("batch_id"), include_lifted=include_lifted))}
        if path == "/disposal/plans":
            return 200, {"items": _dataclass_items(service.list_plans(q("status")))}
        if path == "/disposal/plan":
            return 200, service.get_plan(q("plan_id")).__dict__
        if path == "/disposal/steps":
            return 200, {"items": _dataclass_items(
                service.list_steps(plan_id=q("plan_id"), batch_id=q("batch_id")))}
        if path == "/disposal/custody-event":
            return 200, service.get_custody_event(q("event_id")).__dict__
        if path == "/disposal/scans":
            return 200, {"items": _dataclass_items(service.list_scans(q("step_id")))}
        if path == "/disposal/pending-actions":
            return 200, {"items": _dataclass_items(
                service.pending_actions(product_id=q("product_id"), dealer_id=q("dealer_id")))}
        if path == "/disposal/overdue-actions":
            return 200, {"items": _dataclass_items(
                service.overdue_actions(product_id=q("product_id"), dealer_id=q("dealer_id")))}
        if path == "/disposal/loss-basis":
            return 200, {"items": _dataclass_items(
                service.loss_basis(product_id=q("product_id"), dealer_id=q("dealer_id")))}
        if path == "/disposal/custody-chain":
            return 200, service.custody_chain(q("batch_id"))
        if path == "/disposal/conservation":
            return 200, service.verify_conservation(q("batch_id"))
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    return 404, {"error": "route_not_found", "message": "接口不存在"}


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = dict(body or {})
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    body["__actor_id"] = actor_id
    query = parse_qs(parsed.query)
    try:
        if isinstance(service, DisposalService):
            disposal_result = disposal_route(service, method, parsed.path, body, query)
            if disposal_result is not None:
                return disposal_result
        body.pop("__actor_id", None)
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DisposalService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
