"""eICF 签署记录的数据模型与确定性规范化。

业务主键严格限定为：站点编号、受试者编号、方案版本、签署时间（秒级 UTC
整数时间戳）。不得包含采集批次、来源 URL、抓取时刻、内容哈希等字段。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

# 业务主键字段（顺序即规范化顺序，保持稳定）
BUSINESS_KEY_FIELDS = ("site_id", "subject_id", "protocol_version", "signed_at")

# 参与链式哈希的规范化记录字段（业务键 + 业务内容，不含采集元数据）
RECORD_FIELDS = (
    "site_id",
    "subject_id",
    "protocol_version",
    "signed_at",
    "status",
    "revision",
    "content_hash",
)

VALID_STATUS = ("signed", "withdrawn")


def canonical_json(obj: Any) -> str:
    """键排序、无空白差异的规范化 JSON 字符串（ensure_ascii=False 保持确定）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Record:
    """一条 eICF 签署事件投递（某个业务键的一个修订）。"""

    site_id: str
    subject_id: str
    protocol_version: str
    signed_at: int
    status: str
    revision: int
    content_hash: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Record":
        try:
            record = cls(
                site_id=str(payload["site_id"]),
                subject_id=str(payload["subject_id"]),
                protocol_version=str(payload["protocol_version"]),
                signed_at=int(payload["signed_at"]),
                status=str(payload["status"]),
                revision=int(payload["revision"]),
                content_hash=str(payload["content_hash"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"非法记录字段: {payload!r}") from exc
        if record.status not in VALID_STATUS:
            raise ValueError(f"非法签署状态: {record.status!r}")
        if record.revision < 0:
            raise ValueError(f"修订号必须为非负整数: {record.revision}")
        return record

    def business_key(self) -> tuple[str, str, str, int]:
        return (
            self.site_id,
            self.subject_id,
            self.protocol_version,
            self.signed_at,
        )

    @staticmethod
    def business_key_to_str(key: tuple[str, str, str, int]) -> str:
        return canonical_json(
            {
                "site_id": key[0],
                "subject_id": key[1],
                "protocol_version": key[2],
                "signed_at": key[3],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in RECORD_FIELDS}

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    def record_hash(self) -> str:
        """单条记录规范化内容哈希。"""
        return sha256_text(self.canonical())


def arbitration_sort_key(record: Record) -> tuple[int, list[Any]]:
    """仲裁排序键：修订号降序、内容哈希字典序升序、站点编号字典序升序。

    返回可直接用于 ``min`` 的元组（修订号取负实现降序）。
    """
    return (
        -record.revision,
        [record.content_hash, record.site_id],
    )


def effective_status(records: list[Record]) -> str:
    """计算业务键当前有效状态。

  最高修订号决定状态：若最高修订号上同时存在 signed 与 withdrawn，则
    withdrawn 仅在其修订号不低于任何 signed 修订号时生效；存在更高修订号的
    signed 修订时以 signed 为准。
    """
    max_revision = max(record.revision for record in records)
    has_signed_at_max = any(
        record.revision == max_revision and record.status == "signed"
        for record in records
    )
    has_withdrawn_at_max = any(
        record.revision == max_revision and record.status == "withdrawn"
        for record in records
    )
    if has_signed_at_max:
        return "signed"
    if has_withdrawn_at_max:
        return "withdrawn"
    # 理论上不可达：最高修订号必然有记录
    raise AssertionError("无法确定有效状态")

