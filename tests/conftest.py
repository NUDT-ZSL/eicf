"""端到端测试共享夹具：动态端口启动仿真站点、生成配置、子进程采集。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from eicf.sample_data import build_baseline_dataset
from eicf.simulator import GlobalInflight, SimSite, stop_sites

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_config(path: Path, sites: list[SimSite], **overrides) -> Path:
    config = {
        "global_max_inflight": overrides.pop("global_max_inflight", 3),
        "max_retries": overrides.pop("max_retries", 3),
        "retry_backoff_seconds": overrides.pop("retry_backoff_seconds", 0),
        "request_timeout_seconds": overrides.pop("request_timeout_seconds", 1.0),
        "db_path": overrides.pop("db_path", "audit.db"),
        "snapshot_dir": overrides.pop("snapshot_dir", "snapshots"),
        "sites": [
            {
                "site_id": site.site_id,
                "base_url": f"http://127.0.0.1:{site.httpd.server_address[1]}",
                "page_size_limit": overrides.pop("page_size_limit", site.page_size),
            }
            for site in sites
        ],
    }
    config.update(overrides)
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.fixture
def baseline_sites():
    dataset = build_baseline_dataset()
    sites = [
        SimSite("A", list(dataset["A"]), page_size=2),
        SimSite("B", list(dataset["B"]), page_size=1),
        SimSite("C", list(dataset["C"]), page_size=2),
    ]
    inflight = GlobalInflight()
    urls = {}
    for site in sites:
        urls[site.site_id] = site.start(inflight)
    yield sites, inflight
    stop_sites(sites)


def run_cli(workdir: Path, *args: str, env_updates: dict | None = None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    if env_updates:
        env.update(env_updates)
    proc = subprocess.run(
        [sys.executable, "-m", "eicf.cli", *args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc


def parse_stdout_report(proc) -> dict:
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path

