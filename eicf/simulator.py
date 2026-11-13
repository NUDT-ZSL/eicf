"""确定性本地仿真站点。

基于标准库 ``http.server`` 在线程中运行，支持：

* 游标分页（游标为不透明令牌，服务端维护 令牌->偏移 注册表）；
* 重复投递、撤回、更正、慢响应、截断 JSON、429/500、连接超时；
* 服务端权威的“进行中请求”计数与峰值（供并发上限断言）；
* 按页的行为覆盖（重复数据页、重复 next_cursor 等）。

所有行为由确定性配置驱动，不使用随机数，端口由操作系统分配后回传。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .models import Record


class GlobalInflight:
    """跨所有仿真站点共享的服务端在途请求计数（权威峰值来源）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self.current += 1
            if self.current > self.peak:
                self.peak = self.current

    def leave(self) -> None:
        with self._lock:
            self.current -= 1


@dataclass
class PageBehavior:
    """单个页请求的行为（按请求次序依次消费）。"""

    fault: str | None = None  # http_429/http_500/truncated/timeout/slow
    record_offset: int | None = None  # 覆盖本页记录起始偏移
    next_cursor: str | None = None  # "DEFAULT" 表示按正常规则
    has_more: bool | None = None


class _LazyBehavior:
    """行为工厂包装：请求时基于服务端实时状态生成 PageBehavior。"""

    def __init__(self, factory) -> None:  # type: ignore[no-untyped-def]
        self.factory = factory


@dataclass
class SimSite:
    """单个仿真站点配置与运行态统计。"""

    site_id: str
    records: list[Record]
    page_size: int
    faults: dict[int, list[str | None]] = field(default_factory=dict)
    behaviors: dict[int, list[PageBehavior]] = field(default_factory=dict)
    slow_seconds: float = 0.2

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._cursor_registry: dict[str, int] = {}
        self._cursor_seq = 0
        self._fault_counters: dict[int, int] = {}
        self._behavior_counters: dict[int, int] = {}
        self.request_count = 0
        self.page_request_counts: dict[int, int] = {}
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- 游标注册表 -------------------------------------------------
    def register_cursor(self, offset: int) -> str:
        with self._lock:
            self._cursor_seq += 1
            token = f"cur{self._cursor_seq:04d}"
            self._cursor_registry[token] = offset
            return token

    def resolve_cursor(self, token: str | None) -> int:
        if token is None:
            return 0
        with self._lock:
            if token not in self._cursor_registry:
                raise KeyError(token)
            return self._cursor_registry[token]

    # ---- 故障/行为消费 ---------------------------------------------
    def _consume_fault(self, page_index: int) -> str | None:
        with self._lock:
            seq = self._fault_counters.get(page_index, 0)
            self._fault_counters[page_index] = seq + 1
        rule = self.faults.get(page_index)
        if not rule:
            return None
        if seq < len(rule):
            return rule[seq]
        return rule[-1] if rule else None

    def _consume_behavior(self, page_index: int) -> PageBehavior | None:
        with self._lock:
            seq = self._behavior_counters.get(page_index, 0)
            self._behavior_counters[page_index] = seq + 1
        rule = self.behaviors.get(page_index)
        if not rule:
            return None
        if seq < len(rule):
            return rule[seq]
        return rule[-1]

    # ---- 生命周期 ---------------------------------------------------
    def start(self, inflight: GlobalInflight) -> str:
        site = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静音默认日志
                return None

            def do_GET(self) -> None:  # noqa: N802 (标准库命名)
                inflight.enter()
                try:
                    site._handle(self)
                except Exception:  # pragma: no cover - 防御性
                    self.send_response(500)
                    self.end_headers()
                finally:
                    inflight.leave()

            def send_response(self, code: int, message: str | None = None):  # type: ignore[override]
                super().send_response(code, message)
                # 统一关闭连接，避免 keep-alive 复用在快速连续请求下被重置
                self.send_header("Connection", "close")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name=f"sim-{self.site_id}", daemon=True
        )
        self._thread.start()
        port = self.httpd.server_address[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ---- 请求处理 ---------------------------------------------------
    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        if parsed.path != "/records":
            handler.send_response(404)
            handler.end_headers()
            return

        with self._lock:
            self.request_count += 1
        query = parse_qs(parsed.query)
        cursor = query.get("cursor", [None])[0]
        try:
            offset = self.resolve_cursor(cursor)
        except KeyError:
            handler.send_response(400)
            handler.end_headers()
            handler.wfile.write(b'{"error":"unknown cursor"}')
            return

        page_index = offset // self.page_size
        with self._lock:
            self.page_request_counts[page_index] = (
                self.page_request_counts.get(page_index, 0) + 1
            )

        fault = self._consume_fault(page_index)
        if fault == "slow":
            import time

            time.sleep(self.slow_seconds)
        if fault == "timeout":
            import time

            time.sleep(self.slow_seconds)
            return  # 不发送任何响应，触发客户端连接超时
        if fault in ("http_429", "http_500"):
            status = 429 if fault == "http_429" else 500
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"error": fault}).encode("utf-8"))
            return

        behavior = self._consume_behavior(page_index)
        if isinstance(behavior, _LazyBehavior):
            behavior = behavior.factory(0, self, normal_next=None, offset=offset)
        record_offset = offset
        if behavior is not None and behavior.record_offset is not None:
            record_offset = behavior.record_offset

        page_records = self.records[
            record_offset : record_offset + self.page_size
        ]
        next_offset = offset + self.page_size
        normal_has_more = next_offset < len(self.records)
        normal_next = (
            self.register_cursor(next_offset) if normal_has_more else None
        )

        # 行为显式指定优先；未指定则回退正常分页规则
        has_more = normal_has_more
        next_cursor = normal_next
        if behavior is not None:
            if behavior.has_more is not None:
                has_more = behavior.has_more
            if behavior.next_cursor is not None:
                # 非 DEFAULT 的显式游标（含重复游标）必须原样返回
                next_cursor = (
                    normal_next
                    if behavior.next_cursor == "DEFAULT"
                    else behavior.next_cursor
                )

        payload = {
            "records": [record.to_dict() for record in page_records],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }
        body = json.dumps(payload).encode("utf-8")

        if fault == "truncated":
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(body[: max(1, len(body) // 2)])
            handler.wfile.flush()
            return

        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


def start_sites(sites: list[SimSite]) -> tuple[dict[str, str], GlobalInflight]:
    inflight = GlobalInflight()
    urls: dict[str, str] = {}
    for site in sites:
        urls[site.site_id] = site.start(inflight)
    return urls, inflight


def stop_sites(sites: list[SimSite]) -> None:
    for site in sites:
        site.stop()

