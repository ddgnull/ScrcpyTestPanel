#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ScrcpyTestPanel 自动化脚本模板（零第三方依赖 demo）。

契约见 docs/03-自动化脚本对接契约.md，要点：
1) 接收面板参数：--serial / --scrcpy-version-id / --project-name / --app-version / --report-root
2) 报告目录：{report_root}/{项目}/{App版本}/{时间戳}/report.html
3) 结束时向 stdout 打印 `REPORT_PATH:<绝对路径>`
4) 退出码：0 成功 / 1 用例失败 / 2 环境或参数错误 / 130 被终止
5) 支持 --probe（面板「连通性自检」会调用）

把这个文件替换成真实 Appium 用例时，只需保留参数解析、报告写出、
REPORT_PATH 输出与信号处理这四段骨架。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime

EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_ENV_ERROR = 2
EXIT_INTERRUPTED = 130

_STOP = False


# ---------------------------------------------------------------- 基础工具


def log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] {message}", flush=True)


def _on_sigterm(signum, frame):  # noqa: ARG001
    global _STOP
    _STOP = True
    log("收到终止信号，准备优雅退出", "WARN")


def safe_segment(value: str, fallback: str = "unnamed", max_len: int = 64) -> str:
    """与面板侧保持一致的目录段清洗规则。"""
    if not value:
        return fallback
    text = unicodedata.normalize("NFKC", str(value))
    chars = []
    for ch in text:
        if ch < " " or ch == "\x7f" or ch in '/\\:*?"<>|':
            continue
        chars.append(ch)
    cleaned = "".join(chars).replace("..", "_")
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip("._-")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("._-")
    return cleaned or fallback


