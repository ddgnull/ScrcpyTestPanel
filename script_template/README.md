# script_template

面板「测试任务」的脚本模板，零第三方依赖，可直接跑通全链路（参数解析 → 报告生成 → `REPORT_PATH` 输出 → 退出码）。

## 快速验证

```bash
# 参数自检（面板「连通性自检」等价）
python3 script_template/entry.py --probe
# → PROBE OK，退出码 0

# 不连设备跑一遍，验证报告链路
python3 script_template/entry.py \
  --serial FAKE123 \
  --project-name 模板验证 \
  --app-version 0.0.1 \
  --report-root /tmp/stp_reports \
  --dry-run
# → 打印 REPORT_PATH:/tmp/stp_reports/模板验证/0.0.1/<时间戳>/report.html
```

## 接入真实用例

1. 复制 `entry.py` 到你的测试工程（例如 `tests/scrcpy_panel_entry.py`）。
2. 用你的测试 venv（含 Appium 客户端 / pytest）作为面板中的 `python_interpreter`。
3. 保持这三个骨架不动：
   - `build_parser()` 的参数名与语义
   - `_finish()` 里的 `REPORT_PATH:` / `TASK_STATUS:` 输出
   - `main()` 里的 `signal.signal(SIGTERM, _on_sigterm)`
4. 把 `collect_cases()` 换成你的被测流程，每个用例返回 `(是否通过, 详情字符串)`。

## 注意

- 所有 adb 命令必须带 `-s <serial>`，否则多设备时行为不确定。
- 报告目录必须按 `{report_root}/{项目}/{版本}/{时间戳}` 落地，面板侧按此结构做缺失检测。
- 收到 SIGTERM 后 5 秒内必须退出，否则面板强杀进程组（`SIGKILL`）。
- 不要写 `REPORT_PATH` 之外的相对路径，面板会对路径做存在性检查。
