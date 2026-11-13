# 设计说明

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `eicf/models.py` | 记录模型、业务键、规范化 JSON、仲裁排序键、有效状态 |
| `eicf/config.py` | JSON 配置读取与校验（缺基址/站点号报 `E_CONFIG_INVALID`） |
| `eicf/simulator.py` | 标准库 `http.server` 仿真站点、游标注册表、故障注入、服务端在途计数 |
| `eicf/sample_data.py` | 确定性样本（固定时间戳与哈希，无随机数） |
| `eicf/client.py` | HTTP 客户端，429/500/截断 JSON/超时的有界退避重试 |
| `eicf/collector.py` | 多站点并发调度、串行分页、全局信号量、游标卡滞检测、中断钩子 |
| `eicf/storage.py` | SQLite 修订表/水位/链哈希、同事务页提交、仲裁视图、链校验 |
| `eicf/snapshots.py` | 原子化快照、集合哈希、版本化差异、替换失败注入 |
| `eicf/service.py` | 采集编排、不完整站点隔离、规范化报告 |
| `eicf/cli.py` | `collect` / `verify` / `demo-sites` 命令与退出码 |

## 关键决策

### 业务主键

严格由 `(site_id, subject_id, protocol_version, signed_at)` 组成，不含采集批次、
来源 URL、抓取时刻或内容哈希。同一业务键的多次投递以 `(revision, content_hash)`
区分修订，修订表唯一约束为
`UNIQUE(business_key, site_id, revision, content_hash)`，因此仅抓取时刻不同的重复
投递不会产生新行。

### 仲裁

排序键为 `(revision 降序, content_hash 字典序升序, site_id 字典序升序)`，取最小者
为当前有效修订；有效状态由最高修订号上的 signed/withdrawn 决定。整个过程只依赖
记录内容，与采集顺序、线程交错、站点响应顺序无关，因此规范化视图可逐字节复现。

### 游标推进

客户端不解析、不比较游标字符串含义，终止条件只看服务端 `has_more`，推进只取服务端
返回的 `next_cursor`。当 `next_cursor` 与当前游标相同且 `has_more=true` 时允许重取
一次，仍重复则抛 `CursorStuck`，该站点标记为不完整。

### 并发上限

全局 `threading.BoundedSemaphore(global_max_inflight)` 包裹实际 HTTP 调用；同一站点
分页在单线程内串行。峰值以仿真服务端的 `GlobalInflight` 权威计数为准，不使用客户端
自报。

### 恢复

每一页在单个 SQLite 事务（`BEGIN IMMEDIATE` ... `COMMIT`）内完成：插入本页全部新
修订 + 推进该端点 `last_cursor/max_signed_at/pages_fetched`。已存在修订靠唯一约束
去重，因此重启后从最后已提交游标继续，不重复、不丢页。强杀（无 finally）时仅依赖已
落盘的 SQLite 状态恢复。

### 链式哈希

`chain_hash = sha256(prev_chain_hash + "|" + 规范化记录)`，首条前置哈希为 64 个 0。
校验逐业务键、按 `seq` 顺序重算；字段改写触发 `field_tampered`，删除/插入破坏
`prev_chain_hash` 衔接，触发 `revision_inserted_or_deleted`。单键失败不影响其他键，
最终汇总所有失败业务键。

### 快照原子性

先在快照目录同级建 `.tmp-v<n>-*` 临时目录，写满 `manifest.json` 与 `records.json`
后 `os.replace` 为正式版本目录；替换前抛异常会清理临时目录，旧版本保持完整。集合哈希
对按业务键排序后的规范化记录逐条计算，与文件系统遍历顺序无关。

### 不完整站点隔离

重试耗尽的站点进入 `incomplete_sites`，其已落库修订不进入本次快照记录集
（`allowed_endpoints` 过滤），清单 `incomplete=true` 并列出受影响站点；其他成功站点
记录正常入库入快照。

