"""全局常量与版本信息收口。

_version 与构建信息：
- 源码态直接运行时，_build_info.py 不存在，使用下方兜底值；
- 打包前由 scripts/gen_build_info.py 生成 _build_info.py 覆盖真实值；
- CI 与本地打包都必须先生成，保证两边口径一致。
"""
from __future__ import annotations

import os

APP_NAME = "ScrcpyTestPanel"
APP_DISPLAY_NAME = "ScrcpyTestPanel"
APP_ID = "com.ddgnull.scrcpytestpanel"
REPO_URL = "https://github.com/ddgnull/ScrcpyTestPanel"

VERSION = "0.0.2"
BUILD_TIME = "unknown"
GIT_COMMIT_SHORT = "unknown"

try:  # 构建脚本注入
    from _build_info import BUILD_TIME as _BT  # type: ignore
    from _build_info import GIT_COMMIT_SHORT as _GS  # type: ignore
    from _build_info import VERSION as _V  # type: ignore

    VERSION = _V
    BUILD_TIME = _BT
    GIT_COMMIT_SHORT = _GS
except Exception:  # 源码态无该文件，保留兜底值
    pass

# 配置
CONFIG_VERSION = 1
CONFIG_DIR_NAME = APP_NAME
CONFIG_FILE_NAME = "config.json"

# 设备轮询
POLL_INTERVAL_DEFAULT = 3000
POLL_INTERVAL_MIN = 1000
POLL_INTERVAL_MAX = 60000
PROCESS_MONITOR_INTERVAL = 1000

# 任务
TASK_TIMEOUT_DEFAULT = 7200
TASK_TIMEOUT_MAX = 24 * 3600
GRACE_KILL_SECONDS = 5.0

# 日志
LOG_MAX_LINES_DEFAULT = 5000
LOG_MAX_LINES_MIN = 200
LOG_MAX_LINES_MAX = 50000
MAX_LOG_LINE_LEN = 2000
LOG_FLUSH_INTERVAL_MS = 150
CRASH_LOG_KEEP_LINES = 200

# 历史报告
MAX_RECENT_REPORT_DEFAULT = 20

# scrcpy 窗口标题前缀：用于孤儿进程识别
SCRCPY_TITLE_PREFIX = "STP-"

# ---------------------------------------------------------------- 状态枚举
# 测试任务终态
ST_SUCCESS = "success"
ST_FAILED = "failed"
ST_TIMEOUT = "timeout"
ST_STOPPED = "stopped"
ST_DEVICE_OFFLINE = "device_offline"
ST_START_ERROR = "start_error"

TEST_STATUS_TEXT = {
    ST_SUCCESS: "成功",
    ST_FAILED: "失败",
    ST_TIMEOUT: "超时终止",
    ST_STOPPED: "手动停止",
    ST_DEVICE_OFFLINE: "设备离线中断",
    ST_START_ERROR: "启动失败",
}

TEST_STATUS_LEVEL = {
    ST_SUCCESS: "info",
    ST_FAILED: "error",
    ST_TIMEOUT: "error",
    ST_STOPPED: "warn",
    ST_DEVICE_OFFLINE: "error",
    ST_START_ERROR: "error",
}

# 投屏状态
SC_STOPPED = "stopped"
SC_RUNNING = "running"
SC_EXITED = "exited"
SC_START_ERROR = "start_error"

SCREEN_STATUS_TEXT = {
    SC_STOPPED: "未启动",
    SC_RUNNING: "投屏中",
    SC_EXITED: "已退出",
    SC_START_ERROR: "启动失败",
}

# 设备 adb 状态
DEVICE_STATE_TEXT = {
    "device": "可用",
    "unauthorized": "未授权",
    "offline": "离线",
    "no permissions": "无权限",
    "unknown": "未知",
}

DEVICE_STATE_USABLE = "device"


def config_dir() -> str:
    """配置目录。环境变量可覆盖，便于自检与多环境隔离。"""
    override = os.environ.get("SCRCPYTESTPANEL_CONFIG_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.expanduser(os.path.join("~", "Library", "Application Support", CONFIG_DIR_NAME))


def config_path() -> str:
    return os.path.join(config_dir(), CONFIG_FILE_NAME)


def version_line() -> str:
    return f"v{VERSION}"


def about_lines() -> list[str]:
    return [
        f"{APP_DISPLAY_NAME} {VERSION}",
        f"构建时间：{BUILD_TIME}",
        f"提交：{GIT_COMMIT_SHORT}",
        f"仓库：{REPO_URL}",
    ]
