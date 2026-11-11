"""配置读写、迁移、校验、原子落盘。

本模块不依赖 Qt，可被外部脚本单独导入。
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import constants as C

# ---------------------------------------------------------------- 工具函数

_ILLEGAL_SEGMENT_CHARS = set('/\\:*?"<>|')


def safe_path_segment(value: Any, fallback: str = "unnamed", max_len: int = 64) -> str:
    """把任意用户输入变成可安全用作目录名的片段。

    规则：去除控制字符与路径分隔符、合并空白、削掉 ``..``、截断长度。
    """
    if value is None:
        return fallback
    text = unicodedata.normalize("NFKC", str(value))
    chars: List[str] = []
    for ch in text:
        if ch < " " or ch == "\x7f":
            continue
        if ch in _ILLEGAL_SEGMENT_CHARS:
            continue
        chars.append(ch)
    cleaned = "".join(chars)
    cleaned = cleaned.replace("..", "_")
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    cleaned = cleaned.strip("._-")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("._-")
    return cleaned or fallback


def _as_int(value: Any, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        result = default
    else:
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            result = default
    if lo is not None and result < lo:
        result = lo
    if hi is not None and result > hi:
        result = hi
    return result


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def default_config() -> Dict[str, Any]:
    return {
        "config_version": C.CONFIG_VERSION,
        "adb_bin_path": "adb",
        "poll_interval": C.POLL_INTERVAL_DEFAULT,
        "scrcpy_default_id": "",
        "scrcpy_version_lib": {},
        "device_note": {},
        "device_bind_version": {},
        "test_task_list": [],
        "recent_report": {},
        "max_recent_report_count": C.MAX_RECENT_REPORT_DEFAULT,
        "log_max_lines": C.LOG_MAX_LINES_DEFAULT,
        "auto_scroll_log": True,
    }


# ---------------------------------------------------------------- 任务模板

TASK_REQUIRED_FIELDS = ("task_id", "task_name", "script_path", "python_interpreter", "report_root")


def normalize_task_template(raw: Dict[str, Any]) -> Dict[str, Any]:
    """补齐默认值并修正非法字段，返回规范化后的模板。"""
    task = dict(raw) if isinstance(raw, dict) else {}
    task["task_id"] = _as_str(task.get("task_id", "")).strip()
    task["task_name"] = _as_str(task.get("task_name", "")).strip()
    task["script_path"] = _as_str(task.get("script_path", "")).strip()
    task["python_interpreter"] = _as_str(task.get("python_interpreter", "")).strip()
    task["report_root"] = _as_str(task.get("report_root", "")).strip()
    # timeout：非法或 <=0 一律回退默认值（不做下限 clamp），上限截断
    raw_timeout = task.get("timeout")
    timeout = C.TASK_TIMEOUT_DEFAULT
    if not isinstance(raw_timeout, bool) and isinstance(raw_timeout, (int, float, str)):
        try:
            timeout = int(float(raw_timeout))
        except (TypeError, ValueError):
            timeout = C.TASK_TIMEOUT_DEFAULT
        if timeout <= 0:
            timeout = C.TASK_TIMEOUT_DEFAULT
    if timeout > C.TASK_TIMEOUT_MAX:
        timeout = C.TASK_TIMEOUT_MAX
    task["timeout"] = timeout
    task["enabled"] = _as_bool(task.get("enabled"), True)

    extra_args = task.get("extra_args")
    if isinstance(extra_args, list):
        task["extra_args"] = [_as_str(x) for x in extra_args if _as_str(x)]
    else:
        task["extra_args"] = []

    extra_env = task.get("extra_env")
    if isinstance(extra_env, dict):
        task["extra_env"] = {str(k): _as_str(v) for k, v in extra_env.items()}
    else:
        task["extra_env"] = {}
    return task


def validate_task_template(task: Dict[str, Any], check_fs: bool = False) -> Tuple[bool, str]:
    """校验已规范化的模板。check_fs=True 时同时做文件系统校验。"""
    for field in TASK_REQUIRED_FIELDS:
        if not task.get(field):
            return False, f"缺少必填项：{field}"

    timeout = task.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        return False, "timeout 必须为正整数（秒）"

    if check_fs:
        script = task["script_path"]
        if not os.path.isfile(script):
            return False, f"脚本不存在：{script}"
        interpreter = task["python_interpreter"]
        if not (os.path.isfile(interpreter) and os.access(interpreter, os.X_OK)):
            return False, f"python 解释器不可执行：{interpreter}"
        root = task["report_root"]
        if os.path.isdir(root):
            if not os.access(root, os.W_OK):
                return False, f"报告目录不可写：{root}"
        else:
            parent = os.path.dirname(os.path.abspath(root)) or "/"
            if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
                return False, f"报告目录无法创建：{root}"
    return True, ""


def validate_task_templates(tasks: Iterable[Dict[str, Any]]) -> Tuple[bool, str]:
    seen: set[str] = set()
    for task in tasks:
        tid = task.get("task_id", "")
        if not tid:
            return False, "存在缺少 task_id 的任务模板"
        if tid in seen:
            return False, f"task_id 重复：{tid}"
        seen.add(tid)
    return True, ""


# ---------------------------------------------------------------- 配置对象


class ConfigHelper:
    """配置的唯一入口。所有修改即时落盘（原子写）。"""

    def __init__(self, path: Optional[str] = None):
        self.path = os.path.abspath(path) if path else C.config_path()
        self.data: Dict[str, Any] = default_config()
        self.corrupt_backup: Optional[str] = None
        self.load_error: Optional[str] = None

    # ------------------------------------------------------------ 读写
    def load(self) -> Dict[str, Any]:
        self.corrupt_backup = None
        self.load_error = None

        if not os.path.exists(self.path):
            self.data = default_config()
            self.save()
            return self.data

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("配置根节点不是对象")
        except Exception as exc:  # noqa: BLE001
            self.load_error = str(exc)
            self.corrupt_backup = self._backup_broken()
            self.data = default_config()
            self.save()
            return self.data

        self.data = self._normalize(self._migrate(raw))
        self.save()
        return self.data

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=False)
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _backup_broken(self) -> Optional[str]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = os.path.join(os.path.dirname(self.path), f"config.broken.{stamp}.json")
        try:
            shutil.copy2(self.path, target)
            return target
        except Exception:
            return None

    # ------------------------------------------------------------ 迁移与规范化
    def _migrate(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        version = raw.get("config_version")
        if isinstance(version, bool) or not isinstance(version, int):
            version = 1
        guard = 0
        while version < C.CONFIG_VERSION and guard < 10:
            migration = MIGRATIONS.get(version)
            if migration is None:
                break
            raw = migration(dict(raw))
            version += 1
            guard += 1
        raw["config_version"] = C.CONFIG_VERSION
        return raw

    def _normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        cfg = default_config()

        cfg["adb_bin_path"] = _as_str(raw.get("adb_bin_path", cfg["adb_bin_path"])).strip() or "adb"
        cfg["poll_interval"] = _as_int(
            raw.get("poll_interval"),
            C.POLL_INTERVAL_DEFAULT,
            lo=C.POLL_INTERVAL_MIN,
            hi=C.POLL_INTERVAL_MAX,
        )
        cfg["log_max_lines"] = _as_int(
            raw.get("log_max_lines"),
            C.LOG_MAX_LINES_DEFAULT,
            lo=C.LOG_MAX_LINES_MIN,
            hi=C.LOG_MAX_LINES_MAX,
        )
        cfg["max_recent_report_count"] = _as_int(
            raw.get("max_recent_report_count"),
            C.MAX_RECENT_REPORT_DEFAULT,
            lo=1,
            hi=500,
        )
        cfg["auto_scroll_log"] = _as_bool(raw.get("auto_scroll_log"), True)

        # 版本库
        lib_raw = raw.get("scrcpy_version_lib")
        lib: Dict[str, Dict[str, str]] = {}
        if isinstance(lib_raw, dict):
            for key, entry in lib_raw.items():
                vid = _as_str(key).strip()
                if not vid or not isinstance(entry, dict):
                    continue
                bin_path = _as_str(entry.get("bin_path", "")).strip()
                if not bin_path:
                    continue
                lib[vid] = {
                    "bin_path": bin_path,
                    "real_version": _as_str(entry.get("real_version", "")).strip(),
                    "alias": _as_str(entry.get("alias", "")).strip(),
                    "remark": _as_str(entry.get("remark", "")).strip(),
                }
        cfg["scrcpy_version_lib"] = lib

        default_id = _as_str(raw.get("scrcpy_default_id", "")).strip()
        if default_id not in lib:
            default_id = next(iter(lib), "")
        cfg["scrcpy_default_id"] = default_id

        # 设备备注
        notes_raw = raw.get("device_note")
        notes: Dict[str, str] = {}
        if isinstance(notes_raw, dict):
            for key, val in notes_raw.items():
                serial = _as_str(key).strip()
                if serial:
                    notes[serial] = _as_str(val)
        cfg["device_note"] = notes

        # 设备绑定版本：失效绑定直接丢弃
        bind_raw = raw.get("device_bind_version")
        binds: Dict[str, str] = {}
        if isinstance(bind_raw, dict):
            for key, val in bind_raw.items():
                serial = _as_str(key).strip()
                vid = _as_str(val).strip()
                if serial and vid in lib:
                    binds[serial] = vid
        cfg["device_bind_version"] = binds

        # 任务模板
        tasks_raw = raw.get("test_task_list")
        tasks: List[Dict[str, Any]] = []
        used: set[str] = set()
        if isinstance(tasks_raw, list):
            for item in tasks_raw:
                if not isinstance(item, dict):
                    continue
                task = normalize_task_template(item)
                if not task["task_id"]:
                    continue
                if task["task_id"] in used:
                    continue
                used.add(task["task_id"])
                tasks.append(task)
        cfg["test_task_list"] = tasks

        # 历史报告
        max_recent = cfg["max_recent_report_count"]
        recent_raw = raw.get("recent_report")
        recent: Dict[str, List[Dict[str, Any]]] = {}
        if isinstance(recent_raw, dict):
            for key, items in recent_raw.items():
                serial = _as_str(key).strip()
                if not serial or not isinstance(items, list):
                    continue
                records: List[Dict[str, Any]] = []
                for rec in items:
                    if not isinstance(rec, dict):
                        continue
                    records.append(
                        {
                            "task_id": _as_str(rec.get("task_id", "")),
                            "task_name": _as_str(rec.get("task_name", "")),
                            "app_version": _as_str(rec.get("app_version", "")),
                            "report_path": _as_str(rec.get("report_path", "")),
                            "start_time": _as_str(rec.get("start_time", "")),
                            "end_time": _as_str(rec.get("end_time", "")),
                            "status": _as_str(rec.get("status", "")) or C.ST_FAILED,
                            "exit_code": _as_int(rec.get("exit_code"), -1),
                        }
                    )
                recent[serial] = records[-max_recent:]
        cfg["recent_report"] = recent
        return cfg

    def write(self) -> None:
        self.save()

    # ------------------------------------------------------------ 基础项
    def adb_path(self) -> str:
        return self.data.get("adb_bin_path", "adb")

    def set_adb_path(self, value: str) -> None:
        self.data["adb_bin_path"] = value.strip() or "adb"
        self.save()

    def poll_interval(self) -> int:
        return _as_int(
            self.data.get("poll_interval"),
            C.POLL_INTERVAL_DEFAULT,
            lo=C.POLL_INTERVAL_MIN,
            hi=C.POLL_INTERVAL_MAX,
        )

    def set_poll_interval(self, value: Any) -> int:
        interval = _as_int(value, C.POLL_INTERVAL_DEFAULT, lo=C.POLL_INTERVAL_MIN, hi=C.POLL_INTERVAL_MAX)
        self.data["poll_interval"] = interval
        self.save()
        return interval

    def log_max_lines(self) -> int:
        return _as_int(
            self.data.get("log_max_lines"),
            C.LOG_MAX_LINES_DEFAULT,
            lo=C.LOG_MAX_LINES_MIN,
            hi=C.LOG_MAX_LINES_MAX,
        )

    def set_log_max_lines(self, value: Any) -> int:
        lines = _as_int(value, C.LOG_MAX_LINES_DEFAULT, lo=C.LOG_MAX_LINES_MIN, hi=C.LOG_MAX_LINES_MAX)
        self.data["log_max_lines"] = lines
        self.save()
        return lines

    def auto_scroll(self) -> bool:
        return _as_bool(self.data.get("auto_scroll_log"), True)

    def set_auto_scroll(self, value: bool) -> None:
        self.data["auto_scroll_log"] = bool(value)
        self.save()

    def max_recent_report(self) -> int:
        return _as_int(self.data.get("max_recent_report_count"), C.MAX_RECENT_REPORT_DEFAULT, lo=1, hi=500)

    # ------------------------------------------------------------ scrcpy 版本库
    def versions(self) -> Dict[str, Dict[str, str]]:
        lib = self.data.get("scrcpy_version_lib")
        return lib if isinstance(lib, dict) else {}

    def default_version_id(self) -> str:
        return _as_str(self.data.get("scrcpy_default_id", ""))

    def set_default_version(self, version_id: str) -> Tuple[bool, str]:
        if version_id not in self.versions():
            return False, "版本不存在"
        self.data["scrcpy_default_id"] = version_id
        self.save()
        return True, ""

    def add_version(
        self,
        bin_path: str,
        real_version: str = "",
        alias: str = "",
        remark: str = "",
    ) -> str:
        bin_path = bin_path.strip()
        base = safe_path_segment(real_version or os.path.basename(bin_path), fallback="scrcpy", max_len=32)
        vid = base
        index = 2
        while vid in self.versions():
            vid = f"{base}-{index}"
            index += 1
        self.data.setdefault("scrcpy_version_lib", {})[vid] = {
            "bin_path": bin_path,
            "real_version": real_version.strip(),
            "alias": alias.strip() or real_version.strip() or base,
            "remark": remark.strip(),
        }
        if not self.data.get("scrcpy_default_id"):
            self.data["scrcpy_default_id"] = vid
        self.save()
        return vid

    def update_version(self, version_id: str, **fields: Any) -> Tuple[bool, str]:
        entry = self.versions().get(version_id)
        if not entry:
            return False, "版本不存在"
        for key in ("bin_path", "real_version", "alias", "remark"):
            if key in fields:
                entry[key] = _as_str(fields[key]).strip()
        if not entry.get("bin_path"):
            return False, "二进制路径不能为空"
        self.save()
        return True, ""

    def remove_version(self, version_id: str) -> Tuple[bool, str]:
        lib = self.versions()
        if version_id not in lib:
            return False, "版本不存在"
        del lib[version_id]
        binds = self.data.get("device_bind_version", {})
        for serial, vid in list(binds.items()):
            if vid == version_id:
                del binds[serial]
        if self.data.get("scrcpy_default_id") == version_id:
            self.data["scrcpy_default_id"] = next(iter(lib), "")
        self.save()
        return True, ""

    def bind_version(self, serial: str, version_id: Optional[str]) -> None:
        binds = self.data.setdefault("device_bind_version", {})
        if not version_id or version_id not in self.versions():
            binds.pop(serial, None)
        else:
            binds[serial] = version_id
        self.save()

    def bound_version_id(self, serial: str) -> str:
        binds = self.data.get("device_bind_version", {})
        vid = _as_str(binds.get(serial, "")) if isinstance(binds, dict) else ""
        return vid if vid in self.versions() else ""

    def effective_version(self, serial: str) -> Tuple[str, str]:
        """返回该设备实际应使用的 (版本ID, 二进制路径)；不可用时返回 ("", "")。"""
        vid = self.bound_version_id(serial) or self.default_version_id()
        entry = self.versions().get(vid)
        if not entry:
            return "", ""
        return vid, _as_str(entry.get("bin_path", ""))

    # ------------------------------------------------------------ 设备备注
    def note(self, serial: str) -> str:
        notes = self.data.get("device_note", {})
        return _as_str(notes.get(serial, "")) if isinstance(notes, dict) else ""

    def set_note(self, serial: str, text: str) -> None:
        notes = self.data.setdefault("device_note", {})
        text = text.strip()
        if text:
            notes[serial] = text
        else:
            notes.pop(serial, None)
        self.save()

    # ------------------------------------------------------------ 任务模板
    def tasks(self) -> List[Dict[str, Any]]:
        items = self.data.get("test_task_list")
        return items if isinstance(items, list) else []

    def task(self, task_id: str) -> Optional[Dict[str, Any]]:
        for item in self.tasks():
            if item.get("task_id") == task_id:
                return item
        return None

    def enabled_tasks(self) -> List[Dict[str, Any]]:
        return [t for t in self.tasks() if t.get("enabled", True)]

    def _next_task_id(self, name: str) -> str:
        base = safe_path_segment(name, fallback="task", max_len=24).lower() or "task"
        candidate = base
        index = 2
        existing = {t.get("task_id") for t in self.tasks()}
        while candidate in existing:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def add_task(self, task: Dict[str, Any]) -> Tuple[bool, str]:
        normalized = normalize_task_template(task)
        if not normalized["task_id"]:
            normalized["task_id"] = self._next_task_id(normalized["task_name"])
        ok, msg = validate_task_template(normalized, check_fs=False)
        if not ok:
            return False, msg
        if self.task(normalized["task_id"]):
            return False, f"task_id 已存在：{normalized['task_id']}"
        self.data.setdefault("test_task_list", []).append(normalized)
        self.save()
        return True, normalized["task_id"]

    def update_task(self, task_id: str, task: Dict[str, Any]) -> Tuple[bool, str]:
        normalized = normalize_task_template(task)
        normalized["task_id"] = task_id
        ok, msg = validate_task_template(normalized, check_fs=False)
        if not ok:
            return False, msg
        for index, item in enumerate(self.tasks()):
            if item.get("task_id") == task_id:
                self.data["test_task_list"][index] = normalized
                self.save()
                return True, ""
        return False, "任务不存在"

    def remove_task(self, task_id: str) -> Tuple[bool, str]:
        tasks = self.tasks()
        for index, item in enumerate(tasks):
            if item.get("task_id") == task_id:
                del self.data["test_task_list"][index]
                self.save()
                return True, ""
        return False, "任务不存在"

    def duplicate_task(self, task_id: str) -> Tuple[bool, str]:
        task = self.task(task_id)
        if not task:
            return False, "任务不存在"
        clone = dict(task)
        clone["task_name"] = f"{task.get('task_name', '')} 副本"
        clone["task_id"] = self._next_task_id(clone["task_name"])
        ok, msg = self.add_task(clone)
        return (True, clone["task_id"]) if ok else (False, msg)

    def toggle_task(self, task_id: str, enabled: bool) -> None:
        for item in self.tasks():
            if item.get("task_id") == task_id:
                item["enabled"] = bool(enabled)
                break
        self.save()

    # ------------------------------------------------------------ 历史报告
    def recent(self, serial: str) -> List[Dict[str, Any]]:
        data = self.data.get("recent_report", {})
        items = data.get(serial, []) if isinstance(data, dict) else []
        return items if isinstance(items, list) else []

    def add_recent(self, serial: str, record: Dict[str, Any]) -> None:
        data = self.data.setdefault("recent_report", {})
        records = data.setdefault(serial, [])
        records.append(record)
        limit = self.max_recent_report()
        if len(records) > limit:
            del records[: len(records) - limit]
        self.save()

    # ------------------------------------------------------------ 兼容旧接口
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    # 1: 从 config_version 1 迁移到 2（当前暂无）
}
