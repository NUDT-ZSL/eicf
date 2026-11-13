# eICF 多站点增量采集与审计快照服务

从多个本地 HTTP 仿真站点采集电子知情同意书（eICF）签署事件，提供跨源去重、
确定性冲突仲裁、可校验整库快照、版本化差异与中断恢复。**仅使用 Python 标准库**
（测试用 pytest），兼容 Python 3.11+，不依赖 Docker 或公网。

## 环境准备

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest --version
```

## 一键体验（自带 3 个仿真站点）

```bash
# 终端 1：启动确定性仿真站点，动态端口写入配置
python scripts/run_demo.py --config-out demo_config.json
# 终端 2：采集并生成快照
python -m eicf.cli collect --config demo_config.json --verbose
# 离线校验修订链
python -m eicf.cli verify --config demo_config.json
```

站点 B 在第 2 页返回与第 1 页相同数据但不同 `next_cursor`，并在第 4 页首次返回与
第 3 页相同的 `next_cursor`（客户端重取一次后恢复）。

## 命令与退出码

| 命令 | 作用 |
| --- | --- |
| `eicf collect --config <json>` | 增量采集 + 快照 + 规范化 JSON 报告 |
| `eicf verify --config <json>` | 离线遍历修订链并汇总篡改 |
| `eicf demo-sites --config-out <json>` | 启动仿真站点（长驻） |

退出码：`0` 全部成功；`2` 数据不完整或中断；`3` 链校验失败；`4` 配置错误。
字符串业务错误码：`E_SITE_INCOMPLETE`、`E_CHAIN_BROKEN`、`E_INTERRUPTED`、
`E_CONFIG_INVALID`，错误码同时写入报告 `error_codes` 与 stderr。

## 配置字段

见 `config/example.json`。站点项含 `site_id`、`base_url`、`page_size_limit`；
全局项含 `global_max_inflight`、`max_retries`、`retry_backoff_seconds`（可为 0）、
`request_timeout_seconds`、`db_path`、`snapshot_dir`。

## 快照与报告

```
snapshots/
  v1/manifest.json   # 版本、记录数、每站点记录数、集合哈希、水位、差异、incomplete
  v1/records.json    # 全部当前有效记录的规范化 JSON
  v2/...
  latest.json        # 原子指针，指向最新版本
```

差异按业务键匹配上一版本，输出新增 `added`、修订 `revised`、撤回 `withdrawn`
三类计数，不依赖上一快照文件的行偏移或行号。

## 测试

```bash
python -m pytest
```

覆盖并发上限（服务端计数）、跨源去重与幂等重跑、确定性仲裁、快照原子性与版本差异、
中断恢复（异常钩子 + `Process.terminate()` 强杀）、故障链路（429/500/截断/超时）、
链校验（改写/删除/单键失败不中断）、配置与退出码约定。仿真站点在 teardown 关闭并
释放端口，全部时间戳固定、无随机种子。

设计细节见 `docs/architecture.md`。

