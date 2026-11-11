#!/usr/bin/env python3
"""生成 _build_info.py（构建信息注入）。

用法：
    python scripts/gen_build_info.py --version 0.0.1
    python scripts/gen_build_info.py            # 版本从 git tag 或 constants.py 推断

生成的 _build_info.py 不入库（见 .gitignore），
由 constants.py 以 try/except 方式读取，缺失时用兜底值。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "_build_info.py")


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def short_commit() -> str:
    sha = _git("rev-parse", "--short=8", "HEAD")
    if not sha:
        return "unknown"
    dirty = "dirty" if _git("status", "--porcelain") else ""
    return f"{sha}{'-' + dirty if dirty else ''}"


def version_from_git() -> str:
    tag = _git("describe", "--tags", "--abbrev=0")
    m = re.match(r"^v?(\d+\.\d+\.\d+)", tag or "")
    return m.group(1) if m else ""


def version_from_constants() -> str:
    path = os.path.join(ROOT, "constants.py")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'^VERSION\s*=\s*["\']([^"\']+)["\']', line.strip())
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "0.0.0"


def resolve_version(cli_version: str) -> str:
    if cli_version:
        return cli_version.strip().lstrip("v")
    return version_from_git() or version_from_constants()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 _build_info.py")
    parser.add_argument("--version", default="", help="版本号，如 0.0.1；缺省时从 git tag 或 constants.py 推断")
    parser.add_argument("--out", default=OUT, help="输出路径")
    args = parser.parse_args(argv)

    version = resolve_version(args.version)
    if not re.match(r"^\d+\.\d+\.\d+", version):
        print(f"[gen_build_info] 版本号不合法：{version!r}", file=sys.stderr)
        return 2

    build_time = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    commit = short_commit()

    content = (
        "# -*- coding: utf-8 -*-\n"
        '"""由 scripts/gen_build_info.py 自动生成，请勿手工编辑，也不要提交到仓库。"""\n'
        f'VERSION = "{version}"\n'
        f'BUILD_TIME = "{build_time}"\n'
        f'GIT_COMMIT_SHORT = "{commit}"\n'
    )

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, out_path)

    print(f"[gen_build_info] {out_path}")
    print(f"  VERSION       = {version}")
    print(f"  BUILD_TIME    = {build_time}")
    print(f"  GIT_COMMIT    = {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
