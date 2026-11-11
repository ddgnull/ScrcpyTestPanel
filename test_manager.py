"""自动化测试子进程管理：命令组装、流式输出解析、终止与状态映射。

本模块不依赖 Qt，可被外部脚本单独导入。
"""
from __future__ import annotations

import codecs
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

import constants as C
from scrcpy_manager import stop_process_group

# ANSI 颜色 / OSC 控制序列
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")

REPORT_MARKER = "REPORT_PATH:"
RESULT_MARKER = "RESULT_JSON:"
STATUS_MARKER = "TASK_STATUS:"


# ---------------------------------------------------------------- 输出解析


class LineBuffer:
    """字节流 -> 完整行。正确处理 UTF-8 跨块、ANSI、超长行与 CR 进度条。"""

    def __init__(self, max_len: int = C.MAX_LOG_LINE_LEN):
        self.max_len = max_len
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""

    def feed(self, chunk: bytes) -> List[str]:
        if not chunk:
            return []
        text = self._decoder.decode(chunk)
        if text:
            self._pending += text
        lines: List[str] = []
        while True:
            pos = self._pending.find("\n")
            if pos < 0:
                break
            raw = self._pending[:pos]
            self._pending = self._pending[pos + 1 :]
            cleaned = self._clean(raw)
            if cleaned:
                lines.append(cleaned)
        if len(self._pending) > self.max_len * 4:
            # 极端无换行输出，防止内存膨胀
            self._pending = self._pending[-self.max_len * 2 :]
        return lines

    def flush(self) -> List[str]:
        """进程结束时把残余内容当作最后一行。"""
        remainder, self._pending = self._pending, ""
        cleaned = self._clean(remainder)
        return [cleaned] if cleaned else []

    def _clean(self, raw: str) -> str:
        line = raw.replace("\r", "")
        line = _OSC_RE.sub("", line)
        line = _ANSI_RE.sub("", line)
        line = line.rstrip()
        if len(line) > self.max_len:
            line = line[: self.max_len] + "…(截断)"
        return line


def parse_output_line(line: str) -> Tuple[str, str]:
    """返回 (kind, payload)。kind: report_path / result_json / task_status / log。"""
    stripped = line.strip()
    if stripped.startswith(REPORT_MARKER):
        return "report_path", stripped[len(REPORT_MARKER) :].strip()
    if stripped.startswith(RESULT_MARKER):
        return "result_json", stripped[len(RESULT_MARKER) :].strip()
    if stripped.startswith(STATUS_MARKER):
        return "task_status", stripped[len(STATUS_MARKER) :].strip()
    return "log", line


class OutputAccumulator:
    """累积脚本输出中的关键标记，报告路径取最后一次出现。"""

    def __init__(self, max_len: int = C.MAX_LOG_LINE_LEN):
        self.buffer = LineBuffer(max_len)
        self.report_path = ""
        self.result_json = ""
        self.task_status = ""

    def feed(self, chunk: bytes) -> List[Tuple[str, str]]:
        items: List[Tuple[str, str]] = []
        for line in self.buffer.feed(chunk):
            kind, payload = parse_output_line(line)
            if kind == "report_path" and payload:
                self.report_path = payload
            elif kind == "result_json" and payload:
                self.result_json = payload
            elif kind == "task_status" and payload:
                self.task_status = payload
            items.append((kind, payload))
        return items

    def flush(self) -> List[Tuple[str, str]]:
        items: List[Tuple[str, str]] = []
        for line in self.buffer.flush():
            kind, payload = parse_output_line(line)
            if kind == "report_path" and payload:
                self.report_path = payload
            items.append((kind, payload))
        return items


# ---------------------------------------------------------------- 命令组装


def build_test_command(
    task: Dict[str, Any],
    serial: str,
    app_version: str,
    scrcpy_version_id: str = "",
) -> List[str]:
    args = [
        task["python_interpreter"],
        task["script_path"],
        "--serial",
        serial,
        "--scrcpy-version-id",
        scrcpy_version_id or "",
        "--project-name",
        task.get("task_name", ""),
        "--app-version",
        app_version,
        "--report-root",
        task.get("report_root", ""),
    ]
    extra = task.get("extra_args") or []
    if isinstance(extra, list):
        args.extend([str(x) for x in extra])
    return args


def build_env(extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if isinstance(extra_env, dict):
        for key, value in extra_env.items():
            if key:
                env[str(key)] = str(value)
    return env


def start_test(
    command: List[str],
    cwd: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str, Optional[subprocess.Popen]]:
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=build_env(extra_env),
            start_new_session=True,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        return False, f"无法启动脚本：{exc}", None
    except PermissionError as exc:
        return False, f"无权限执行：{exc}", None
    except Exception as exc:  # noqa: BLE001
        return False, f"启动测试失败：{exc}", None
    return True, "", proc


def terminate_test(proc: subprocess.Popen, grace: float = C.GRACE_KILL_SECONDS) -> str:
    return stop_process_group(proc, grace)


# ---------------------------------------------------------------- 状态映射


def exit_code_to_status(
    exit_code: Optional[int],
    timeout: bool = False,
    stopped: bool = False,
    start_error: bool = False,
) -> str:
    if start_error:
        return C.ST_START_ERROR
    if timeout:
        return C.ST_TIMEOUT
    if stopped:
        return C.ST_STOPPED
    if exit_code == 0:
        return C.ST_SUCCESS
    if exit_code is None:
        return C.ST_FAILED
    return C.ST_FAILED


# ---------------------------------------------------------------- 环境自检


def probe_interpreter(interpreter: str, timeout: float = 20.0) -> Tuple[bool, str]:
    if not interpreter:
        return False, "未填写 python 解释器"
    if not os.path.isfile(interpreter):
        return False, f"解释器不存在：{interpreter}"
    if not os.access(interpreter, os.X_OK):
        return False, f"解释器不可执行：{interpreter}"
    try:
        out = subprocess.run(
            [interpreter, "-c", "import sys;print(sys.version.split()[0])"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"解释器执行失败：{exc}"
    if out.returncode != 0:
        return False, (out.stderr or "").strip() or "解释器返回非 0"
    return True, (out.stdout or "").strip()


def probe_script(task: Dict[str, Any], timeout: float = 30.0) -> Tuple[bool, str]:
    """执行 `{interpreter} {script} --probe`，确认脚本可解析参数。"""
    script = task.get("script_path", "")
    interpreter = task.get("python_interpreter", "")
    if not os.path.isfile(script):
        return False, f"脚本不存在：{script}"
    try:
        out = subprocess.run(
            [interpreter, script, "--probe"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.path.dirname(script) or None,
            env=build_env(task.get("extra_env")),
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"脚本自检失败：{exc}"
    if out.returncode == 0:
        return True, (out.stdout or "").strip() or "脚本可执行"
    message = (out.stderr or out.stdout or "").strip().splitlines()
    return False, message[-1] if message else "脚本自检返回非 0（脚本需支持 --probe）"
