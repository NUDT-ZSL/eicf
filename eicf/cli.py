"""命令行入口：collect / verify / demo-sites。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import load_config
from .errors import (
    E_CHAIN_BROKEN,
    E_CONFIG_INVALID,
    EXIT_CONFIG_INVALID,
    ConfigError,
)
from .models import canonical_json
from .service import run_collection, verify_chain_report


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _emit(report: dict, exit_code: int, report_path: str | None) -> None:
    report_text = canonical_json(report)
    if report_path:
        Path(report_path).write_text(report_text + "\n", encoding="utf-8")
    sys.stdout.write(report_text + "\n")

    # 业务错误码同时输出到 stderr，便于进程级断言
    for code in report.get("error_codes", []):
        sys.stderr.write(code + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eicf")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="执行增量采集并生成快照")
    collect.add_argument("--config", required=True)
    collect.add_argument("--report", default=None)
    collect.add_argument(
        "--fail-before-replace", action="store_true", help=argparse.SUPPRESS
    )
    collect.add_argument("--verbose", action="store_true")

    verify = sub.add_parser("verify", help="离线校验修订链")
    verify.add_argument("--config", required=True)
    verify.add_argument("--verbose", action="store_true")

    demo = sub.add_parser("demo-sites", help="启动 3 个确定性仿真站点")
    demo.add_argument("--config-out", required=True)
    demo.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))

    if args.command == "demo-sites":
        return _run_demo(args)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        sys.stderr.write(f"{E_CONFIG_INVALID}: {exc}\n")
        return EXIT_CONFIG_INVALID

    if args.command == "collect":
        try:
            report, exit_code = run_collection(
                config, fail_before_replace=args.fail_before_replace
            )
        except ConfigError as exc:
            sys.stderr.write(f"{E_CONFIG_INVALID}: {exc}\n")
            return EXIT_CONFIG_INVALID
        _emit(report, exit_code, args.report)
        return exit_code

    if args.command == "verify":
        report, exit_code = verify_chain_report(config.db_path)
        for failure in report["failures"]:
            sys.stderr.write(
                f"{E_CHAIN_BROKEN} business_key={failure['business_key']} "
                f"failure_type={failure['failure_type']} "
                f"detail={failure['detail']}\n"
            )
        sys.stdout.write(canonical_json(report) + "\n")
        return exit_code

    return 0


def _run_demo(args: argparse.Namespace) -> int:
    from .demo import build_demo_sites, write_demo_config

    sites, inflight = build_demo_sites()
    write_demo_config(sites, args.config_out)
    sys.stderr.write(
        "仿真站点已启动，配置已写入 "
        f"{args.config_out}，Ctrl+C 退出（仅演示，测试自动管理生命周期）\n"
    )
    try:
        import time

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for site in sites:
            site.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

