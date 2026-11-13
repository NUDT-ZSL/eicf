"""验收 1/2/9：并发上限、游标推进、跨源去重、幂等重跑。"""

from __future__ import annotations

from eicf.demo import build_demo_sites, write_demo_config
from eicf.sample_data import baseline_unique_business_keys, build_baseline_dataset
from eicf.storage import AuditStore

from .conftest import parse_stdout_report, run_cli, write_config


def test_demo_three_sites_union_and_cursor(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites, inflight = build_demo_sites()
    try:
        write_demo_config(sites, "config.json")
        proc = run_cli(workdir, "collect", "--config", "config.json")
        assert proc.returncode == 0, proc.stderr
        report = parse_stdout_report(proc)

        dataset = build_baseline_dataset()
        expected_keys = baseline_unique_business_keys(dataset)
        store = AuditStore("audit.db")
        assert store.master_count() == expected_keys
        assert report["master_records"] == expected_keys

        # 服务端权威计数：峰值 > 1（并发生效）且 <= 配置上限 3
        assert 1 < inflight.peak <= 3

        # B 第 4 页因重复 next_cursor 被重取一次（服务端计数为准）
        site_b = next(s for s in sites if s.site_id == "B")
        assert site_b.page_request_counts[3] == 2

        # 游标严格按服务端 next_cursor 推进：末页 last_cursor 为 None
        state_b = store.get_endpoint_state("B")
        # B 共 4 个数据页；重取页也产生一次提交，故 pages_fetched=5
        assert state_b["pages_fetched"] == 5
        assert state_b["last_cursor"] is None
        assert state_b["complete"] == 1
    finally:
        for site in sites:
            site.stop()


def test_global_inflight_ceiling_from_server_counter(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    from eicf.simulator import SimSite
    from eicf.simulator import GlobalInflight
    from eicf.sample_data import rec, BASE_TS

    # 4 个站点 + 慢响应，制造足够重叠；全局上限 2
    sites = []
    for idx, site_id in enumerate(["P", "Q", "R", "S"]):
        records = [
            rec(site_id, f"X{j}", "v1", BASE_TS + j, "signed", 1, f"h{idx}{j}")
            for j in range(3)
        ]
        site = SimSite(site_id, records, page_size=1, slow_seconds=0.15)
        site.faults = {page: ["slow"] for page in range(3)}
        sites.append(site)

    shared = GlobalInflight()
    for site in sites:
        site.start(shared)
    try:
        write_config(
            workdir / "config.json",
            sites,
            global_max_inflight=2,
            request_timeout_seconds=5.0,
        )
        proc = run_cli(workdir, "collect", "--config", "config.json")
        assert proc.returncode == 0, proc.stderr
        # 峰值来源是服务端计数，而非客户端自报
        assert shared.peak == 2
    finally:
        for site in sites:
            site.stop()


def test_cross_source_dedup_and_idempotent_rerun(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites, _ = build_demo_sites()
    try:
        write_demo_config(sites, "config.json")
        proc1 = run_cli(workdir, "collect", "--config", "config.json")
        assert proc1.returncode == 0
        store = AuditStore("audit.db")

        # SHARED 业务键：A/C 两条仅哈希不同 -> 1 行主记录、2 条修订
        from eicf.models import Record

        shared_key = Record.business_key_to_str(
            ("A", "SHARED", "v1.0", 1_700_000_100)
        )
        import sqlite3

        conn = sqlite3.connect("audit.db")
        master = conn.execute(
            "SELECT COUNT(DISTINCT business_key) FROM revisions "
            "WHERE business_key=?",
            (shared_key,),
        ).fetchone()[0]
        revisions = conn.execute(
            "SELECT COUNT(*) FROM revisions WHERE business_key=?",
            (shared_key,),
        ).fetchone()[0]
        hashes = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT content_hash FROM revisions "
                "WHERE business_key=?",
                (shared_key,),
            )
        }
        assert master == 1
        assert revisions == 2
        assert hashes == {"hA0", "hC0"}

        revisions_before = store.count_revisions()
        masters_before = store.master_count()

        # 幂等重跑：仅抓取时刻不同、业务键完全相同 -> 不新增修订/主记录
        proc2 = run_cli(workdir, "collect", "--config", "config.json")
        assert proc2.returncode == 0
        report2 = parse_stdout_report(proc2)
        assert store.count_revisions() == revisions_before
        assert store.master_count() == masters_before
        assert report2["raw_seen_records"] > 0
        assert report2["distinct_revisions"] == revisions_before
    finally:
        for site in sites:
            site.stop()


def test_teardown_releases_port(baseline_sites):
    # baseline_sites fixture 停止后端口应可再次绑定
    import socket

    sites, _ = baseline_sites
    port = sites[0].httpd.server_address[1]
    for site in sites:
        site.stop()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))  # 不抛异常即说明已释放

