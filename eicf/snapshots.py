"""整库审计快照、集合哈希与版本化差异。

快照目录布局（每次快照一个版本目录 + 原子替换的 latest 指针）::

    snapshot_dir/
      v1/ manifest.json records.json
      v2/ manifest.json records.json
      latest.json   # 原子写入，指向当前最新版本目录

写入先在同级临时目录 ``.tmp-<version>-...`` 中完成全部文件，再原子
``os.replace`` 为正式版本目录；替换阶段抛异常时旧快照保持完整、无半成品。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SnapshotReplaceError
from .models import canonical_json, sha256_text
from .storage import AuditStore, business_key_tuple_from_dict


def _deterministic_now() -> str:
    """确定性时间戳。

    默认使用环境变量 ``EICF_FIXED_NOW``（测试固定），未设置时回退到 UTC。
    """
    fixed = os.environ.get("EICF_FIXED_NOW")
    if fixed:
        return fixed
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SnapshotResult:
    version: int
    path: Path
    manifest: dict[str, Any]
    collection_hash: str
    diff: dict[str, list[dict[str, Any]]]


def _key_from_view_item(item: dict[str, Any]) -> tuple:
    return business_key_tuple_from_dict(item["business_key"])


def compute_collection_hash(view: list[dict[str, Any]]) -> str:
    """对记录按业务键排序后逐条规范化计算集合哈希（与遍历顺序无关）。"""
    ordered = sorted(view, key=_key_from_view_item)
    digest_input = "\n".join(canonical_json(item) for item in ordered)
    return sha256_text(digest_input)


def _per_endpoint_record_count(
    view: list[dict[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in view:
        site_id = item["winner"]["site_id"]
        counts[site_id] = counts.get(site_id, 0) + 1
    return dict(sorted(counts.items()))


def _watermarks(store: AuditStore) -> dict[str, dict[str, Any]]:
    watermarks: dict[str, dict[str, Any]] = {}
    for row in store.all_endpoint_states():
        watermarks[row["endpoint_id"]] = {
            "last_cursor": row["last_cursor"],
            "max_signed_at": row["max_signed_at"],
            "pages_fetched": row["pages_fetched"],
            "complete": bool(row["complete"]),
        }
    return dict(sorted(watermarks.items()))


def diff_views(
    previous: list[dict[str, Any]] | None,
    current: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """按业务键匹配上一版本与当前版本，得出新增/修订/撤回。

    仅以业务键（而非行号/偏移）定位记录。
    """
    prev_map = {
        canonical_json(item["business_key"]): item for item in (previous or [])
    }
    added: list[dict[str, Any]] = []
    revised: list[dict[str, Any]] = []
    withdrawn: list[dict[str, Any]] = []

    for item in sorted(current, key=_key_from_view_item):
        key = canonical_json(item["business_key"])
        old = prev_map.pop(key, None)
        if old is None:
            added.append(item)
            continue
        if item["effective_status"] == "withdrawn":
            withdrawn.append(item)
        elif canonical_json(old["winner"]) != canonical_json(item["winner"]):
            revised.append(item)

    return {
        "added": added,
        "revised": revised,
        "withdrawn": withdrawn,
    }


def load_view(snapshot_path: str | Path) -> list[dict[str, Any]]:
    records_path = Path(snapshot_path) / "records.json"
    return json.loads(records_path.read_text(encoding="utf-8"))


class SnapshotManager:
    def __init__(
        self,
        store: AuditStore,
        snapshot_dir: str | Path,
        fail_before_replace: bool = False,
    ) -> None:
        self.store = store
        self.snapshot_dir = Path(snapshot_dir)
        self.fail_before_replace = fail_before_replace
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def create_snapshot(
        self,
        incomplete_sites: list[str] | None = None,
        allowed_endpoints: set[str] | None = None,
    ) -> SnapshotResult:
        incomplete_sites = sorted(incomplete_sites or [])
        view = self.store.current_view()
        if allowed_endpoints is not None:
            # 不完整端点的部分数据不得进入被标为完整的记录集
            view = [
                item
                for item in view
                if item["winner"]["site_id"] in allowed_endpoints
            ]
        view = sorted(view, key=_key_from_view_item)

        collection_hash = compute_collection_hash(view)
        version = self.store.latest_snapshot_version() + 1
        previous_view = self._previous_view()
        diff = diff_views(previous_view, view)

        manifest = {
            "version": version,
            "record_count": len(view),
            "per_site_record_count": _per_endpoint_record_count(view),
            "collection_hash": collection_hash,
            "watermarks": _watermarks(self.store),
            "incomplete": bool(incomplete_sites),
            "incomplete_sites": incomplete_sites,
            "diff_summary": {
                "added": len(diff["added"]),
                "revised": len(diff["revised"]),
                "withdrawn": len(diff["withdrawn"]),
            },
        }

        version_dir = self._atomic_write(version, manifest, view)
        self.store.save_snapshot(
            version=version,
            created_at=_deterministic_now(),
            incomplete=bool(incomplete_sites),
            collection_hash=collection_hash,
            manifest=manifest,
        )
        return SnapshotResult(
            version=version,
            path=version_dir,
            manifest=manifest,
            collection_hash=collection_hash,
            diff=diff,
        )

    def _previous_view(self) -> list[dict[str, Any]] | None:
        latest_version = self.store.latest_snapshot_version()
        if latest_version == 0:
            return None
        previous_dir = self.snapshot_dir / f"v{latest_version}"
        if not previous_dir.exists():
            return None
        return load_view(previous_dir)

    def _atomic_write(
        self,
        version: int,
        manifest: dict[str, Any],
        view: list[dict[str, Any]],
    ) -> Path:
        final_dir = self.snapshot_dir / f"v{version}"
        temp_dir = Path(
            tempfile.mkdtemp(prefix=f".tmp-v{version}-", dir=str(self.snapshot_dir))
        )
        try:
            manifest_bytes = (
                canonical_json(manifest) + "\n"
            ).encode("utf-8")
            records_bytes = (
                canonical_json(view) + "\n"
            ).encode("utf-8")
            (temp_dir / "manifest.json").write_bytes(manifest_bytes)
            (temp_dir / "records.json").write_bytes(records_bytes)

            # 测试注入：替换阶段前抛出，临时目录随后清理，旧快照不受影响。
            if self.fail_before_replace:
                raise SnapshotReplaceError(
                    f"注入失败：v{version} 替换前中止"
                )

            os.replace(temp_dir, final_dir)
        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        latest_pointer = self.snapshot_dir / "latest.json"
        pointer_bytes = (
            canonical_json({"latest_version": version}) + "\n"
        ).encode("utf-8")
        self._atomic_write_pointer(latest_pointer, pointer_bytes)
        return final_dir

    @staticmethod
    def _atomic_write_pointer(path: Path, content: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=".latest-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp_name, path)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

