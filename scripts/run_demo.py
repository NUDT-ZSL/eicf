"""一键启动 3 个确定性仿真站点并回传运行时配置。

用法（Windows）::

    python scripts\\run_demo.py --config-out demo_config.json

端口由操作系统动态分配并写入配置文件；不依赖公网。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eicf.demo import build_demo_sites, write_demo_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-out", default="demo_config.json")
    args = parser.parse_args()

    sites, inflight = build_demo_sites()
    write_demo_config(sites, args.config_out)
    print(f"仿真站点已启动，配置写入 {args.config_out}")
    for site in sites:
        port = site.httpd.server_address[1]
        print(f"  站点 {site.site_id}: http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for site in sites:
            site.stop()
        print("已停止，端口释放。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

