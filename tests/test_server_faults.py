"""验收 7：429/500/截断 JSON/超时的重试、退避与不完整站点隔离。"""

from __future__ import annotations

import json

from eicf.sample_data import BASE_TS, rec
from eicf.simulator import GlobalInflight, SimSite

from .conftest import parse_stdout_report, run_cli, write_config


def _make_site(site_id: str, count: int = 3, page_size: int = 1) -> SimSite:
    records = [
        rec(site_id, f"{site_id}{j}", "v1", BASE_TS + j, "signed", 1, f"h{j}")
        for j in range(count)
    ]
    return SimSite(site_id, records, page_size=page_size)


def test_500_twice_then_success(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    site_c = _make_site("C", count=1)
    # 第 1 页前两次 500，第三次成功
    site_c.faults = {0: ["http_500", "http_500", None]}
    shared = GlobalInflight()
    site_c.start(shared)
    try:
        write_config(
            workdir / "config.json", [site_c], max_retries=3,
            retry_backoff_seconds=0,
        )
        proc = run_cli(workdir, "collect", "--config", "config.json")
        assert proc.returncode == 0, proc.stderr
        report = parse_stdout_report(proc)
        # 该页请求次数为 3
        assert site_c.page_request_counts[0] == 3
        assert report["total_retries"] == 2
        assert report["incomplete_sites"] == []
    finally:
        site_c.stop()


def test_truncated_json_is_retried(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    site = _make_site("T")
    site.faults = {0: ["truncated", None]}
    shared = GlobalInflight()
    site.start(shared)
    try:
        write_config(workdir / "config.json", [site], max_retries=3)
        proc = run_cli(workdir, "collect", "--config", "config.json")
        assert proc.returncode == 0, proc.stderr
        assert site.page_request_counts[0] == 2
    finally:
        site.stop()


def test_site_d_always_429_marks_incomplete(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    site_a = _make_site("A", count=2)
    site_d = _make_site("D", count=2)
    # D 所有请求都 429
    site_d.faults = {0: ["http_429"], 1: ["http_429"]}
    shared = GlobalInflight()
    for site in (site_a, site_d):
        site.start(shared)
    try:
        write_config(
            workdir / "config.json",
            [site_a, site_d],
            max_retries=3,
            retry_backoff_seconds=0,
            request_timeout_seconds=2.0,
        )
        proc = run_cli(workdir, "collect", "--config", "config.json")
        assert proc.returncode == 2
        report = parse_stdout_report(proc)
        assert "E_SITE_INCOMPLETE" in report["error_codes"]
        assert report["incomplete_sites"] == ["D"]

        manifest = json.loads(
            (workdir / "snapshots/v1/manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["incomplete"] is True
        assert "D" in manifest["incomplete_sites"]

        # A 成功记录正常入库；D 的部分修订不进入快照记录集
        records_view = json.loads(
            (workdir / "snapshots/v1/records.json").read_text(encoding="utf-8")
        )
        snapshot_site_ids = {item["winner"]["site_id"] for item in records_view}
        assert "D" not in snapshot_site_ids
        assert "A" in snapshot_site_ids
    finally:
        for site in (site_a, site_d):
            site.stop()
def test_timeout_exhausts_retries(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    site = _make_site("E", count=1)
    # 慢响应时长远大于客户端超时，保证在调度抖动下也稳定判定为超时
    site.slow_seconds = 1.0
    site.faults = {0: ["timeout"]}
    shared = GlobalInflight()
    site.start(shared)
    try:
        write_config(
            workdir / "config.json",
            [site],
            max_retries=2,
            retry_backoff_seconds=0,
            request_timeout_seconds=0.5,
        )
        proc = run_cli(workdir, "collect", "--config", "config.json")
        assert proc.returncode == 2
        report = parse_stdout_report(proc)
        assert report["incomplete_sites"] == ["E"]
        # 首次 + 2 次重试 = 3 次请求
        assert site.page_request_counts[0] == 3
    finally:
        site.stop()

