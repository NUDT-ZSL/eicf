"""标准库 HTTP 客户端：有界退避重试，覆盖 429/500/截断 JSON/超时。"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class RetryableHttpError(Exception):
    """可重试的传输/服务端错误。"""


@dataclass
class FetchResult:
    payload: dict[str, Any]
    attempts: int  # 含首次成功请求在内的总请求次数
    retries: int   # 失败重试次数


class HttpClient:
    def __init__(
        self,
        max_retries: int,
        backoff_seconds: float,
        timeout_seconds: float,
    ) -> None:
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.timeout_seconds = timeout_seconds

    def get_json(self, url: str) -> FetchResult:
        """GET 并解析 JSON。

        最多发起 ``max_retries+1`` 次请求（首次 + max_retries 次重试）。
        429/500、连接超时、响应体截断（JSONDecodeError/连接重置）均重试。
        """
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(self.max_retries + 1):
            attempts += 1
            try:
                payload = self._one_request(url)
                return FetchResult(
                    payload=payload, attempts=attempts, retries=attempt
                )
            except RetryableHttpError as exc:
                last_error = exc
                if attempt < self.max_retries and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds)
        raise RetryableHttpError(
            f"重试 {self.max_retries} 次后仍失败: {last_error}"
        )

    def _one_request(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500):
                raise RetryableHttpError(f"HTTP {exc.code}") from exc
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            # 连接超时 / 对端无响应关闭连接
            raise RetryableHttpError(f"连接异常: {exc}") from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # 截断的 JSON 或不完整响应体
            raise RetryableHttpError(f"响应体 JSON 截断/非法: {exc}") from exc
        if not isinstance(payload, dict):
            raise RetryableHttpError("响应根节点不是 JSON 对象")
        return payload

