"""验收 3：确定性冲突仲裁，与采集/响应顺序无关。"""

from __future__ import annotations

from eicf.models import Record, arbitration_sort_key, effective_status
from eicf.sample_data import arbitration_cases
from eicf.simulator import GlobalInflight, SimSite
from eicf.storage import AuditStore

from .conftest import run_cli, write_config


def _status_map(store: AuditStore) -> dict[str, str]:
    result = {}
    for item in store.current_view():
        key = item["business_key"]["subject_id"]
        result[key] = item
    return result


def test_withdrawn_vs_signed_revision_rules():
    cases = arbitration_cases()
    by_subject: dict[str, list[Record]] = {}
    for record in cases:
        by_subject.setdefault(record.subject_id, []).append(record)

    w5s7 = by_subject["W5S7"]
    assert effective_status(w5s7) == "signed"
    assert min(w5s7, key=arbitration_sort_key).revision == 7

    w7s5 = by_subject["W7S5"]
    assert effective_status(w7s5) == "withdrawn"

    tie = by_subject["TIE"]
    winner = min(tie, key=arbitration_sort_key)
    assert winner.content_hash == "hz_aaa"


def test_arbitration_independent_of_response_order(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    cases = arbitration_cases()
    outputs = []

    for reverse in (False, True):
        ordered = list(reversed(cases)) if reverse else list(cases)
        site = SimSite("Z", ordered, page_size=1)
        shared = GlobalInflight()
        site.start(shared)
        try:
            db = f"audit_{'rev' if reverse else 'fwd'}.db"
            snap = f"snap_{'rev' if reverse else 'fwd'}"
            write_config(
                workdir / f"cfg_{'rev' if reverse else 'fwd'}.json",
                [site],
                db_path=db,
                snapshot_dir=snap,
            )
            proc = run_cli(
                workdir,
                "collect",
                "--config",
                f"cfg_{'rev' if reverse else 'fwd'}.json",
            )
            assert proc.returncode == 0, proc.stderr
            outputs.append((workdir / snap / "v1" / "records.json").read_bytes())
        finally:
            site.stop()

    # 两种相反响应顺序的规范化视图逐字节相同
    assert outputs[0] == outputs[1]


def test_current_view_winner_and_status_combined(workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    site = SimSite("Z", arbitration_cases(), page_size=2)
    shared = GlobalInflight()
    site.start(shared)
    try:
        write_config(workdir / "cfg.json", [site])
        proc = run_cli(workdir, "collect", "--config", "cfg.json")
        assert proc.returncode == 0, proc.stderr
        store = AuditStore("audit.db")
        view = _status_map(store)
        assert view["W5S7"]["effective_status"] == "signed"
        assert view["W7S5"]["effective_status"] == "withdrawn"
        assert view["TIE"]["winner"]["content_hash"] == "hz_aaa"
    finally:
        site.stop()

