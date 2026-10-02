"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def _disposal_routes(service: DomainService, disposal, method: str, path: str,
                     body: dict[str, Any], query: dict[str, list[str]],
                     actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """处置域路由；无法识别时返回 None 交回基础路由。"""

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    def receipt(call) -> tuple[int, dict[str, Any]]:
        result = call(actor_id=actor_id, **body)
        return 200 if result.replayed else 201, result.as_dict()

    try:
        if method == "POST" and path == "/disposal/rules":
            return receipt(disposal.publish_rule)
        if method == "POST" and path == "/disposal/batches":
            return receipt(disposal.register_batch)
        if method == "GET" and path == "/disposal/batches":
            items = [item.__dict__ for item in disposal.list_batches(
                product_id=q("product_id"), distributor_id=q("distributor_id"))]
            return 200, {"items": items}
        if method == "GET" and path.startswith("/disposal/batches/"):
            batch_id = path.rsplit("/", 1)[-1]
            return 200, disposal.get_batch(batch_id).__dict__
        if method == "POST" and path == "/disposal/freezes":
            return receipt(disposal.freeze_batch)
        if method == "POST" and path == "/disposal/freezes/release":
            return receipt(disposal.release_freeze)
        if method == "GET" and path.startswith("/disposal/freezes/"):
            batch_id = path.rsplit("/", 1)[-1]
            return 200, {"items": [item.__dict__ for item in disposal.list_freezes(batch_id)]}
        if method == "POST" and path == "/disposal/plans":
            return receipt(disposal.create_plan)
        if method == "POST" and path == "/disposal/plans/approve":
            return receipt(disposal.approve_plan)
        if method == "POST" and path == "/disposal/plans/revise":
            return receipt(disposal.revise_plan)
        if method == "GET" and path.startswith("/disposal/plans/"):
            plan_id = path.rsplit("/", 1)[-1]
            return 200, disposal.get_plan(plan_id)
        if method == "POST" and path == "/disposal/steps/handoff":
            return receipt(disposal.confirm_handoff)
        if method == "POST" and path == "/disposal/steps/execute":
            return receipt(disposal.execute_step)
        if method == "POST" and path == "/disposal/steps/refund":
            return receipt(disposal.record_refund)
        if method == "GET" and path.startswith("/disposal/steps/"):
            step_id = path.rsplit("/", 1)[-1]
            return 200, disposal.get_step(step_id)
        if method == "GET" and path == "/disposal/open-actions":
            return 200, disposal.open_actions(
                product_id=q("product_id"), distributor_id=q("distributor_id"),
                overdue_only=q("overdue_only") == "true")
        if method == "GET" and path == "/disposal/profit-loss":
            return 200, disposal.profit_loss_basis(
                product_id=q("product_id"), distributor_id=q("distributor_id"))
        if method == "GET" and path.startswith("/disposal/custody-chain/"):
            batch_id = path.rsplit("/", 1)[-1]
            return 200, disposal.custody_chain(batch_id)
        if method == "GET" and path == "/disposal/conservation":
            return 200, disposal.verify_conservation()
        return None
    except TypeError as exc:
        raise ValidationError(str(exc)) from exc


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          disposal=None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if parsed.path.startswith("/disposal/"):
            if disposal is None:
                from .disposal import DisposalService
                disposal = DisposalService(service.database, service.clock)
            query = parse_qs(parsed.query)
            dispatched = _disposal_routes(service, disposal, method, parsed.path, body,
                                          query, actor_id)
            if dispatched is not None:
                return dispatched
            return 404, {"error": "route_not_found", "message": "处置接口不存在"}
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
    disposal: object = None

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                disposal=self.disposal)
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
    Handler.service = DomainService(database)
    from .disposal import DisposalService
    Handler.disposal = DisposalService(database, Handler.service.clock)
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
