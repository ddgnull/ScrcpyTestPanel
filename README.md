# ScrcpyTestPanel

macOS 上的 Android 自动化测试调度控制台（PySide6）：多版本 scrcpy 投屏调度 + 独立 Python 环境跑自动化脚本 + 报告归档与一键打开。

- 工具版本独立演进，与 scrcpy 版本**无任何绑定关系**（scrcpy 只是被调度的外部二进制）
- 当前版本：`0.0.2`（0.x 亦为正式发布，非预发布）
- 仓库：https://github.com/ddgnull/ScrcpyTestPanel

---

## 功能

| 模块 | 能力 |
|---|---|
| 设备 | 轮询 `adb devices`、状态细分（device / unauthorized / offline / no permissions）、备注、重复 SN 告警、重启 adb |
| 投屏 | 多版本 scrcpy 登记（别名/备注/真实版本），设备绑定或全局默认，单设备启停，全部断开，孤儿进程清理 |
| 测试 | 任务模板增删改复制，独立解释器，`--probe` 连通性自检，流式日志，6 种终态，历史报告与一键打开 |
| 日志 | 分级着色、行数与单行上限、节流刷新、导出、崩溃保留尾部 |
| 系统 | 托盘常驻、单实例锁、配置原子写与损坏重建 |

---

## 环境准备

```bash
cd /path/to/ScrcpyTestPanel
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> 当前锁定：Python 3.14.7 / PySide6 6.10.3 / PyInstaller 6.22.3。
> PySide6 6.7.x 不支持 Python 3.13+，因此不能用更旧的版本。

前置外部依赖（不参与打包）：

- `adb`：Android platform-tools，需在 PATH 中，或在设置里指到 `/Users/<you>/Library/Android/sdk/platform-tools/adb`
- `scrcpy`：在设置中登记多个版本目录
- 测试脚本自己的 venv（含 Appium 等依赖）

---

## 运行

```bash
source venv/bin/activate
python main_qt.py
```

无界面自检（CI 门禁 / 排障用）：

```bash
python main_qt.py --selftest
```

输出 12 项结果，末行 `SELFTEST OK` 且退出码 0 为通过。

---

## 打包（本地手动，备选通道）

```bash
python scripts/gen_build_info.py --version 0.0.2
pyinstaller build_mac.spec --noconfirm
codesign --force --deep --sign - dist/ScrcpyTestPanel.app
ditto -c -k --keepParent dist/ScrcpyTestPanel.app ScrcpyTestPanel-v0.0.2-macos-aarch64.app.zip
shasum -a 256 ScrcpyTestPanel-v0.0.2-macos-aarch64.app.zip > SHA256SUMS.txt
```

验证：

```bash
codesign --verify --deep --strict --verbose=2 dist/ScrcpyTestPanel.app
dist/ScrcpyTestPanel.app/Contents/MacOS/ScrcpyTestPanel --selftest
```

> 压缩必须用 `ditto`，`zip` 会破坏 `.app` 内的符号链接。

---

## 发布

### 自动（主通道）

推 tag 即触发 `.github/workflows/release-macos.yml`：

```bash
git tag v0.0.2
git push origin v0.0.2
```

流程：源码自检 → 打包 → ad-hoc 签名 → 九项门禁 → zip + SHA256 → 创建**正式 Release**（不进 prerelease）。

要求：`.github/workflows/` 已在默认分支上，否则推 tag 不会触发。

### 手动（备选）

本地按上一节打包，然后在 GitHub 页面 New Release 填写 tag `v0.0.2`，上传 zip 与 `SHA256SUMS.txt`，**直接 Publish，不要勾 "Set as a pre-release"**。

---

## 首次打开提示

应用仅做 ad-hoc 签名，未做 Apple 公证。首次打开被 Gatekeeper 拦截时：

- 方式一：Finder 中右键 → 打开 → 再次确认
- 方式二：`xattr -cr /Applications/ScrcpyTestPanel.app`

---

## 数据与配置

| 内容 | 路径 |
|---|---|
| 配置 | `~/Library/Application Support/ScrcpyTestPanel/config.json` |
| 单实例锁 | 同目录 `panel.lock` |
| 损坏备份 | 同目录 `config.broken.<ts>.json` |
| 崩溃日志 | 同目录 `crash-<ts>.log` |
| 报告 | 由任务模板 `report_root` 指定：`{root}/{项目}/{版本}/{时间戳}/` |

可用环境变量 `SCRCPYTESTPANEL_CONFIG_DIR` 覆盖配置目录（`--selftest` 即依赖它做隔离）。

---

## 目录结构

```
.
├── main_qt.py                 # UI / 托盘 / 单实例 / --selftest
├── config_helper.py           # 配置、迁移、校验、原子写
├── scrcpy_manager.py          # adb 查询、scrcpy 启停、孤儿扫描
├── test_manager.py            # 测试进程、行缓冲解析、终态映射
├── constants.py               # 版本与阈值常量
├── scripts/gen_build_info.py  # 生成 _build_info.py
├── build_mac.spec             # PyInstaller
├── docs/
│   ├── 00-开发计划.md
│   └── 03-自动化脚本对接契约.md
├── script_template/           # 零依赖脚本 demo
└── .github/workflows/         # ci.yml / release-macos.yml
```

---

## 文档

- [开发计划（基线）](docs/00-开发计划.md)
- [自动化脚本对接契约](docs/03-自动化脚本对接契约.md)
- [脚本模板](script_template/README.md)

---

## 已知限制

- 仅支持 macOS aarch64
- 同一序列号不支持多实例并发
- ad-hoc 签名，首次打开需手动放行
- 投屏与测试的进程组在面板异常退出后依赖 `STP-` 标题前缀扫描清理
