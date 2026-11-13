"""验收 4/5：整库快照原子性、集合哈希、版本化差异与顺序无关。"""

from __future__ import annotations

import json

from eicf.demo import build_demo_sites, write_demo_config
from eicf.sample_data import build_increment_for_site_a
from eicf.storage import AuditStore

from .conftest import parse_stdout_report, run_cli


def test_snapshot_deterministic_and_atomic(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    import os

    os.environ["EICF_FIXED_NOW"] = "2024-01-01T00:00:00Z"
    sites, _ = build_demo_sites()
    try:
        write_demo_config(sites, "config.json")
        proc1 = run_cli(workdir, "collect", "--config", "config.json")
        assert proc1.returncode == 0
        manifest1 = (workdir / "snapshots/v1/manifest.json").read_bytes()
        records1 = (workdir / "snapshots/v1/records.json").read_bytes()

        # 第二次快照与第一次逐字节一致（同版本内容、确定性时间戳）
        # 通过直接重建第二版快照验证哈希稳定性
        store = AuditStore("audit.db")
        from eicf.snapshots import SnapshotManager

        manager = SnapshotManager(store, "snapshots")
        result2 = manager.create_snapshot()
        records2 = (result2.path / "records.json").read_bytes()
        # 版本号不同，其余记录内容与集合哈希一致：比较去除版本后的集合哈希
        assert result2.collection_hash == json.loads(manifest1)["collection_hash"]
        assert records2 == records1
    finally:
        for site in sites:
            site.stop()


def test_replace_failure_keeps_old_snapshot_intact(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites, _ = build_demo_sites()
    try:
        write_demo_config(sites, "config.json")
        proc = run_cli(workdir, "collect", "--config", "config.json")
        assert proc.returncode == 0
        old_manifest = (workdir / "snapshots/v1/manifest.json").read_bytes()
        old_records = (workdir / "snapshots/v1/records.json").read_bytes()

        store = AuditStore("audit.db")
        from eicf.errors import SnapshotReplaceError
        from eicf.snapshots import SnapshotManager

        manager = SnapshotManager(
            store, "snapshots", fail_before_replace=True
        )
        try:
            manager.create_snapshot()
            raise AssertionError("应当抛出替换异常")
        except SnapshotReplaceError:
            pass

        # 旧快照完整可读
        assert (workdir / "snapshots/v1/manifest.json").read_bytes() == old_manifest
        assert (workdir / "snapshots/v1/records.json").read_bytes() == old_records
        # 不存在半成品临时目录
        leftovers = [
            entry.name
            for entry in (workdir / "snapshots").iterdir()
            if entry.name.startswith(".tmp")
        ]
        assert leftovers == []
    finally:
        for site in sites:
            site.stop()


def test_versioned_diff_add_revise_withdraw(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites, _ = build_demo_sites()
    try:
        write_demo_config(sites, "config.json")
        proc1 = run_cli(workdir, "collect", "--config", "config.json")
        assert proc1.returncode == 0
        report1 = parse_stdout_report(proc1)
        assert report1["version"] == 1

        # 站点 A 追加增量：新增 2、更正 1、撤回 1
        increment = build_increment_for_site_a()["A"]
        site_a = next(s for s in sites if s.site_id == "A")
        site_a.records.extend(increment)

        proc2 = run_cli(workdir, "collect", "--config", "config.json")
        assert proc2.returncode == 0, proc2.stderr
        report2 = parse_stdout_report(proc2)
        assert report2["version"] == 2
        assert report2["diff_summary"] == {
            "added": 2,
            "revised": 1,
            "withdrawn": 1,
        }

        manifest2 = json.loads(
            (workdir / "snapshots/v2/manifest.json").read_text(encoding="utf-8")
        )
        # 站点 A 水位前进（max_signed_at 增大、页数增加）
        wm_a = manifest2["watermarks"]["A"]
        manifest1 = json.loads(
            (workdir / "snapshots/v1/manifest.json").read_text(encoding="utf-8")
        )
        assert wm_a["max_signed_at"] >= manifest1["watermarks"]["A"]["max_signed_at"]
        assert wm_a["pages_fetched"] > manifest1["watermarks"]["A"]["pages_fetched"]

        # 集合哈希相对 v1 发生变化
        assert (
            manifest2["collection_hash"]
            != manifest1["collection_hash"]
        )
    finally:
        for site in sites:
            site.stop()


def test_diff_independent_of_record_file_order(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites, _ = build_demo_sites()
    try:
        write_demo_config(sites, "config.json")
        run_cli(workdir, "collect", "--config", "config.json")

        # 打乱 v1 records.json 中记录顺序（保持 JSON 合法）
        records_path = workdir / "snapshots/v1/records.json"
        view = json.loads(records_path.read_text(encoding="utf-8"))
        shuffled = list(reversed(view))
        records_path.write_text(
            json.dumps(shuffled, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        increment = build_increment_for_site_a()["A"]
        next(s for s in sites if s.site_id == "A").records.extend(increment)
        proc = run_cli(workdir, "collect", "--config", "config.json")
        assert proc.returncode == 0, proc.stderr
        report = parse_stdout_report(proc)
        # 差异按业务键匹配，与 v1 文件内顺序无关
        assert report["diff_summary"] == {
            "added": 2,
            "revised": 1,
            "withdrawn": 1,
        }
    finally:
        for site in sites:
            site.stop()

