"""多站点并发采集调度器。

* 同一站点内分页串行，不同站点并发；
* 全局在途 HTTP 请求数受信号量约束（不超过配置上限）；
* 游标推进只依赖服务端返回的 ``next_cursor``，不做页码/字符串比较终止；
* 同一 next_cursor 重复时最多重取一次，仍重复则判定站点异常；
* 每页在同一 SQLite 事务内提交修订与已提交游标，支持中断恢复。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from .client import HttpClient, RetryableHttpError
from .config import CollectorConfig, SiteConfig
from .errors import CursorStuck
from .models import Record
from .storage import AuditStore

LOGGER = logging.getLogger("eicf.collector")


@dataclass
class SiteStats:
    endpoint_id: str
    pages_fetched: int = 0
    requests: int = 0
    retries: int = 0
    complete: bool = False
    error: str | None = None
    resume_from_cursor: str | None = None


@dataclass
class CollectOutcome:
    site_stats: dict[str, SiteStats] = field(default_factory=dict)
    incomplete_sites: list[str] = field(default_factory=list)
    interrupted: bool = False
    interrupt_detail: str | None = None
    raw_seen: int = 0
    inserted: int = 0


class Collector:
    def __init__(
        self,
        config: CollectorConfig,
        store: AuditStore,
        client: HttpClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client or HttpClient(
            max_retries=config.max_retries,
            backoff_seconds=config.retry_backoff_seconds,
            timeout_seconds=config.request_timeout_seconds,
        )
        self._semaphore = threading.BoundedSemaphore(config.global_max_inflight)
        self.outcome = CollectOutcome()
        self._raw_lock = threading.Lock()

    def run(self) -> CollectOutcome:
        # 在主线程、启动任何采集线程前确保 schema 已持久化，避免多线程
        # DDL 与 BEGIN IMMEDIATE 之间的竞态。
        self.store.ensure_schema()
        self.store.reset_endpoint_completion(
            site.site_id for site in self.config.sites
        )
        threads: list[threading.Thread] = []
        for site in self.config.sites:
            stats = SiteStats(
                endpoint_id=site.site_id,
                resume_from_cursor=self.store.get_resume_cursor(site.site_id),
            )
            self.outcome.site_stats[site.site_id] = stats
            thread = threading.Thread(
                target=self._collect_site,
                args=(site, stats),
                name=f"collect-{site.site_id}",
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        return self.outcome

    # ---- 单站点串行分页 --------------------------------------------
    def _collect_site(self, site: SiteConfig, stats: SiteStats) -> None:
        cursor = self.store.get_resume_cursor(site.site_id)
        LOGGER.info(
            "站点 %s 开始采集，恢复游标=%s", site.site_id, cursor
        )
        stuck_rearmed = False
        try:
            while True:
                url = self._build_url(site, cursor)
                payload, attempts = self._fetch_with_limit(site, stats, url)
                records = self._parse_records(payload)
                with self._raw_lock:
                    self.outcome.raw_seen += len(records)

                next_cursor = payload.get("next_cursor")
                has_more = bool(payload.get("has_more"))

                # 游标卡滞检测：仅比较服务端本次返回的 next_cursor 与当前游标
                if has_more and next_cursor is not None and next_cursor == cursor:
                    if not stuck_rearmed:
                        stuck_rearmed = True
                        LOGGER.warning(
                            "站点 %s 游标重复 %r，允许重取一次",
                            site.site_id,
                            next_cursor,
                        )
                        continue
                    raise CursorStuck(
                        f"站点 {site.site_id} 游标 {next_cursor!r} 重取后仍重复"
                    )

                inserted = self.store.commit_page(
                    site.site_id, records, next_cursor
                )
                with self._raw_lock:
                    self.outcome.inserted += inserted
                stats.pages_fetched += 1
                LOGGER.info(
                    "站点 %s 第 %d 页提交，记录 %d 条（新增 %d），next_cursor=%s",
                    site.site_id,
                    stats.pages_fetched,
                    len(records),
                    inserted,
                    next_cursor,
                )
                self._maybe_interrupt_hook(site, stats)

                # 终止条件只取决于服务端 has_more
                if not has_more:
                    break
                cursor = next_cursor
                stuck_rearmed = False
        except (RetryableHttpError, CursorStuck) as exc:
            stats.complete = False
            stats.error = str(exc)
            self.outcome.incomplete_sites.append(site.site_id)
            self.store.mark_complete(site.site_id, False)
            LOGGER.error("站点 %s 采集不完整: %s", site.site_id, exc)
            return
        except _InterruptHook as exc:
            self.outcome.interrupted = True
            self.outcome.interrupt_detail = str(exc)
            self.store.mark_complete(site.site_id, False)
            LOGGER.error("站点 %s 采集中断: %s", site.site_id, exc)
            return

        stats.complete = True
        self.store.mark_complete(site.site_id, True)
        LOGGER.info("站点 %s 采集完成，页数=%d", site.site_id, stats.pages_fetched)

    def _fetch_with_limit(
        self, site: SiteConfig, stats: SiteStats, url: str
    ) -> tuple[dict[str, Any], int]:
        # 全局在途上限：获取信号量期间才发起 HTTP 请求
        with self._semaphore:
            result = self.client.get_json(url)
        stats.requests += result.attempts
        stats.retries += result.retries
        self.store.add_request(site.site_id, result.retries)
        return result.payload, result.attempts

    @staticmethod
    def _parse_records(payload: dict[str, Any]) -> list[Record]:
        raw_records = payload.get("records", [])
        if not isinstance(raw_records, list):
            raise RetryableHttpError("records 字段不是数组")
        return [Record.from_payload(item) for item in raw_records]

    @staticmethod
    def _build_url(site: SiteConfig, cursor: str | None) -> str:
        query = f"page_size={site.page_size_limit}"
        if cursor is not None:
            query += f"&cursor={cursor}"
        return f"{site.base_url}/records?{query}"

    # ---- 测试钩子：在指定页提交后中断 ------------------------------
    def _maybe_interrupt_hook(self, site: SiteConfig, stats: SiteStats) -> None:
        target_site = os.environ.get("EICF_INTERRUPT_SITE")
        if not target_site or site.site_id != target_site:
            return
        try:
            after_page = int(os.environ.get("EICF_INTERRUPT_AFTER_PAGE", "0"))
        except ValueError:
            after_page = 0
        if stats.pages_fetched == after_page:
            raise _InterruptHook(
                f"测试钩子：站点 {site.site_id} 第 {after_page} 页后中断"
            )


class _InterruptHook(Exception):
    pass

