"""验收 8：链式哈希不可篡改校验（改写/删除/插入检测、单键失败不中断）。"""

from __future__ import annotations

import json
import sqlite3

from eicf.demo import build_demo_sites, write_demo_config
from .conftest import run_cli


def _collect(workdir):
    sites, _ = build_demo_sites()
    write_demo_config(sites, "config.json")
    proc = run_cli(workdir, "collect", "--config", "config.json")
    assert proc.returncode == 0, proc.stderr
    return sites


def test_clean_chain_passes(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites = _collect(workdir)
    try:
        proc = run_cli(workdir, "verify", "--config", "config.json")
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        assert report["business_keys_failed"] == 0
        assert report["error_codes"] == []
    finally:
        for site in sites:
            site.stop()


def test_field_tamper_detected_with_business_key(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites = _collect(workdir)
    try:
        conn = sqlite3.connect("audit.db")
        conn.row_factory = sqlite3.Row
        target = conn.execute(
            "SELECT business_key FROM revisions LIMIT 1"
        ).fetchone()["business_key"]
        # 直接改写签署状态字段（绕过应用层）
        conn.execute(
            "UPDATE revisions SET status='withdrawn' WHERE business_key=?",
            (target,),
        )
        conn.commit()
        conn.close()

        proc = run_cli(workdir, "verify", "--config", "config.json")
        assert proc.returncode == 3
        assert "E_CHAIN_BROKEN" in proc.stderr
        assert target in proc.stderr
        assert "field_tampered" in proc.stderr
        # 不输出崩溃堆栈
        assert "Traceback" not in proc.stderr

        report = json.loads(proc.stdout.strip().splitlines()[-1])
        assert report["business_keys_failed"] >= 1
    finally:
        for site in sites:
            site.stop()


def test_middle_revision_delete_detected_and_others_audited(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    sites = _collect(workdir)
    try:
        conn = sqlite3.connect("audit.db")
        conn.row_factory = sqlite3.Row
        # 找一个有 2 条修订的业务键，删除其链首修订（seq 最小）。
        # 剩余修订的 prev_chain_hash 将不再等于创世哈希，从而被检测为删除。
        multi = conn.execute(
            "SELECT business_key FROM revisions GROUP BY business_key "
            "HAVING COUNT(*) >= 2 LIMIT 1"
        ).fetchone()
        assert multi is not None
        target_key = multi["business_key"]
        head_seq = conn.execute(
            "SELECT seq FROM revisions WHERE business_key=? ORDER BY seq",
            (target_key,),
        ).fetchall()[0]["seq"]
        conn.execute(
            "DELETE FROM revisions WHERE business_key=? AND seq=?",
            (target_key, head_seq),
        )
        conn.commit()
        total_keys = conn.execute(
            "SELECT COUNT(DISTINCT business_key) FROM revisions"
        ).fetchone()[0]
        conn.close()

        proc = run_cli(workdir, "verify", "--config", "config.json")
        assert proc.returncode == 3
        assert target_key in proc.stderr
        assert (
            "revision_deleted_or_inserted" in proc.stderr
            or "revision_inserted_or_deleted" in proc.stderr
        )

        report = json.loads(proc.stdout.strip().splitlines()[-1])
        # 其余未受影响业务键仍被审计并纳入汇总（不因前一条失败而跳过）
        assert report["business_keys_total"] == total_keys
        affected = {
            failure["business_key"] for failure in report["failures"]
        }
        assert target_key in affected
    finally:
        for site in sites:
            site.stop()

