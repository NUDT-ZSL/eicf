"""SQLite 持久化：修订入库、链式哈希、水位、仲裁视图、链校验。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import (
    BUSINESS_KEY_FIELDS,
    Record,
    arbitration_sort_key,
    canonical_json,
    effective_status,
    sha256_text,
)

GENESIS_HASH = "0" * 64


class AuditStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：采集器跨线程使用，内部用锁串行化写入。
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS revisions (
                    business_key     TEXT NOT NULL,
                    seq              INTEGER NOT NULL,
                    site_id          TEXT NOT NULL,
                    subject_id       TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    signed_at        INTEGER NOT NULL,
                    status           TEXT NOT NULL,
                    revision         INTEGER NOT NULL,
                    content_hash     TEXT NOT NULL,
                    record_hash      TEXT NOT NULL,
                    prev_chain_hash  TEXT NOT NULL,
                    chain_hash       TEXT NOT NULL,
                    UNIQUE(business_key, site_id, revision, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_revisions_key
                    ON revisions(business_key, seq);

                CREATE TABLE IF NOT EXISTS endpoint_state (
                    endpoint_id        TEXT PRIMARY KEY,
                    last_cursor        TEXT,
                    max_signed_at      INTEGER NOT NULL DEFAULT 0,
                    pages_fetched      INTEGER NOT NULL DEFAULT 0,
                    requests           INTEGER NOT NULL DEFAULT 0,
                    retries            INTEGER NOT NULL DEFAULT 0,
                    complete           INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at        TEXT NOT NULL,
                    finished_at       TEXT,
                    exit_code         INTEGER,
                    report_json       TEXT
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    version           INTEGER PRIMARY KEY,
                    created_at        TEXT NOT NULL,
                    incomplete        INTEGER NOT NULL,
                    collection_hash   TEXT NOT NULL,
                    manifest_json     TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def ensure_schema(self) -> None:
        """显式确保 schema 已建立（采集线程启动前在主线程调用）。"""
        self._init_schema()

    # ---- 链哈希 -----------------------------------------------------
    @staticmethod
    def compute_chain_hash(prev_chain_hash: str, record: Record) -> str:
        payload = prev_chain_hash + "|" + record.canonical()
        return sha256_text(payload)

    # ---- 页提交（单事务） ------------------------------------------
    def commit_page(
        self,
        endpoint_id: str,
        records: Iterable[Record],
        last_cursor: str | None,
    ) -> int:
        """在同一事务内写入本页全部新修订并推进该端点水位。

        返回本页新插入的修订条数（已存在的修订会被去重，不重复写入）。
        """
        records = list(records)
        inserted = 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                for record in records:
                    business_key = Record.business_key_to_str(record.business_key())
                    existing = cur.execute(
                        "SELECT 1 FROM revisions WHERE business_key=? "
                        "AND site_id=? AND revision=? AND content_hash=?",
                        (
                            business_key,
                            record.site_id,
                            record.revision,
                            record.content_hash,
                        ),
                    ).fetchone()
                    if existing is not None:
                        continue
                    row = cur.execute(
                        "SELECT COALESCE(MAX(seq), -1) AS max_seq, "
                        "COALESCE((SELECT chain_hash FROM revisions "
                        "WHERE business_key=? ORDER BY seq DESC LIMIT 1), ?) "
                        "AS prev_hash FROM revisions",
                        (business_key, GENESIS_HASH),
                    ).fetchone()
                    next_seq = row["max_seq"] + 1
                    prev_hash = row["prev_hash"]
                    record_hash = record.record_hash()
                    chain_hash = self.compute_chain_hash(prev_hash, record)
                    cur.execute(
                        "INSERT INTO revisions (business_key, seq, site_id, "
                        "subject_id, protocol_version, signed_at, status, "
                        "revision, content_hash, record_hash, prev_chain_hash, "
                        "chain_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            business_key,
                            next_seq,
                            record.site_id,
                            record.subject_id,
                            record.protocol_version,
                            record.signed_at,
                            record.status,
                            record.revision,
                            record.content_hash,
                            record_hash,
                            prev_hash,
                            chain_hash,
                        ),
                    )
                    inserted += 1

                max_signed_at = max(
                    [record.signed_at for record in records] or [0]
                )
                cur.execute(
                    "INSERT INTO endpoint_state (endpoint_id, last_cursor, "
                    "max_signed_at, pages_fetched) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(endpoint_id) DO UPDATE SET "
                    "last_cursor=excluded.last_cursor, "
                    "max_signed_at=MAX(endpoint_state.max_signed_at, "
                    "excluded.max_signed_at), "
                    "pages_fetched=endpoint_state.pages_fetched+1",
                    (endpoint_id, last_cursor, max_signed_at),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return inserted

    # ---- 端点水位与统计 --------------------------------------------
    def get_endpoint_state(self, endpoint_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM endpoint_state WHERE endpoint_id=?",
                (endpoint_id,),
            ).fetchone()

    def get_resume_cursor(self, endpoint_id: str) -> str | None:
        row = self.get_endpoint_state(endpoint_id)
        return None if row is None else row["last_cursor"]

    def add_request(self, endpoint_id: str, retries: int) -> None:
        """累计一次逻辑请求：requests +1，retries 累加实际失败重试次数。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO endpoint_state (endpoint_id, requests, retries) "
                "VALUES (?, 1, ?) ON CONFLICT(endpoint_id) DO UPDATE SET "
                "requests=requests+1, retries=retries+?",
                (endpoint_id, retries, retries),
            )
            self._conn.commit()

    def mark_complete(self, endpoint_id: str, complete: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO endpoint_state (endpoint_id, complete) "
                "VALUES (?, ?) ON CONFLICT(endpoint_id) DO UPDATE SET "
                "complete=?",
                (endpoint_id, 1 if complete else 0, 1 if complete else 0),
            )
            self._conn.commit()

    def all_endpoint_states(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM endpoint_state ORDER BY endpoint_id"
                ).fetchall()
            )

    def reset_endpoint_completion(self, endpoint_ids: Iterable[str]) -> None:
        """新一轮采集开始时重置完成标记（水位/游标保留用于恢复）。"""
        with self._lock:
            for endpoint_id in endpoint_ids:
                self._conn.execute(
                    "INSERT INTO endpoint_state (endpoint_id, complete) "
                    "VALUES (?, 0) ON CONFLICT(endpoint_id) DO UPDATE SET "
                    "complete=0",
                    (endpoint_id,),
                )
            self._conn.commit()

    # ---- 查询 -------------------------------------------------------
    def all_revisions(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM revisions ORDER BY business_key, seq"
                ).fetchall()
            )

    def count_revisions(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS c FROM revisions"
            ).fetchone()["c"]

    def grouped_revisions(self) -> dict[str, list[Record]]:
        groups: dict[str, list[Record]] = {}
        for row in self.all_revisions():
            record = self._row_to_record(row)
            groups.setdefault(row["business_key"], []).append(record)
        return groups

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> Record:
        return Record(
            site_id=row["site_id"],
            subject_id=row["subject_id"],
            protocol_version=row["protocol_version"],
            signed_at=row["signed_at"],
            status=row["status"],
            revision=row["revision"],
            content_hash=row["content_hash"],
        )

    def current_view(self) -> list[dict[str, Any]]:
        """全库当前有效视图（仲裁 + 有效状态），按业务键排序。"""
        view: list[dict[str, Any]] = []
        groups = self.grouped_revisions()
        for business_key in sorted(groups):
            records = groups[business_key]
            winner = min(records, key=arbitration_sort_key)
            view.append(
                {
                    "business_key": json_loads_key(business_key),
                    "effective_status": effective_status(records),
                    "winner": winner.to_dict(),
                    "revision_count": len(records),
                }
            )
        return view

    def master_count(self) -> int:
        return len(self.grouped_revisions())

    # ---- 链校验 -----------------------------------------------------
    def verify_chain(self) -> list[dict[str, str]]:
        """遍历每个业务键的修订链，返回失败明细（单键失败不影响其他键）。"""
        failures: list[dict[str, str]] = []
        with self._lock:
            keys = [
                row["business_key"]
                for row in self._conn.execute(
                    "SELECT DISTINCT business_key FROM revisions "
                    "ORDER BY business_key"
                ).fetchall()
            ]
            for business_key in keys:
                rows = self._conn.execute(
                    "SELECT * FROM revisions WHERE business_key=? ORDER BY seq",
                    (business_key,),
                ).fetchall()
                failures.extend(self._verify_one_chain(business_key, rows))
        return failures

    def _verify_one_chain(
        self, business_key: str, rows: list[sqlite3.Row]
    ) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        prev_hash = GENESIS_HASH
        seen_seq: set[int] = set()
        for row in rows:
            # seq 仅用于排序；跨运行去重跳号是合法的，不能据此判失败，
            # 但同一链内 seq 重复/乱序意味着插入异常。
            if row["seq"] in seen_seq:
                failures.append(
                    {
                        "business_key": business_key,
                        "failure_type": "revision_inserted_or_deleted",
                        "detail": f"seq 重复: {row['seq']}",
                    }
                )
            seen_seq.add(row["seq"])
            if row["prev_chain_hash"] != prev_hash:
                failures.append(
                    {
                        "business_key": business_key,
                        "failure_type": "revision_inserted_or_deleted",
                        "detail": "prev_chain_hash 与上一修订不衔接（修订被删除或插入）",
                    }
                )
            record = self._row_to_record(row)
            if record.record_hash() != row["record_hash"]:
                failures.append(
                    {
                        "business_key": business_key,
                        "failure_type": "field_tampered",
                        "detail": "record_hash 与规范化字段不一致",
                    }
                )
            recomputed = self.compute_chain_hash(prev_hash, record)
            if recomputed != row["chain_hash"]:
                failures.append(
                    {
                        "business_key": business_key,
                        "failure_type": "field_tampered",
                        "detail": "chain_hash 重算不一致",
                    }
                )
            prev_hash = row["chain_hash"]
        return failures

    # ---- 快照元数据 -------------------------------------------------
    def latest_snapshot_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM snapshots"
            ).fetchone()
            return int(row["v"])

    def save_snapshot(
        self, version: int, created_at: str, incomplete: bool,
        collection_hash: str, manifest: dict[str, Any],
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots (version, created_at, incomplete, "
                "collection_hash, manifest_json) VALUES (?,?,?,?,?)",
                (
                    version,
                    created_at,
                    1 if incomplete else 0,
                    collection_hash,
                    canonical_json(manifest),
                ),
            )
            self._conn.commit()

    def start_run(self, started_at: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (started_at) VALUES (?)", (started_at,)
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, finished_at: str, exit_code: int, report: dict[str, Any]
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at=?, exit_code=?, report_json=? "
                "WHERE run_id=?",
                (finished_at, exit_code, canonical_json(report), run_id),
            )
            self._conn.commit()


def json_loads_key(business_key: str) -> dict[str, Any]:
    import json

    return json.loads(business_key)


def business_key_tuple_from_dict(key_dict: dict[str, Any]) -> tuple:
    return tuple(key_dict[field] for field in BUSINESS_KEY_FIELDS)

