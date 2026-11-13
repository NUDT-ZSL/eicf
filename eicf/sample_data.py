"""确定性样本数据构造。

所有时间戳与“哈希”均为固定常量，不使用随机数，保证重复执行逐字节一致。
内容哈希使用形如 ``h##`` 的可读确定性字符串，便于断言字典序仲裁。
"""

from __future__ import annotations

from .models import Record

# 固定基准：2023-11-14T22:13:20Z
BASE_TS = 1_700_000_000


def rec(
    site_id: str,
    subject_id: str,
    protocol_version: str,
    signed_at: int,
    status: str,
    revision: int,
    content_hash: str,
) -> Record:
    return Record(
        site_id=site_id,
        subject_id=subject_id,
        protocol_version=protocol_version,
        signed_at=signed_at,
        status=status,
        revision=revision,
        content_hash=content_hash,
    )


def build_baseline_dataset() -> dict[str, list[Record]]:
    """构造三站点基线数据集（含跨站同业务键、撤回、更正、重复投递）。"""
    records: dict[str, list[Record]] = {"A": [], "B": [], "C": []}

    # 站点 A：4 个业务键，其中 S1 有两个修订（更正）
    records["A"].append(rec("A", "S1", "v1.0", BASE_TS + 10, "signed", 1, "h10"))
    records["A"].append(rec("A", "S1", "v1.0", BASE_TS + 10, "signed", 2, "h11"))
    records["A"].append(rec("A", "S2", "v1.0", BASE_TS + 20, "signed", 1, "h20"))
    records["A"].append(rec("A", "S3", "v1.1", BASE_TS + 30, "signed", 1, "h30"))
    # S4 后续会被更高修订号撤回（基线先放 signed 修订 1）
    records["A"].append(rec("A", "S4", "v1.0", BASE_TS + 40, "signed", 1, "h40"))

    # 站点 B：3 个业务键；S5 有撤回
    records["B"].append(rec("B", "S5", "v1.0", BASE_TS + 50, "signed", 1, "h50"))
    records["B"].append(rec("B", "S5", "v1.0", BASE_TS + 50, "withdrawn", 2, "h51"))
    records["B"].append(rec("B", "S6", "v1.0", BASE_TS + 60, "signed", 1, "h60"))
    records["B"].append(rec("B", "S7", "v1.1", BASE_TS + 70, "signed", 1, "h70"))

    # 站点 C：与 A 的 S1 同业务键（site_id 不同则业务键不同，因此这里构造
    # 真正跨站同业务键需要 site_id 相同——业务键含 site_id，故跨站同业务键
    # 通过让 C 投递 site_id=A 的记录来模拟“同一逻辑站点数据被两个端点重复
    # 投递”的跨源去重场景，见 SHARED 记录）。
    records["C"].append(rec("C", "S8", "v1.0", BASE_TS + 80, "signed", 1, "h80"))
    records["C"].append(rec("C", "S9", "v1.0", BASE_TS + 90, "signed", 1, "h90"))

    # 跨源去重：A 与 C 两个端点投递完全相同的业务键（含相同 site_id=A），
    # 仅内容哈希不同 -> 同一业务键的两个修订来源。
    shared_key = ("A", "SHARED", "v1.0", BASE_TS + 100)
    records["A"].append(rec(*shared_key, "signed", 3, "hA0"))
    records["C"].append(rec(*shared_key, "signed", 3, "hC0"))

    # 仅抓取时刻不同、业务键与内容完全相同的重复投递（幂等去重测试）：
    # 在 C 端点重复放一条与 A 完全一致的记录。
    records["C"].append(rec("A", "S2", "v1.0", BASE_TS + 20, "signed", 1, "h20"))

    return records


def baseline_unique_business_keys(dataset: dict[str, list[Record]]) -> int:
    keys = set()
    for endpoint_records in dataset.values():
        for record in endpoint_records:
            keys.add(record.business_key())
    return len(keys)


def build_increment_for_site_a() -> dict[str, list[Record]]:
    """站点 A 的增量：新增 2 条、更正 1 条（更高修订号）、撤回 1 条。"""
    return {
        "A": [
            # 新增 2 条
            rec("A", "NEW1", "v1.1", BASE_TS + 110, "signed", 1, "hn1"),
            rec("A", "NEW2", "v1.1", BASE_TS + 120, "signed", 1, "hn2"),
            # 对 S3 更高修订号更正
            rec("A", "S3", "v1.1", BASE_TS + 30, "signed", 2, "h31"),
            # 对 S4 更高修订号撤回
            rec("A", "S4", "v1.0", BASE_TS + 40, "withdrawn", 3, "h41"),
        ]
    }


def arbitration_cases() -> list[Record]:
    """确定性仲裁专项用例记录（独立业务键，避免污染主数据集）。"""
    ts = BASE_TS + 900
    cases: list[Record] = []
    # withdrawn 5 vs signed 7 -> signed
    cases.append(rec("Z", "W5S7", "vX", ts, "withdrawn", 5, "hz1"))
    cases.append(rec("Z", "W5S7", "vX", ts, "signed", 7, "hz2"))
    # withdrawn 7 vs signed 5 -> withdrawn
    cases.append(rec("Z", "W7S5", "vX", ts + 1, "withdrawn", 7, "hz3"))
    cases.append(rec("Z", "W7S5", "vX", ts + 1, "signed", 5, "hz4"))
    # 同修订号不同哈希 -> 选字典序较小者 hz_small
    cases.append(rec("Z", "TIE", "vX", ts + 2, "signed", 4, "hz_zzz"))
    cases.append(rec("Z", "TIE", "vX", ts + 2, "signed", 4, "hz_aaa"))
    return cases