def adb(serial: str, *args: str, timeout: float = 8.0) -> subprocess.CompletedProcess:
    adb_bin = os.environ.get("ADB_BIN", "adb")
    return subprocess.run(
        [adb_bin, "-s", serial, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------- 用例


def run_case(name: str, func) -> dict:
    started = time.time()
    try:
        ok, detail = func()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return {
        "name": name,
        "status": "pass" if ok else "fail",
        "detail": detail,
        "ms": int((time.time() - started) * 1000),
    }


def case_device_online(serial: str):
    out = adb(serial, "get-state")
    state = (out.stdout or "").strip()
    if state == "device":
        return True, "adb 状态 device"
    return False, (out.stderr or state or "设备不可用").strip()


def case_device_model(serial: str):
    out = adb(serial, "shell", "getprop", "ro.product.model")
    model = (out.stdout or "").strip()
    return (bool(model), model or "读取机型失败")


def case_android_version(serial: str):
    out = adb(serial, "shell", "getprop", "ro.build.version.release")
    version = (out.stdout or "").strip()
    return (bool(version), f"Android {version}" if version else "读取版本失败")


def collect_cases(serial: str, args) -> list[dict]:
    if args.dry_run:
        log("dry-run：跳过真实设备检查", "WARN")
        return [
            {"name": "设备在线", "status": "pass", "detail": "dry-run", "ms": 0},
            {"name": "读取机型", "status": "pass", "detail": "dry-run", "ms": 0},
            {"name": "Android 版本", "status": "pass", "detail": "dry-run", "ms": 0},
        ]

    cases = []
    for name, func in (
        ("设备在线", lambda: case_device_online(serial)),
        ("读取机型", lambda: case_device_model(serial)),
        ("Android 版本", lambda: case_android_version(serial)),
    ):
        if _STOP:
            break
        cases.append(run_case(name, func))
        log(f"{name}：{cases[-1]['status']} - {cases[-1]['detail']}", "INFO")
    return cases


# ---------------------------------------------------------------- 报告


REPORT_CSS = """
body{font-family:-apple-system,"PingFang SC",sans-serif;margin:0;padding:28px 36px;color:#202124;background:#fafafa}
h1{font-size:22px;margin:0 0 14px}
.meta{color:#5f6368;font-size:13px;line-height:1.9;margin-bottom:18px}
.summary{display:flex;gap:12px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e8eaed;border-radius:8px;padding:10px 16px;min-width:96px}
.card b{display:block;font-size:20px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e8eaed;border-radius:8px;overflow:hidden}
th,td{padding:9px 12px;font-size:13px;border-bottom:1px solid #f1f3f4;text-align:left}
th{background:#f8f9fa;color:#5f6368;font-weight:600}
tr.pass td:first-child{border-left:3px solid #1e8e3e}
tr.fail td:first-child{border-left:3px solid #c5221f}
.status-pass{color:#1e8e3e;font-weight:600}
.status-fail{color:#c5221f;font-weight:600}
"""

REPORT_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="meta">__META__</div>
<div class="summary">__SUMMARY__</div>
<table>
<thead><tr><th>用例</th><th>结果</th><th>详情</th><th>耗时</th></tr></thead>
<tbody>__ROWS__</tbody>
</table>
</body>
</html>
"""


def write_report(report_dir: str, meta: dict, cases: list[dict], status: str) -> str:
    total = len(cases)
    passed = sum(1 for c in cases if c["status"] == "pass")
    failed = total - passed

    title = f"{meta['project']} · {meta['app_version']}"
    meta_rows = "<br>".join(
        [
            f"设备：{html.escape(meta['serial'])}",
            f"任务：{html.escape(meta['project'])}",
            f"App 版本：{html.escape(meta['app_version'])}",
            f"scrcpy 版本 ID：{html.escape(meta['scrcpy_version_id'] or '未绑定')}",
            f"开始时间：{html.escape(meta['start_time'])}",
            f"结束时间：{html.escape(meta['end_time'])}",
            f"最终状态：{html.escape(status)}",
        ]
    )
    summary = "".join(
        [
            f'<div class="card"><b>{total}</b>用例总数</div>',
            f'<div class="card"><b>{passed}</b>通过</div>',
            f'<div class="card"><b>{failed}</b>失败</div>',
        ]
    )
    rows = "".join(
        [
            '<tr class="{cls}"><td>{name}</td><td class="status-{cls}">{text}</td>'
            "<td>{detail}</td><td>{ms} ms</td></tr>".format(
                cls=c["status"],
                name=html.escape(c["name"]),
                text="通过" if c["status"] == "pass" else "失败",
                detail=html.escape(c["detail"]),
                ms=c["ms"],
            )
            for c in cases
        ]
    )
    page = (
        REPORT_TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__CSS__", REPORT_CSS)
        .replace("__META__", meta_rows)
        .replace("__SUMMARY__", summary)
        .replace("__ROWS__", rows)
    )
    report_path = os.path.join(report_dir, "report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(page)
    return report_path


def write_result_json(report_dir: str, meta: dict, cases: list[dict], status: str, exit_code: int) -> str:
    payload = {"meta": meta, "status": status, "exit_code": exit_code, "cases": cases}
    path = os.path.join(report_dir, "result.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ScrcpyTestPanel 脚本模板")
    parser.add_argument("--serial", default="", help="设备序列号")
    parser.add_argument("--scrcpy-version-id", default="", help="面板绑定的 scrcpy 版本 ID")
    parser.add_argument("--project-name", default="", help="任务名称")
    parser.add_argument("--app-version", default="", help="被测 App 版本")
    parser.add_argument("--report-root", default="", help="报告根目录")
    parser.add_argument("--probe", action="store_true", help="仅做参数自检，供面板连通性检查")
    parser.add_argument("--dry-run", action="store_true", help="跳过真实设备检查")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.probe:
        print("PROBE OK")
        return EXIT_SUCCESS

    missing = [
        name
        for name, value in (
            ("--serial", args.serial),
            ("--report-root", args.report_root),
            ("--project-name", args.project_name),
            ("--app-version", args.app_version),
        )
        if not value
    ]
    if missing:
        log(f"缺少必填参数：{', '.join(missing)}", "ERROR")
        return EXIT_ENV_ERROR

    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)

    now = datetime.now()
    started_at = now.strftime("%Y-%m-%d %H:%M:%S")
    stamp = now.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(
        args.report_root,
        safe_segment(args.project_name, "project"),
        safe_segment(args.app_version, "version"),
        stamp,
    )

    meta = {
        "serial": args.serial,
        "project": args.project_name,
        "app_version": args.app_version,
        "scrcpy_version_id": args.scrcpy_version_id,
        "start_time": started_at,
        "end_time": "",
    }

    status = "success"
    exit_code = EXIT_SUCCESS
    cases: list[dict] = []
    report_path = ""

    try:
        os.makedirs(report_dir, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        log(f"无法创建报告目录：{exc}", "ERROR")
        return EXIT_ENV_ERROR

    try:
        if not args.dry_run:
            probe_env = subprocess.run(
                [os.environ.get("ADB_BIN", "adb"), "version"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if probe_env.returncode != 0:
                log("adb 不可用，无法执行用例", "ERROR")
                status = "failed"
                exit_code = EXIT_ENV_ERROR
                cases.append(
                    {"name": "adb 环境", "status": "fail", "detail": "adb 不可用", "ms": 0}
                )
                return _finish(meta, cases, status, exit_code, report_dir)

        cases = collect_cases(args.serial, args)
        if _STOP:
            status = "stopped"
            exit_code = EXIT_INTERRUPTED
        elif any(c["status"] == "fail" for c in cases):
            status = "failed"
            exit_code = EXIT_FAILED
        else:
            status = "success"
            exit_code = EXIT_SUCCESS
        return _finish(meta, cases, status, exit_code, report_dir)
    except Exception as exc:  # noqa: BLE001
        log(f"执行异常：{exc}", "ERROR")
        cases.append({"name": "脚本异常", "status": "fail", "detail": str(exc), "ms": 0})
        return _finish(meta, cases, "failed", EXIT_FAILED, report_dir)


def _finish(meta: dict, cases: list[dict], status: str, exit_code: int, report_dir: str) -> int:
    meta["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        report_path = write_report(report_dir, meta, cases, status)
        write_result_json(report_dir, meta, cases, status, exit_code)
        log(f"报告已生成：{report_path}", "INFO")
    except Exception as exc:  # noqa: BLE001
        log(f"写出报告失败：{exc}", "ERROR")
        return EXIT_ENV_ERROR

    # 面板依赖的两行协议输出
    print(f"REPORT_PATH:{report_path}", flush=True)
    print(f"TASK_STATUS:{status}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
