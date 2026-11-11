# ScrcpyTestPanel v__VERSION__

macOS（Apple Silicon / arm64）桌面控制台，用于调度多版本 scrcpy 投屏与 Android 自动化测试脚本。

## 安装

1. 下载 `ScrcpyTestPanel-v__VERSION__-macos-aarch64.app.zip`
2. 解压得到 `ScrcpyTestPanel.app`，拖入「应用程序」
3. 因仅为 ad-hoc 签名、未做 Apple 公证，首次打开请：
   - Finder 中右键 → 打开 → 再次确认；或
   - 终端执行 `xattr -cr /Applications/ScrcpyTestPanel.app`

## 使用前置

- `adb` 需在 PATH 中，或在「设置」中指定 `platform-tools/adb` 路径
- `scrcpy` 二进制在「版本管理」中登记，可登记多个版本，按设备绑定或全局默认
- 自动化脚本需运行在自己的 Python 环境中（任务模板里指定解释器路径）

## 校验

```bash
shasum -a 256 -c SHA256SUMS.txt
```

## 自检

```bash
/Applications/ScrcpyTestPanel.app/Contents/MacOS/ScrcpyTestPanel --selftest
```

输出 12 项结果，末行应为 `SELFTEST OK`。

## 本版范围

- 设备列表与状态细分、备注、重复 SN 告警、重启 adb
- 多版本 scrcpy 登记与投屏启停、全部断开、孤儿进程清理
- 测试任务模板、独立解释器、`--probe` 连通性自检、流式日志、6 种终态
- 报告按「项目 / App 版本 / 时间戳」归档，历史记录一键打开
- 托盘常驻、单实例锁、配置原子写与损坏自动重建
- `--selftest` 无头自检（CI 门禁与排障用）
- GitHub Actions 自动构建与正式 Release

## 已知限制

- 仅支持 macOS aarch64
- 同一序列号不支持并发多实例
- 未签名公证，首次打开需手动放行
- 不含批量下发、结果看板、截图预览（后续版本）

完整说明见仓库内 `README.md` 与 `docs/`。
