"""验收 10：规范化报告字段、业务错误码与退出码约定。"""

from __future__ import annotations

import json

from eicf.demo import build_demo_sites, write_demo_config

from .conftest import parse_stdout_report, run_cli


def test_normal_report_conflicts_and_exit_zero(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites, _ = build_demo_sites()
    try:
        write_demo_config(sites, "config.json")
        proc = run_cli(workdir, "collect", "--config", "config.json")
        assert proc.returncode == 0
        report = parse_stdout_report(proc)
        # 构造数据中冲突业务键：SHARED（跨源两修订）与 S1（A 两修订）
        assert report["arbitration_conflicts"] == 2
        required = {
            "version",
            "per_site",
            "total_pages_fetched",
            "total_requests",
            "raw_seen_records",
            "distinct_revisions",
            "master_records",
            "arbitration_conflicts",
            "total_retries",
            "incomplete_sites",
            "snapshot_collection_hash",
            "error_codes",
        }
        assert required.issubset(report.keys())
        assert report["error_codes"] == []
        for site_id, stats in report["per_site"].items():
            assert "pages_fetched" in stats
            assert "requests" in stats
    finally:
        for site in sites:
            site.stop()


def test_missing_base_url_is_config_error(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    bad_config = {
        "global_max_inflight": 2,
        "max_retries": 3,
        "sites": [
            {"site_id": "A", "page_size_limit": 2},  # 缺 base_url
        ],
    }
    (workdir / "bad.json").write_text(
        json.dumps(bad_config), encoding="utf-8"
    )
    proc = run_cli(workdir, "collect", "--config", "bad.json")
    assert proc.returncode == 4
    assert "E_CONFIG_INVALID" in proc.stderr


def test_missing_site_id_is_config_error(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    bad_config = {"sites": [{"base_url": "http://127.0.0.1:1"}]}
    (workdir / "bad2.json").write_text(
        json.dumps(bad_config), encoding="utf-8"
    )
    proc = run_cli(workdir, "collect", "--config", "bad2.json")
    assert proc.returncode == 4
    assert "E_CONFIG_INVALID" in proc.stderr

