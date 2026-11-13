"""采集配置的读取与校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    base_url: str
    page_size_limit: int


@dataclass(frozen=True)
class CollectorConfig:
    sites: list[SiteConfig]
    global_max_inflight: int
    max_retries: int
    retry_backoff_seconds: float
    request_timeout_seconds: float
    db_path: str
    snapshot_dir: str
    extra: dict[str, Any] = field(default_factory=dict)


def load_config(path: str | Path) -> CollectorConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是 JSON 对象")

    sites_raw = raw.get("sites")
    if not isinstance(sites_raw, list) or not sites_raw:
        raise ConfigError("sites 必须是非空数组")

    sites: list[SiteConfig] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(sites_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"sites[{index}] 必须是对象")
        site_id = item.get("site_id")
        base_url = item.get("base_url")
        if not isinstance(site_id, str) or not site_id.strip():
            raise ConfigError(f"sites[{index}] 缺少站点编号 site_id")
        # 关键校验：缺少基址必须报 E_CONFIG_INVALID
        if not isinstance(base_url, str) or not base_url.strip():
            raise ConfigError(f"站点 {site_id!r} 缺少基址 base_url")
        if site_id in seen_ids:
            raise ConfigError(f"站点编号重复: {site_id}")
        seen_ids.add(site_id)
        page_size_limit = item.get("page_size_limit", 100)
        if not isinstance(page_size_limit, int) or page_size_limit <= 0:
            raise ConfigError(f"站点 {site_id} 的 page_size_limit 必须是正整数")
        sites.append(
            SiteConfig(
                site_id=site_id,
                base_url=base_url.rstrip("/"),
                page_size_limit=page_size_limit,
            )
        )

    global_max_inflight = raw.get("global_max_inflight", 4)
    if not isinstance(global_max_inflight, int) or global_max_inflight <= 0:
        raise ConfigError("global_max_inflight 必须是正整数")

    max_retries = raw.get("max_retries", 3)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ConfigError("max_retries 必须是非负整数")

    retry_backoff_seconds = raw.get("retry_backoff_seconds", 0)
    if not isinstance(retry_backoff_seconds, (int, float)) or retry_backoff_seconds < 0:
        raise ConfigError("retry_backoff_seconds 必须是非负数字")

    request_timeout_seconds = raw.get("request_timeout_seconds", 5.0)
    if (
        not isinstance(request_timeout_seconds, (int, float))
        or request_timeout_seconds <= 0
    ):
        raise ConfigError("request_timeout_seconds 必须是正数")

    db_path = raw.get("db_path", "eicf_audit.db")
    snapshot_dir = raw.get("snapshot_dir", "snapshots")
    if not isinstance(db_path, str) or not db_path.strip():
        raise ConfigError("db_path 非法")
    if not isinstance(snapshot_dir, str) or not snapshot_dir.strip():
        raise ConfigError("snapshot_dir 非法")

    known = {
        "sites",
        "global_max_inflight",
        "max_retries",
        "retry_backoff_seconds",
        "request_timeout_seconds",
        "db_path",
        "snapshot_dir",
    }
    extra = {key: value for key, value in raw.items() if key not in known}

    return CollectorConfig(
        sites=sites,
        global_max_inflight=global_max_inflight,
        max_retries=max_retries,
        retry_backoff_seconds=float(retry_backoff_seconds),
        request_timeout_seconds=float(request_timeout_seconds),
        db_path=db_path,
        snapshot_dir=snapshot_dir,
        extra=extra,
    )

