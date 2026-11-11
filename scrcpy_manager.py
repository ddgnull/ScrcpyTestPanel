"""adb 设备查询与 scrcpy 进程管理。

本模块不依赖 Qt，可被外部脚本单独导入。
"""
from __future__ import annotations

import glob
import os
import shutil
import signal
import subprocess
import time
from typing import List, Optional, Tuple

import constants as C


class AdbError(Exception):
    """adb 不可用或执行失败。"""


# ---------------------------------------------------------------- 路径探测


def autodetect_adb() -> str:
    found = shutil.which("adb")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
        "/opt/homebrew/bin/adb",
        "/usr/local/bin/adb",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


def autodetect_scrcpy() -> str:
    candidates = [
        "/opt/homebrew/bin/scrcpy",
        os.path.expanduser("~/Downloads/scrcpy-macos-aarch64*/scrcpy"),
        os.path.expanduser("~/Downloads/scrcpy*/scrcpy"),
        os.path.expanduser("~/Downloads/scrcpy-macos*/scrcpy"),
    ]
    for pattern in candidates:
        if "*" in pattern:
            matches = sorted(glob.glob(pattern))
            for path in reversed(matches):
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    return path
        elif os.path.isfile(pattern) and os.access(pattern, os.X_OK):
            return pattern
    return ""


# ---------------------------------------------------------------- adb


def adb_probe(adb_path: str) -> Tuple[bool, str]:
    """校验 adb 是否可用，返回 (是否可用, 版本或错误说明)。"""
    path = (adb_path or "").strip() or "adb"
    if os.path.isabs(path) and not os.path.isfile(path):
        return False, f"adb 不存在：{path}"
    if not os.path.isabs(path) and shutil.which(path) is None:
        return False, f"PATH 中找不到 adb：{path}"
    try:
        out = subprocess.run(
            [path, "version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"adb 执行异常：{exc}"
    if out.returncode != 0:
        return False, (out.stderr or out.stdout).strip() or "adb version 执行失败"
    first = (out.stdout or "").strip().splitlines()
    return True, first[0] if first else "adb 已就绪"


def list_devices(adb_path: str) -> Tuple[List[dict], Optional[str]]:
    """返回 (设备列表, 错误信息)。设备项包含 serial/state/duplicate。"""
    path = (adb_path or "").strip() or "adb"
    try:
        out = subprocess.run(
            [path, "devices"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return [], f"adb 不存在，请在设置中配置 adb 路径（当前：{path}）"
    except subprocess.TimeoutExpired:
        return [], "adb devices 执行超时，adb server 可能异常"
    except Exception as exc:  # noqa: BLE001
        return [], f"adb 执行失败：{exc}"

    if out.returncode != 0:
        err = (out.stderr or "").strip()
        return [], err or f"adb devices 返回码 {out.returncode}"

    devices: List[dict] = []
    counts: dict[str, int] = {}
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        if line.startswith("*"):  # daemon 启动提示
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], " ".join(parts[1:]).strip()
        if state.startswith("no permissions"):
            state = "no permissions"
        elif state not in C.DEVICE_STATE_TEXT:
            state = "unknown"
        devices.append({"serial": serial, "state": state, "duplicate": False})
        counts[serial] = counts.get(serial, 0) + 1

    for device in devices:
        if counts.get(device["serial"], 0) > 1:
            device["duplicate"] = True
    return devices, None


def restart_adb(adb_path: str) -> Tuple[bool, str]:
    path = (adb_path or "").strip() or "adb"
    try:
        subprocess.run([path, "kill-server"], capture_output=True, text=True, timeout=15)
        out = subprocess.run([path, "start-server"], capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return False, f"重启 adb 失败：{exc}"
    if out.returncode != 0:
        return False, (out.stderr or out.stdout).strip() or "adb start-server 失败"
    return True, "adb 已重启"


def device_is_online(adb_path: str, serial: str) -> bool:
    devices, error = list_devices(adb_path)
    if error:
        return False
    return any(d["serial"] == serial and d["state"] == C.DEVICE_STATE_USABLE for d in devices)


# ---------------------------------------------------------------- scrcpy


def probe_scrcpy(bin_path: str) -> Tuple[bool, str]:
    """校验 scrcpy 二进制并返回真实版本串。"""
    path = (bin_path or "").strip()
    if not path:
        return False, "未填写 scrcpy 路径"
    if not os.path.isfile(path):
        return False, f"文件不存在：{path}"
    if not os.access(path, os.X_OK):
        return False, f"文件不可执行：{path}"
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
    except OSError as exc:
        return False, f"无法执行 scrcpy：{exc}"
    except subprocess.TimeoutExpired:
        return False, "scrcpy --version 超时"
    if out.returncode != 0:
        message = (out.stderr or out.stdout).strip()
        if "cannot be opened" in message or "not opened" in message:
            message += "（可能被 Gatekeeper 拦截，请到「系统设置-隐私与安全性」放行）"
        return False, message or f"scrcpy 返回码 {out.returncode}"
    lines = [x.strip() for x in (out.stdout or "").splitlines() if x.strip()]
    return True, lines[0] if lines else "scrcpy 已就绪"


def build_scrcpy_args(serial: str, bin_path: str, extra_args: Optional[List[str]] = None) -> List[str]:
    args = [
        bin_path,
        "-s",
        serial,
        "--window-title",
        f"{C.SCRCPY_TITLE_PREFIX}{serial}",
    ]
    if extra_args:
        args.extend([str(x) for x in extra_args])
    return args


def start_scrcpy(
    serial: str,
    bin_path: str,
    extra_args: Optional[List[str]] = None,
    cwd: Optional[str] = None,
) -> Tuple[bool, str, Optional[subprocess.Popen]]:
    if not bin_path:
        return False, "未选择 scrcpy 版本", None
    if not os.path.isfile(bin_path):
        return False, f"scrcpy 不存在：{bin_path}", None
    args = build_scrcpy_args(serial, bin_path, extra_args)
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
            bufsize=0,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"启动 scrcpy 失败：{exc}", None
    return True, "", proc


def _kill_group(pid: int, sig: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    try:
        os.killpg(pgid, sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def stop_process_group(proc: subprocess.Popen, grace: float = C.GRACE_KILL_SECONDS) -> str:
    """先 SIGTERM，宽限期后 SIGKILL。返回 graceful / killed / not_running。"""
    if proc is None:
        return "not_running"
    if proc.poll() is not None:
        return "not_running"
    _kill_group(proc.pid, signal.SIGTERM)
    deadline = time.time() + max(1.0, float(grace))
    while time.time() < deadline:
        if proc.poll() is not None:
            return "graceful"
        time.sleep(0.2)
    _kill_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    return "killed"


def stop_scrcpy(proc: subprocess.Popen, grace: float = C.GRACE_KILL_SECONDS) -> str:
    return stop_process_group(proc, grace)


# ---------------------------------------------------------------- 孤儿进程


def list_orphan_scrcpy() -> List[Tuple[int, str]]:
    """扫描仍带 STP- 标题前缀的 scrcpy 进程（面板崩溃/强杀后遗留）。"""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        return []
    if out.returncode != 0:
        return []

    own_pid = os.getpid()
    orphans: List[Tuple[int, str]] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command = parts
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid == own_pid:
            continue
        if C.SCRCPY_TITLE_PREFIX in command and "scrcpy" in command:
            orphans.append((pid, command))
    return orphans


def kill_pid(pid: int, sig: int = signal.SIGTERM) -> bool:
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False
