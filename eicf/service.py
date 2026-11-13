"""采集编排：调度 -> 快照 -> 规范化报告与退出码。"""

from __future__ import annotations

import logging
from typing import Any

from .client import HttpClient
from .collector import Collector
from .config import CollectorConfig
from .errors import (
    E_INTERRUPTED,
    E_SITE_INCOMPLETE,
    EXIT_INCOMPLETE,
    EXIT_OK,
)
from .snapshots import SnapshotManager
from .storage import AuditStore

LOGGER = logging.getLogger("eicf.service")


def count_conflicts(store: AuditStore) -> int:
    """仲裁冲突数：存在多个修订（不同 revision/content_hash）的业务键数量。"""
    groups = store.grouped_revisions()
    return sum(1 for records in groups.values() if len(records) > 1)


def _build_report(
    config: CollectorConfig,
    outcome,
    store: AuditStore,
    incomplete_sites: list[str],
    error_codes: list[str],
    snapshot,
) -> dict[str, Any]:
    per_site: dict[str, dict[str, Any]] = {}
    total_pages = 0
    total_requests = 0
    total_retries = 0
    for site_id in sorted(outcome.site_stats):
        stats = outcome.site_stats[site_id]
        per_site[site_id] = {
            "pages_fetched": stats.pages_fetched,
            "requests": stats.requests,
            "retries": stats.retries,
            "complete": stats.complete,
            "resume_from_cursor": stats.resume_from_cursor,
        }
        total_pages += stats.pages_fetched
        total_requests += stats.requests
        total_retries += stats.retries

    return {
        "version": snapshot.version if snapshot is not None else 0,
        "per_site": per_site,
        "total_pages_fetched": total_pages,
        "total_requests": total_requests,
        "total_retries": total_retries,
        "raw_seen_records": outcome.raw_seen,
        "distinct_revisions": store.count_revisions(),
        "master_records": store.master_count(),
        "arbitration_conflicts": count_conflicts(store),
        "incomplete_sites": incomplete_sites,
        "snapshot_collection_hash": (
            snapshot.collection_hash if snapshot is not None else None
        ),
        "diff_summary": (
            snapshot.manifest["diff_summary"]
            if snapshot is not None
            else {"added": 0, "revised": 0, "withdrawn": 0}
        ),
        "error_codes": error_codes,
    }


def run_collection(
    config: CollectorConfig,
    fail_before_replace: bool = False,
) -> tuple[dict[str, Any], int]:
    store = AuditStore(config.db_path)
    error_codes: list[str] = []
    try:
        client = HttpClient(
            max_retries=config.max_retries,
            backoff_seconds=config.retry_backoff_seconds,
            timeout_seconds=config.request_timeout_seconds,
        )
        collector = Collector(config, store, client)
        outcome = collector.run()

        incomplete_sites = sorted(outcome.incomplete_sites)
        if outcome.interrupted:
            error_codes.append(E_INTERRUPTED)
        if incomplete_sites:
            error_codes.append(E_SITE_INCOMPLETE)

        # 中断时不生成快照，仅依赖已提交的 SQLite 状态在重启后恢复
        if outcome.interrupted:
            report = _build_report(
                config=config,
                outcome=outcome,
                store=store,
                incomplete_sites=incomplete_sites,
                error_codes=error_codes,
                snapshot=None,
            )
            return report, EXIT_INCOMPLETE

        # 不完整端点的部分数据不进入快照记录集；成功端点记录正常入库入快照
        allowed = {
            site.site_id
            for site in config.sites
            if site.site_id not in incomplete_sites
        }
        manager = SnapshotManager(
            store, config.snapshot_dir, fail_before_replace=fail_before_replace
        )
        snapshot = manager.create_snapshot(
            incomplete_sites=incomplete_sites,
            allowed_endpoints=allowed if incomplete_sites else None,
        )

        report = _build_report(
            config=config,
            outcome=outcome,
            store=store,
            incomplete_sites=incomplete_sites,
            error_codes=error_codes,
            snapshot=snapshot,
        )

        if outcome.interrupted:
            exit_code = EXIT_INCOMPLETE
        elif incomplete_sites:
            exit_code = EXIT_INCOMPLETE
        else:
            exit_code = EXIT_OK
        return report, exit_code
    finally:
        store.close()


def verify_chain_report(db_path: str) -> tuple[dict[str, Any], int]:
    from .errors import E_CHAIN_BROKEN, EXIT_CHAIN_BROKEN, EXIT_OK

    store = AuditStore(db_path)
    try:
        failures = store.verify_chain()
        groups = store.grouped_revisions()
        affected = sorted({item["business_key"] for item in failures})
        report = {
            "business_keys_total": len(groups),
            "business_keys_failed": len(affected),
            "failures": failures,
            "error_codes": [E_CHAIN_BROKEN] if failures else [],
        }
        exit_code = EXIT_CHAIN_BROKEN if failures else EXIT_OK
        return report, exit_code
    finally:
        store.close()

