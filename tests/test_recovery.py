"""验收 6：采集中断恢复（可捕获异常 + Process.terminate 强杀）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from eicf.demo import build_demo_sites, write_demo_config
from eicf.storage import AuditStore

from .conftest import ROOT, parse_stdout_report, run_cli


def _baseline_master_count() -> int:
    from eicf.sample_data import baseline_unique_business_keys, build_baseline_dataset

    return baseline_unique_business_keys(build_baseline_dataset())


def test_interrupt_then_resume(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites, _ = build_demo_sites()
    try:
        write_demo_config(sites, "config.json")

        # 在 B 第 2 页提交后通过测试钩子中断
        interrupted = run_cli(
            workdir,
            "collect",
            "--config",
            "config.json",
            env_updates={
                "EICF_INTERRUPT_SITE": "B",
                "EICF_INTERRUPT_AFTER_PAGE": "2",
            },
        )
        assert interrupted.returncode == 2, interrupted.stderr
        assert "E_INTERRUPTED" in interrupted.stderr
        # 中断时不生成快照，仅依赖 SQLite 状态恢复
        assert not (workdir / "snapshots").exists()

        store = AuditStore("audit.db")
        state_b = store.get_endpoint_state("B")
        saved_cursor = state_b["last_cursor"]
        assert state_b["pages_fetched"] == 2
        assert saved_cursor is not None

        # 重启采集：从已提交游标之后继续
        resumed = run_cli(workdir, "collect", "--config", "config.json")
        assert resumed.returncode == 0, resumed.stderr
        report = parse_stdout_report(resumed)

        store2 = AuditStore("audit.db")
        assert store2.master_count() == _baseline_master_count()
        # 修订表无重复行
        import sqlite3

        conn = sqlite3.connect("audit.db")
        duplicate = conn.execute(
            "SELECT business_key, site_id, revision, content_hash, COUNT(*) c "
            "FROM revisions GROUP BY business_key, site_id, revision, "
            "content_hash HAVING c > 1"
        ).fetchall()
        assert duplicate == []
        # B 恢复后最终完成且水位走到末页
        assert store2.get_endpoint_state("B")["complete"] == 1
        assert report["incomplete_sites"] == []
    finally:
        for site in sites:
            site.stop()


def test_hard_terminate_recovers_from_sqlite_only(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites, _ = build_demo_sites()
    try:
        write_demo_config(sites, "config.json")

        # 用 Popen 启动后直接 terminate（不经过 finally 清理）
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        env["EICF_INTERRUPT_SITE"] = "B"
        env["EICF_INTERRUPT_AFTER_PAGE"] = "2"

        proc = subprocess.Popen(
            [sys.executable, "-m", "eicf.cli", "collect",
             "--config", "config.json"],
            cwd=str(workdir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # 等待 B 至少提交两页后强杀；轮询数据库水位
        import time

        deadline = time.time() + 20
        killed = False
        while time.time() < deadline:
            if Path("audit.db").exists():
                store = AuditStore("audit.db")
                row = store.get_endpoint_state("B")
                store.close()
                if row is not None and row["pages_fetched"] >= 2:
                    proc.terminate()  # 硬终止，无 finally 保证
                    killed = True
                    break
            time.sleep(0.05)
        assert killed, "未能在超时前触发强杀"
        proc.wait(timeout=20)

        # 仅凭 SQLite 已提交状态恢复
        resumed = run_cli(workdir, "collect", "--config", "config.json")
        assert resumed.returncode == 0, resumed.stderr
        store = AuditStore("audit.db")
        assert store.master_count() == _baseline_master_count()
        assert store.get_endpoint_state("B")["complete"] == 1
    finally:
        for site in sites:
            site.stop()

