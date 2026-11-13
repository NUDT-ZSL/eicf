"""确定性三站点演示构造（任务验收第 1 条场景）。

站点 B：
* 第 2 页返回与第 1 页相同的数据，但 next_cursor 不同（正常推进）；
* 第 4 页首次返回与第 3 页相同的 next_cursor（触发客户端重取一次），
  重取时恢复正常。
"""

from __future__ import annotations

import json
from pathlib import Path

from .sample_data import build_baseline_dataset
from .simulator import (
    GlobalInflight,
    PageBehavior,
    SimSite,
    _LazyBehavior,
    start_sites,
)


def _build_site(site_id: str, records, page_size: int) -> SimSite:
    return SimSite(site_id=site_id, records=list(records), page_size=page_size)


def build_demo_sites() -> tuple[list[SimSite], GlobalInflight]:
    dataset = build_baseline_dataset()
    # A:6 条（含 SHARED、重复 S2），B:4 条，C:4 条
    site_a = _build_site("A", dataset["A"], page_size=2)  # 3 页
    site_b = _build_site("B", dataset["B"], page_size=1)  # 4 页
    site_c = _build_site("C", dataset["C"], page_size=2)  # 2 页

    # 固定慢响应（首页）以保证不同站点线程在服务端确定性重叠，
    # 使服务端权威并发峰值稳定 > 1；时长固定，不引入随机。
    for site in (site_a, site_b, site_c):
        site.slow_seconds = 0.15
        site.faults[0] = ["slow"]

    # B 第 2 页（page_index=1）：返回与第 1 页相同的数据，游标正常不同
    site_b.behaviors[1] = [PageBehavior(record_offset=0)]

    # B 第 4 页（page_index=3）：首次返回与第 3 页相同的 next_cursor。
    # 该游标指向偏移 3，客户端重取一次后第二次行为恢复默认（末页 has_more=false）。
    def page4_first(behavior_index: int, site: SimSite, normal_next, offset):
        reused = site.register_cursor(3)
        # 首次：返回指向偏移 3 的重复游标且声明还有下一页，
        # 客户端据此重取一次；第二次走默认 PageBehavior 返回末页。
        return PageBehavior(next_cursor=reused, has_more=True)

    # 首项为延迟工厂，第二项为默认行为（重取恢复正常）
    site_b.behaviors[3] = [_LazyBehavior(page4_first), PageBehavior()]

    sites = [site_a, site_b, site_c]
    _, inflight = start_sites(sites)
    return sites, inflight

def write_demo_config(sites: list[SimSite], path: str) -> None:
    config = {
        "global_max_inflight": 3,
        "max_retries": 3,
        "retry_backoff_seconds": 0,
        "request_timeout_seconds": 5.0,
        "db_path": "audit.db",
        "snapshot_dir": "snapshots",
        "sites": [
            {
                "site_id": site.site_id,
                "base_url": f"http://127.0.0.1:{site.httpd.server_address[1]}",
                "page_size_limit": site.page_size,
            }
            for site in sites
        ],
    }
    Path(path).write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )

