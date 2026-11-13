"""字符串业务错误码与对应进程退出码。"""

# 业务错误码（字符串常量）
E_SITE_INCOMPLETE = "E_SITE_INCOMPLETE"
E_CHAIN_BROKEN = "E_CHAIN_BROKEN"
E_INTERRUPTED = "E_INTERRUPTED"
E_CONFIG_INVALID = "E_CONFIG_INVALID"

# 进程退出码
EXIT_OK = 0
EXIT_INCOMPLETE = 2
EXIT_CHAIN_BROKEN = 3
EXIT_CONFIG_INVALID = 4


class ConfigError(Exception):
    """配置文件非法。"""


class SiteIncomplete(Exception):
    """某个站点在重试耗尽后仍无法完整抓取。"""


class CollectorInterrupted(Exception):
    """采集过程被可捕获异常或 Ctrl+C 中断。"""


class CursorStuck(Exception):
    """服务端重复返回同一 next_cursor，重取一次后仍重复。"""


class SnapshotReplaceError(Exception):
    """测试注入：快照目录替换阶段失败。"""

