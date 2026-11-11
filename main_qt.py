"""ScrcpyTestPanel 主程序：PySide6 界面、托盘、定时器与自检入口。

分层约束：本文件只负责 UI 与调度，业务逻辑在 config_helper / scrcpy_manager / test_manager。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import constants as C
import config_helper as CH
import scrcpy_manager as SM
import test_manager as TM

from PySide6.QtCore import QLockFile, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

SERVER_NAME = "com.ddgnull.scrcpytestpanel.instance"

LOG_INFO = "#202124"
LOG_WARN = "#B26B00"
LOG_ERROR = "#C62828"
LOG_TEST = "#1B5E20"

LEVEL_COLORS = {
    "info": LOG_INFO,
    "warn": LOG_WARN,
    "error": LOG_ERROR,
    "test": LOG_TEST,
}


# ---------------------------------------------------------------- 纯逻辑


def compute_device_diff(old: Dict[str, dict], new: Dict[str, dict]) -> Dict[str, list]:
    """对比两批设备状态，返回增量指令（纯函数，便于自检）。"""
    added = [serial for serial in new if serial not in old]
    removed = [serial for serial in old if serial not in new]
    changed: List[Tuple[str, Dict[str, tuple]]] = []
    for serial, row in new.items():
        if serial not in old:
            continue
        prev = old[serial]
        delta = {}
        for key, value in row.items():
            if prev.get(key) != value:
                delta[key] = (prev.get(key), value)
        if delta:
            changed.append((serial, delta))
    return {"added": added, "removed": removed, "changed": changed}


def build_log_line(source: str, text: str, when: Optional[datetime] = None) -> str:
    stamp = (when or datetime.now()).strftime("%H:%M:%S")
    return f"[{stamp}] [{source}] {text}"


class LogBridge(QObject):
    """子线程 -> 主线程的日志与标记通道。"""

    line = Signal(str, str, str)  # serial, source, text
    marker = Signal(str, str, str)  # serial, kind, payload


def _stream_reader(
    bridge: LogBridge,
    serial: str,
    source: str,
    proc,
    accumulator: TM.OutputAccumulator,
) -> None:
    """独立线程读取子进程输出，避免阻塞 UI。"""
    try:
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            for kind, payload in accumulator.feed(raw):
                if kind == "log":
                    bridge.line.emit(serial, source, payload)
                else:
                    bridge.marker.emit(serial, kind, payload)
        for kind, payload in accumulator.flush():
            if kind == "log":
                bridge.line.emit(serial, source, payload)
            else:
                bridge.marker.emit(serial, kind, payload)
    except Exception as exc:  # noqa: BLE001
        bridge.line.emit(serial, source, f"读取输出失败：{exc}")
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 弹窗


class SettingsDialog(QDialog):
    def __init__(self, cfg: CH.ConfigHelper, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)

        form = QFormLayout(self)

        self.adb_edit = QLineEdit(cfg.adb_path())
        adb_row = QHBoxLayout()
        adb_row.addWidget(self.adb_edit)
        browse = QPushButton("选择…")
        browse.clicked.connect(self._pick_adb)
        adb_row.addWidget(browse)
        container = QWidget()
        container.setLayout(adb_row)
        form.addRow("adb 路径", container)

        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(C.POLL_INTERVAL_MIN, C.POLL_INTERVAL_MAX)
        self.poll_spin.setSingleStep(500)
        self.poll_spin.setSuffix(" ms")
        self.poll_spin.setValue(cfg.poll_interval())
        form.addRow("设备轮询间隔", self.poll_spin)

        self.log_spin = QSpinBox()
        self.log_spin.setRange(C.LOG_MAX_LINES_MIN, C.LOG_MAX_LINES_MAX)
        self.log_spin.setSingleStep(500)
        self.log_spin.setValue(cfg.log_max_lines())
        form.addRow("日志最大行数", self.log_spin)

        self.scroll_box = QCheckBox("新日志自动滚动到底部")
        self.scroll_box.setChecked(cfg.auto_scroll())
        form.addRow("", self.scroll_box)

        restart = QPushButton("重启 adb 服务")
        restart.clicked.connect(self._restart_adb)
        form.addRow("", restart)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _pick_adb(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 adb 可执行文件")
        if path:
            self.adb_edit.setText(path)

    def _restart_adb(self):
        ok, msg = SM.restart_adb(self.adb_edit.text().strip() or "adb")
        QMessageBox.information(self, "重启 adb", msg if msg else ("成功" if ok else "失败"))

    def _accept(self):
        adb = self.adb_edit.text().strip() or "adb"
        ok, msg = SM.adb_probe(adb)
        if not ok:
            answer = QMessageBox.question(
                self,
                "adb 校验失败",
                f"{msg}\n\n仍然保存该路径吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.cfg.set_adb_path(adb)
        self.cfg.set_poll_interval(self.poll_spin.value())
        self.cfg.set_log_max_lines(self.log_spin.value())
        self.cfg.set_auto_scroll(self.scroll_box.isChecked())
        self.accept()


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        text = QLabel(
            "\n".join(
                [
                    f"<b>{C.APP_DISPLAY_NAME}</b>",
                    f"版本：{C.VERSION}",
                    f"构建时间：{C.BUILD_TIME}",
                    f"提交：{C.GIT_COMMIT_SHORT}",
                    "",
                    "Android 自动化测试调度控制台：",
                    "· 多版本 scrcpy 投屏调度（工具版本与 scrcpy 版本无关）",
                    "· 独立 Appium 脚本调度，环境隔离",
                    "· 报告按「项目 / 版本 / 日期」归档",
                    "",
                    f"仓库：{C.REPO_URL}",
                ]
            )
        )
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class VersionManagerDialog(QDialog):
    def __init__(self, cfg: CH.ConfigHelper, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Scrcpy 版本管理")
        self.resize(760, 420)

        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["别名", "真实版本", "二进制路径", "默认"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        row = QHBoxLayout()
        add_btn = QPushButton("添加版本")
        add_btn.clicked.connect(self._add)
        del_btn = QPushButton("删除选中")
        del_btn.clicked.connect(self._remove)
        default_btn = QPushButton("设为默认")
        default_btn.clicked.connect(self._set_default)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(add_btn)
        row.addWidget(del_btn)
        row.addWidget(default_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)
        self._reload()

    def _reload(self):
        versions = self.cfg.versions()
        default_id = self.cfg.default_version_id()
        self.table.setRowCount(0)
        for vid, entry in versions.items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            alias = entry.get("alias") or vid
            items = [
                alias,
                entry.get("real_version", ""),
                entry.get("bin_path", ""),
                "默认" if vid == default_id else "",
            ]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, vid)
                if col == 3 and vid == default_id:
                    item.setForeground(QColor(LOG_TEST))
                self.table.setItem(row, col, item)
            if not os.path.isfile(entry.get("bin_path", "")):
                for col in range(4):
                    self.table.item(row, col).setForeground(QColor(LOG_ERROR))
                self.table.item(row, 0).setToolTip("二进制不存在，该版本已失效")

    def _selected_id(self) -> str:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return ""
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _add(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 scrcpy 可执行文件")
        if not path:
            return
        ok, info = SM.probe_scrcpy(path)
        if not ok:
            QMessageBox.warning(self, "校验失败", f"无法使用该文件：\n{info}")
            return
        alias, accepted = QInputDialog.getText(self, "版本别名", "给这个版本起个名字：", text=info)
        if not accepted:
            return
        self.cfg.add_version(path, real_version=info, alias=alias.strip())
        self._reload()

    def _remove(self):
        vid = self._selected_id()
        if not vid:
            QMessageBox.information(self, "删除版本", "请先选中一行")
            return
        answer = QMessageBox.question(self, "删除版本", "删除后，绑定该版本的设备会回退到默认版本。继续？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.cfg.remove_version(vid)
        self._reload()

    def _set_default(self):
        vid = self._selected_id()
        if not vid:
            QMessageBox.information(self, "设为默认", "请先选中一行")
            return
        ok, msg = self.cfg.set_default_version(vid)
        if not ok:
            QMessageBox.warning(self, "设置失败", msg)
        self._reload()


class TaskEditDialog(QDialog):
    def __init__(self, cfg: CH.ConfigHelper, task: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.task = dict(task or {})
        self.setWindowTitle("编辑测试任务" if task else "新增测试任务")
        self.setMinimumWidth(640)

        form = QFormLayout(self)
        self.name_edit = QLineEdit(self.task.get("task_name", ""))
        form.addRow("任务名称", self.name_edit)

        self.script_edit = QLineEdit(self.task.get("script_path", ""))
        form.addRow("脚本路径", self._path_row(self.script_edit, "选择脚本"))

        self.interp_edit = QLineEdit(self.task.get("python_interpreter", sys.executable))
        form.addRow("Python 解释器", self._path_row(self.interp_edit, "选择解释器"))

        self.root_edit = QLineEdit(self.task.get("report_root", os.path.expanduser("~/ScrcpyTestReports")))
        root_row = QHBoxLayout()
        root_row.addWidget(self.root_edit)
        browse = QPushButton("选择…")
        browse.clicked.connect(self._pick_root)
        root_row.addWidget(browse)
        root_widget = QWidget()
        root_widget.setLayout(root_row)
        form.addRow("报告根目录", root_widget)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, C.TASK_TIMEOUT_MAX)
        self.timeout_spin.setSingleStep(600)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setValue(int(self.task.get("timeout") or C.TASK_TIMEOUT_DEFAULT))
        form.addRow("超时时间", self.timeout_spin)

        self.enabled_box = QCheckBox("启用该任务")
        self.enabled_box.setChecked(bool(self.task.get("enabled", True)))
        form.addRow("", self.enabled_box)

        self.args_edit = QLineEdit(" ".join(self.task.get("extra_args") or []))
        self.args_edit.setPlaceholderText("追加命令行参数，空格分隔（可选）")
        form.addRow("额外参数", self.args_edit)

        self.env_edit = QPlainTextEdit(json.dumps(self.task.get("extra_env") or {}, ensure_ascii=False))
        self.env_edit.setFixedHeight(70)
        form.addRow("额外环境变量", self.env_edit)

        self.probe_label = QLabel("")
        self.probe_label.setWordWrap(True)
        form.addRow("", self.probe_label)
        probe_btn = QPushButton("连通性自检")
        probe_btn.clicked.connect(self._probe)
        form.addRow("", probe_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _path_row(self, edit: QLineEdit, title: str) -> QWidget:
        row = QHBoxLayout()
        row.addWidget(edit)
        btn = QPushButton("选择…")
        btn.clicked.connect(lambda: self._pick_file(edit, title))
        row.addWidget(btn)
        widget = QWidget()
        widget.setLayout(row)
        return widget

    def _pick_file(self, edit: QLineEdit, title: str):
        path, _ = QFileDialog.getOpenFileName(self, title)
        if path:
            edit.setText(path)

    def _pick_root(self):
        path = QFileDialog.getExistingDirectory(self, "选择报告根目录")
        if path:
            self.root_edit.setText(path)

    def _collect(self) -> dict:
        try:
            env = json.loads(self.env_edit.toPlainText() or "{}")
            if not isinstance(env, dict):
                env = {}
        except Exception:  # noqa: BLE001
            env = {}
        task = dict(self.task)
        task.update(
            {
                "task_name": self.name_edit.text().strip(),
                "script_path": self.script_edit.text().strip(),
                "python_interpreter": self.interp_edit.text().strip(),
                "report_root": self.root_edit.text().strip(),
                "timeout": self.timeout_spin.value(),
                "enabled": self.enabled_box.isChecked(),
                "extra_args": [x for x in self.args_edit.text().split() if x],
                "extra_env": {str(k): str(v) for k, v in env.items()},
            }
        )
        return task

    def _probe(self):
        task = CH.normalize_task_template(self._collect())
        ok, msg = TM.probe_interpreter(task["python_interpreter"])
        if not ok:
            self.probe_label.setText(f"解释器：失败 - {msg}")
            return
        ok2, msg2 = TM.probe_script(task)
        self.probe_label.setText(
            f"解释器：Python {msg}\n脚本：{'通过 - ' + msg2 if ok2 else '失败 - ' + msg2}"
        )

    def _accept(self):
        task = CH.normalize_task_template(self._collect())
        if not task["task_name"]:
            QMessageBox.warning(self, "校验失败", "任务名称不能为空")
            return
        ok, msg = CH.validate_task_template(task, check_fs=True)
        if not ok:
            QMessageBox.warning(self, "校验失败", msg)
            return
        self.task = task
        self.accept()


class TaskManagerDialog(QDialog):
    def __init__(self, cfg: CH.ConfigHelper, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("测试任务管理")
        self.resize(820, 440)
        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["任务名称", "脚本", "解释器", "超时", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(lambda _: self._edit())
        layout.addWidget(self.table)

        row = QHBoxLayout()
        for text, handler in (
            ("新增", self._add),
            ("编辑", self._edit),
            ("删除", self._remove),
            ("复制", self._duplicate),
            ("连通性自检", self._probe),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(handler)
            row.addWidget(btn)
        row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)
        self._reload()

    def _reload(self):
        self.table.setRowCount(0)
        for task in self.cfg.tasks():
            row = self.table.rowCount()
            self.table.insertRow(row)
            cells = [
                task.get("task_name", ""),
                task.get("script_path", ""),
                task.get("python_interpreter", ""),
                str(task.get("timeout", "")),
                "启用" if task.get("enabled", True) else "停用",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, task.get("task_id"))
                self.table.setItem(row, col, item)
            if not os.path.isfile(task.get("script_path", "")):
                for col in range(5):
                    self.table.item(row, col).setForeground(QColor(LOG_ERROR))

    def _selected_task(self) -> Optional[dict]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        task_id = item.data(Qt.ItemDataRole.UserRole) if item else ""
        return self.cfg.task(task_id) if task_id else None

    def _add(self):
        dialog = TaskEditDialog(self.cfg, None, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ok, msg = self.cfg.add_task(dialog.task)
            if not ok:
                QMessageBox.warning(self, "保存失败", msg)
            self._reload()

    def _edit(self):
        task = self._selected_task()
        if not task:
            QMessageBox.information(self, "编辑", "请先选中一行")
            return
        dialog = TaskEditDialog(self.cfg, task, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ok, msg = self.cfg.update_task(task["task_id"], dialog.task)
            if not ok:
                QMessageBox.warning(self, "保存失败", msg)
            self._reload()

    def _remove(self):
        task = self._selected_task()
        if not task:
            QMessageBox.information(self, "删除", "请先选中一行")
            return
        if QMessageBox.question(self, "删除任务", f"确定删除「{task.get('task_name')}」？") != QMessageBox.StandardButton.Yes:
            return
        self.cfg.remove_task(task["task_id"])
        self._reload()

    def _duplicate(self):
        task = self._selected_task()
        if not task:
            QMessageBox.information(self, "复制", "请先选中一行")
            return
        ok, msg = self.cfg.duplicate_task(task["task_id"])
        if not ok:
            QMessageBox.warning(self, "复制失败", msg)
        self._reload()

    def _probe(self):
        task = self._selected_task()
        if not task:
            QMessageBox.information(self, "自检", "请先选中一行")
            return
        ok, msg = TM.probe_interpreter(task.get("python_interpreter", ""))
        if not ok:
            QMessageBox.warning(self, "自检失败", f"解释器：{msg}")
            return
        ok2, msg2 = TM.probe_script(task)
        QMessageBox.information(
            self,
            "自检结果",
            f"解释器：Python {msg}\n脚本：{'通过 - ' + msg2 if ok2 else '失败 - ' + msg2}",
        )


class RunTaskDialog(QDialog):
    def __init__(self, cfg: CH.ConfigHelper, serial: str, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("执行测试")
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        form.addRow("设备", QLabel(serial))

        self.task_combo = QComboBox()
        for task in cfg.enabled_tasks():
            label = task.get("task_name", task.get("task_id", ""))
            self.task_combo.addItem(label, task.get("task_id"))
        form.addRow("测试任务", self.task_combo)

        self.version_edit = QLineEdit()
        self.version_edit.setPlaceholderText("如 2.3.0（用于报告目录命名）")
        form.addRow("被测 App 版本", self.version_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept(self):
        if self.task_combo.count() == 0:
            QMessageBox.warning(self, "无法执行", "没有启用的测试任务，请先在「测试任务管理」中添加")
            return
        if not self.version_edit.text().strip():
            QMessageBox.warning(self, "无法执行", "请填写被测 App 版本")
            return
        self.accept()

    def result_values(self) -> Tuple[str, str]:
        return self.task_combo.currentData() or "", self.version_edit.text().strip()


class HistoryDialog(QDialog):
    def __init__(self, cfg: CH.ConfigHelper, serial: str, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.serial = serial
        self.setWindowTitle(f"历史报告 - {serial}")
        self.resize(900, 420)
        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["结束时间", "任务", "App 版本", "状态", "报告"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(lambda _: self._open_selected())
        layout.addWidget(self.table)

        row = QHBoxLayout()
        open_btn = QPushButton("打开报告")
        open_btn.clicked.connect(self._open_selected)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._reload)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(open_btn)
        row.addWidget(refresh_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)
        self._reload()

    def _reload(self):
        records = list(reversed(self.cfg.recent(self.serial)))
        self.table.setRowCount(0)
        for rec in records:
            row = self.table.rowCount()
            self.table.insertRow(row)
            path = rec.get("report_path", "")
            exists = bool(path) and os.path.isfile(path)
            status = C.TEST_STATUS_TEXT.get(rec.get("status", ""), rec.get("status", ""))
            cells = [rec.get("end_time", ""), rec.get("task_name", ""), rec.get("app_version", ""), status, path]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, path)
                if not exists:
                    item.setForeground(QColor("#9AA0A6"))
                self.table.setItem(row, col, item)

    def _open_selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            QMessageBox.information(self, "打开报告", "请先选中一行")
            return
        path = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) or ""
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "报告不存在", "该报告文件已被删除或移动，记录仍保留。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


# ---------------------------------------------------------------- 主窗口


class MainWindow(QMainWindow):
    COL_STATE, COL_SERIAL, COL_NOTE, COL_VERSION, COL_SCREEN, COL_TEST, COL_OPS = range(7)

    def __init__(self, cfg: CH.ConfigHelper):
        super().__init__()
        self.cfg = cfg
        self.devices: Dict[str, dict] = {}
        self.rows: Dict[str, dict] = {}
        self.row_of: Dict[str, int] = {}
        self.screen_procs: Dict[str, Any] = {}
        self.test_procs: Dict[str, dict] = {}
        self.log_lines: List[str] = []
        self._log_queue: List[Tuple[str, str]] = []
        self._updating = False
        self._last_adb_error = ""
        self._sort_key = "serial"
        self._sort_desc = False

        self.bridge = LogBridge()
        self.bridge.line.connect(self._on_remote_line)
        self.bridge.marker.connect(self._on_remote_marker)

        self.setWindowTitle(f"{C.APP_DISPLAY_NAME} {C.VERSION}")
        self.resize(1120, 700)
        self.setWindowIcon(self._make_icon())
        self._build_ui()
        self._build_tray()
        self._start_timers()
        self._bootstrap()

    # -------------------------------------------------------- UI 构建
    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        self.refresh_btn = QPushButton("刷新设备 (F5)")
        self.refresh_btn.clicked.connect(lambda: self._poll_devices(force=True))
        toolbar.addWidget(self.refresh_btn)

        versions_btn = QPushButton("Scrcpy 版本管理")
        versions_btn.clicked.connect(self._open_versions)
        toolbar.addWidget(versions_btn)

        tasks_btn = QPushButton("测试任务管理")
        tasks_btn.clicked.connect(self._open_tasks)
        toolbar.addWidget(tasks_btn)

        settings_btn = QPushButton("设置")
        settings_btn.clicked.connect(self._open_settings)
        toolbar.addWidget(settings_btn)

        disconnect_btn = QPushButton("全部断开投屏")
        disconnect_btn.clicked.connect(self._disconnect_all)
        toolbar.addWidget(disconnect_btn)

        toolbar.addStretch(1)
        self.version_label = QPushButton(f"v{C.VERSION}")
        self.version_label.setFlat(True)
        self.version_label.clicked.connect(lambda: AboutDialog(self).exec())
        toolbar.addWidget(self.version_label)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["状态", "序列号", "备注", "Scrcpy 版本", "投屏", "测试", "操作"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_STATE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SERIAL, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_NOTE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_VERSION, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SCREEN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_TEST, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_OPS, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        layout.addWidget(self.table, 1)

        self.log_label = QLabel("日志")
        layout.addWidget(self.log_label)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Menlo", 11))
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_view.document().setMaximumBlockCount(self.cfg.log_max_lines())
        layout.addWidget(self.log_view, 0)

        log_row = QHBoxLayout()
        export_btn = QPushButton("导出日志")
        export_btn.clicked.connect(self._export_log)
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self._clear_log)
        self.scroll_box = QCheckBox("自动滚动")
        self.scroll_box.setChecked(self.cfg.auto_scroll())
        self.scroll_box.toggled.connect(self._on_scroll_toggled)
        log_row.addWidget(export_btn)
        log_row.addWidget(clear_btn)
        log_row.addWidget(self.scroll_box)
        log_row.addStretch(1)
        layout.addLayout(log_row)

        self.setCentralWidget(central)
        shortcut = QShortcut(QKeySequence("F5"), self)
        shortcut.activated.connect(lambda: self._poll_devices(force=True))
        QTimer.singleShot(0, lambda: self.log_view.setMinimumHeight(150))

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self.tray.setToolTip(f"{C.APP_DISPLAY_NAME} {C.VERSION}")
        menu = QMenu(self)
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)
        screen_action = QAction("全部断开投屏", self)
        screen_action.triggered.connect(lambda: self._disconnect_all(silent=True))
        menu.addAction(screen_action)
        test_action = QAction("终止全部测试任务", self)
        test_action.triggered.connect(lambda: self._stop_all_tests(silent=True))
        menu.addAction(test_action)
        menu.addSeparator()
        quit_action = QAction("退出程序", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _start_timers(self):
        self.device_timer = QTimer(self)
        self.device_timer.timeout.connect(self._poll_devices)
        self.device_timer.start(self.cfg.poll_interval())

        self.proc_timer = QTimer(self)
        self.proc_timer.timeout.connect(self._monitor_processes)
        self.proc_timer.start(C.PROCESS_MONITOR_INTERVAL)

        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self._flush_logs)
        self.log_timer.start(C.LOG_FLUSH_INTERVAL_MS)

    def _make_icon(self) -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#1A73E8"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(4, 4, 56, 56, 14, 14)
        painter.setPen(QColor("#FFFFFF"))
        font = QFont()
        font.setPointSize(30)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "S")
        painter.end()
        return QIcon(pixmap)

    # -------------------------------------------------------- 启动引导
    def _bootstrap(self):
        if self.cfg.corrupt_backup:
            self.log("SYS", f"配置文件损坏，已备份为 {os.path.basename(self.cfg.corrupt_backup)} 并重建默认配置", "warn")

        adb = self.cfg.adb_path()
        if adb in ("", "adb") and not SM.autodetect_adb():
            self.log("SYS", "未找到 adb，请在「设置」中指定路径", "error")
        elif adb == "adb":
            detected = SM.autodetect_adb()
            if detected:
                self.cfg.set_adb_path(detected)
                self.log("SYS", f"自动识别 adb：{detected}")

        if not self.cfg.versions():
            detected = SM.autodetect_scrcpy()
            if detected:
                ok, info = SM.probe_scrcpy(detected)
                if ok:
                    self.cfg.add_version(detected, real_version=info, alias="自动识别")
                    self.log("SYS", f"自动识别 scrcpy：{info}")
                else:
                    self.log("SYS", f"自动识别 scrcpy 失败：{info}", "warn")
            else:
                self.log("SYS", "尚未添加 scrcpy 版本，请打开「Scrcpy 版本管理」添加", "warn")

        self._cleanup_orphans()
        self._poll_devices()

    def _cleanup_orphans(self):
        orphans = SM.list_orphan_scrcpy()
        if not orphans:
            return
        answer = QMessageBox.question(
            self,
            "发现遗留投屏进程",
            f"上次退出后仍有 {len(orphans)} 个 scrcpy 进程在运行，是否清理？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for pid, _command in orphans:
            SM.kill_pid(pid)
        self.log("SYS", f"已清理 {len(orphans)} 个遗留 scrcpy 进程")

    # -------------------------------------------------------- 日志
    def log(self, source: str, text: str, level: str = "info"):
        self._log_queue.append((build_log_line(source, text), level))

    def _on_remote_line(self, serial: str, source: str, text: str):
        level = "test" if source == "TEST" else "info"
        self._log_queue.append((build_log_line(source, text), level))

    def _on_remote_marker(self, serial: str, kind: str, payload: str):
        info = self.test_procs.get(serial)
        if info and kind in ("report_path", "result_json", "task_status"):
            self._log_queue.append((build_log_line("TEST", f"{kind} = {payload}"), "info"))

    def _flush_logs(self):
        if not self._log_queue:
            return
        batch, self._log_queue = self._log_queue, []
        scroll = self.scroll_box.isChecked()
        for text, level in batch:
            self.log_lines.append(text)
            self.log_view.setTextColor(QColor(LEVEL_COLORS.get(level, LOG_INFO)))
            self.log_view.append(text)
        overflow = len(self.log_lines) - self.cfg.log_max_lines()
        if overflow > 0:
            del self.log_lines[:overflow]
        if scroll:
            bar = self.log_view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _clear_log(self):
        self.log_view.clear()
        self.log_lines.clear()

    def _export_log(self):
        default = os.path.expanduser(f"~/ScrcpyTestPanel-{datetime.now():%Y%m%d-%H%M%S}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", default, "文本文件 (*.txt)")
        if not path:
            return
        self._flush_logs()
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.log_lines) + "\n")
        self.log("SYS", f"日志已导出：{path}")

    def _on_scroll_toggled(self, checked: bool):
        self.cfg.set_auto_scroll(checked)

    # -------------------------------------------------------- 设备轮询
    def _poll_devices(self, force: bool = False):
        devices, error = SM.list_devices(self.cfg.adb_path())
        if error:
            if error != self._last_adb_error:
                self._last_adb_error = error
                self.log("SYS", error, "error")
            return
        if self._last_adb_error:
            self.log("SYS", "adb 已恢复")
            self._last_adb_error = ""

        new_state: Dict[str, dict] = {}
        for device in devices:
            serial = device["serial"]
            prev = self.devices.get(serial, {})
            new_state[serial] = {
                "state": device["state"],
                "duplicate": device["duplicate"],
                "note": self.cfg.note(serial),
                "version_id": self.cfg.bound_version_id(serial) or self.cfg.default_version_id(),
                "screen": prev.get("screen", C.SC_STOPPED),
                "test": prev.get("test", "idle"),
            }
            if device["duplicate"] and not prev.get("duplicate"):
                self.log("SYS", f"检测到重复序列号 {serial}，状态可能串扰", "warn")

        # 设备不可用时，清理其上的投屏与测试
        for serial, row in new_state.items():
            if row["state"] != C.DEVICE_STATE_USABLE:
                self._on_device_unusable(serial)

        diff = compute_device_diff(self.devices, new_state)
        self.devices = new_state
        if diff["added"] or diff["removed"]:
            self._rebuild_table()
            for serial in diff["added"]:
                self.log("SYS", f"设备上线：{serial}")
            for serial in diff["removed"]:
                self.log("SYS", f"设备下线：{serial}", "warn")
        else:
            for serial, _delta in diff["changed"]:
                self._refresh_row(serial)

    def _on_device_unusable(self, serial: str):
        info = self.test_procs.get(serial)
        if info and not info.get("offline"):
            info["offline"] = True
            self.log("SYS", f"{serial} 设备不可用，终止进行中的测试", "warn")
            TM.terminate_test(info["proc"])
        proc = self.screen_procs.get(serial)
        if proc is not None and proc.poll() is None:
            SM.stop_scrcpy(proc)
            self.log("SCRCPY", f"{serial} 设备不可用，断开投屏", "warn")

    # -------------------------------------------------------- 表格
    def _sorted_serials(self) -> List[str]:
        key = self._sort_key

        def sort_key(serial: str):
            row = self.devices.get(serial, {})
            if key == "state":
                return row.get("state", "")
            if key == "note":
                return row.get("note", "")
            if key == "screen":
                return row.get("screen", "")
            if key == "test":
                return row.get("test", "")
            return serial

        return sorted(self.devices.keys(), key=sort_key, reverse=self._sort_desc)

    def _rebuild_table(self):
        selected = self._selected_serial()
        editing_serial = self._editing_serial()
        self._updating = True
        self.table.setRowCount(0)
        self.rows.clear()
        self.row_of.clear()
        for serial in self._sorted_serials():
            self._add_row(serial)
        self._updating = False
        if editing_serial:
            self.table.editItem(self.rows[editing_serial]["note_item"]) if editing_serial in self.rows else None
        if selected and selected in self.row_of:
            self.table.selectRow(self.row_of[selected])

    def _add_row(self, serial: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        row_data = self.devices.get(serial, {})

        state_item = QTableWidgetItem(C.DEVICE_STATE_TEXT.get(row_data.get("state", ""), "未知"))
        state_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        state_item.setData(Qt.ItemDataRole.UserRole, serial)
        if row_data.get("state") != C.DEVICE_STATE_USABLE:
            state_item.setForeground(QColor(LOG_ERROR))
        self.table.setItem(row, self.COL_STATE, state_item)

        serial_item = QTableWidgetItem(serial)
        serial_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        if row_data.get("duplicate"):
            serial_item.setForeground(QColor(LOG_ERROR))
            serial_item.setToolTip("序列号重复：状态可能串扰")
        self.table.setItem(row, self.COL_SERIAL, serial_item)

        note_item = QTableWidgetItem(row_data.get("note", ""))
        note_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, self.COL_NOTE, note_item)

        combo = QComboBox()
        self._populate_combo(combo, serial)
        combo.currentIndexChanged.connect(lambda _idx, s=serial, c=combo: self._on_version_changed(s, c))
        self.table.setCellWidget(row, self.COL_VERSION, combo)

        screen_item = QTableWidgetItem(C.SCREEN_STATUS_TEXT.get(row_data.get("screen", ""), "未启动"))
        screen_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, self.COL_SCREEN, screen_item)

        test_item = QTableWidgetItem(self._test_text(row_data.get("test", "idle")))
        test_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, self.COL_TEST, test_item)

        ops = QWidget()
        ops_layout = QHBoxLayout(ops)
        ops_layout.setContentsMargins(4, 2, 4, 2)
        ops_layout.setSpacing(6)
        screen_btn = QPushButton("投屏")
        screen_btn.setFixedWidth(96)
        screen_btn.clicked.connect(lambda _=False, s=serial: self._toggle_screen(s))
        test_btn = QPushButton("执行测试")
        test_btn.setFixedWidth(110)
        test_btn.clicked.connect(lambda _=False, s=serial: self._toggle_test(s))
        history_btn = QPushButton("历史")
        history_btn.setFixedWidth(64)
        history_btn.clicked.connect(lambda _=False, s=serial: HistoryDialog(self.cfg, s, self).exec())
        ops_layout.addWidget(screen_btn)
        ops_layout.addWidget(test_btn)
        ops_layout.addWidget(history_btn)
        ops_layout.addStretch(1)
        self.table.setCellWidget(row, self.COL_OPS, ops)

        self.rows[serial] = {
            "state_item": state_item,
            "serial_item": serial_item,
            "note_item": note_item,
            "combo": combo,
            "screen_item": screen_item,
            "test_item": test_item,
            "screen_btn": screen_btn,
            "test_btn": test_btn,
        }
        self.row_of[serial] = row
        self._apply_row_actions(serial)

    def _populate_combo(self, combo: QComboBox, serial: str):
        row_data = self.devices.get(serial, {})
        usable = row_data.get("state") == C.DEVICE_STATE_USABLE
        combo.blockSignals(True)
        combo.clear()
        versions = self.cfg.versions()
        for vid, entry in versions.items():
            label = entry.get("alias") or entry.get("real_version") or vid
            combo.addItem(label, vid)
        target = self.cfg.bound_version_id(serial) or self.cfg.default_version_id()
        index = combo.findData(target)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentIndex(-1)
            combo.addItem("未选择", "")
        combo.setEnabled(usable and bool(versions))
        combo.blockSignals(False)

    def _apply_row_actions(self, serial: str):
        row_data = self.devices.get(serial, {})
        widgets = self.rows.get(serial)
        if not widgets:
            return
        usable = row_data.get("state") == C.DEVICE_STATE_USABLE
        screen_running = serial in self.screen_procs and self.screen_procs[serial].poll() is None
        test_running = serial in self.test_procs

        widgets["screen_btn"].setText("断开" if screen_running else "投屏")
        widgets["screen_btn"].setEnabled(usable)
        widgets["test_btn"].setText("停止测试" if test_running else "执行测试")
        widgets["test_btn"].setEnabled(usable)

    def _refresh_row(self, serial: str):
        row_data = self.devices.get(serial)
        widgets = self.rows.get(serial)
        if row_data is None or widgets is None:
            return
        self._updating = True
        state_text = C.DEVICE_STATE_TEXT.get(row_data.get("state", ""), "未知")
        widgets["state_item"].setText(state_text)
        widgets["state_item"].setForeground(
            QColor(LOG_INFO) if row_data.get("state") == C.DEVICE_STATE_USABLE else QColor(LOG_ERROR)
        )
        # 备注：编辑中不覆盖
        if serial != self._editing_serial():
            widgets["note_item"].setText(row_data.get("note", ""))
        widgets["screen_item"].setText(C.SCREEN_STATUS_TEXT.get(row_data.get("screen", ""), "未启动"))
        widgets["test_item"].setText(self._test_text(row_data.get("test", "idle")))
        widgets["serial_item"].setForeground(QColor(LOG_ERROR) if row_data.get("duplicate") else QColor(LOG_INFO))
        if widgets["combo"].count() == 0 or row_data.get("version_id") != widgets["combo"].currentData():
            self._populate_combo(widgets["combo"], serial)
        self._updating = False
        self._apply_row_actions(serial)

    def _test_text(self, status: str) -> str:
        if status in ("", "idle"):
            return "空闲"
        if status == "running":
            return "运行中"
        return C.TEST_STATUS_TEXT.get(status, status)

    def _editing_serial(self) -> str:
        if self.table.state() != QAbstractItemView.State.EditingState:
            return ""
        index = self.table.currentIndex()
        if index.row() < 0:
            return ""
        item = self.table.item(index.row(), self.COL_SERIAL)
        return item.text() if item else ""

    def _selected_serial(self) -> str:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return ""
        item = self.table.item(rows[0].row(), self.COL_SERIAL)
        return item.text() if item else ""

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._updating or item.column() != self.COL_NOTE:
            return
        serial = self._serial_at(item.row())
        if not serial:
            return
        text = item.text().strip()
        self.cfg.set_note(serial, text)
        self.devices[serial]["note"] = text

    def _serial_at(self, row: int) -> str:
        item = self.table.item(row, self.COL_SERIAL)
        return item.text() if item else ""

    def _on_version_changed(self, serial: str, combo: QComboBox):
        if self._updating:
            return
        vid = combo.currentData() or ""
        self.cfg.bind_version(serial, vid or None)
        self.devices.setdefault(serial, {})["version_id"] = self.cfg.bound_version_id(serial) or self.cfg.default_version_id()
        self.log("SYS", f"{serial} 使用 scrcpy 版本：{vid or '默认'}")

    def _on_header_clicked(self, section: int):
        key = {
            self.COL_STATE: "state",
            self.COL_SERIAL: "serial",
            self.COL_NOTE: "note",
            self.COL_SCREEN: "screen",
            self.COL_TEST: "test",
        }.get(section)
        if not key:
            return
        if key == self._sort_key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = False
        self._rebuild_table()

    # -------------------------------------------------------- 投屏
    def _toggle_screen(self, serial: str):
        proc = self.screen_procs.get(serial)
        if proc is not None and proc.poll() is None:
            self._stop_screen(serial, manual=True)
            return

        row_data = self.devices.get(serial, {})
        if row_data.get("state") != C.DEVICE_STATE_USABLE:
            QMessageBox.warning(self, "无法投屏", "设备当前不可用（未授权 / 离线 / 无权限）")
            return

        vid, bin_path = self.cfg.effective_version(serial)
        if not bin_path:
            QMessageBox.warning(self, "无法投屏", "尚未配置 scrcpy 版本，请打开「Scrcpy 版本管理」")
            return

        ok, message = SM.probe_scrcpy(bin_path)
        if not ok:
            self._set_screen_status(serial, C.SC_START_ERROR)
            self.log("SCRCPY", f"{serial} 启动失败：{message}", "error")
            QMessageBox.warning(self, "scrcpy 不可用", message)
            return

        ok, message, proc = SM.start_scrcpy(serial, bin_path)
        if not ok or proc is None:
            self._set_screen_status(serial, C.SC_START_ERROR)
            self.log("SCRCPY", f"{serial} 启动失败：{message}", "error")
            QMessageBox.warning(self, "启动失败", message)
            return

        self.screen_procs[serial] = proc
        self._set_screen_status(serial, C.SC_RUNNING)
        self.log("SCRCPY", f"{serial} 投屏已启动（{vid} · {os.path.basename(bin_path)}）")
        accumulator = TM.OutputAccumulator()
        threading.Thread(
            target=_stream_reader,
            args=(self.bridge, serial, "SCRCPY", proc, accumulator),
            daemon=True,
        ).start()

    def _stop_screen(self, serial: str, manual: bool = False):
        proc = self.screen_procs.pop(serial, None)
        if proc is None:
            return
        result = SM.stop_scrcpy(proc)
        self._set_screen_status(serial, C.SC_STOPPED)
        note = "已断开" if result == "graceful" else "已强制结束"
        self.log("SCRCPY", f"{serial} 投屏{note}", "warn" if result == "killed" else "info")
        if manual:
            self._apply_row_actions(serial)

    def _disconnect_all(self, silent: bool = False):
        running = [s for s, p in self.screen_procs.items() if p.poll() is None]
        if not running:
            if not silent:
                QMessageBox.information(self, "全部断开", "当前没有正在投屏的设备")
            return
        if not silent:
            answer = QMessageBox.question(self, "全部断开投屏", f"将断开 {len(running)} 个设备，继续？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        for serial in running:
            self._stop_screen(serial)
        self.log("SYS", f"已断开 {len(running)} 个投屏")

    def _set_screen_status(self, serial: str, status: str):
        if serial in self.devices:
            self.devices[serial]["screen"] = status
            self._refresh_row(serial)

    # -------------------------------------------------------- 测试
    def _toggle_test(self, serial: str):
        if serial in self.test_procs:
            info = self.test_procs[serial]
            info["stopped"] = True
            self.log("TEST", f"{serial} 正在停止测试…", "warn")
            TM.terminate_test(info["proc"])
            return

        row_data = self.devices.get(serial, {})
        if row_data.get("state") != C.DEVICE_STATE_USABLE:
            QMessageBox.warning(self, "无法执行", "设备当前不可用（未授权 / 离线 / 无权限）")
            return
        if not self.cfg.enabled_tasks():
            QMessageBox.warning(self, "无法执行", "没有启用的测试任务，请先在「测试任务管理」中添加")
            return

        dialog = RunTaskDialog(self.cfg, serial, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        task_id, app_version = dialog.result_values()
        task = self.cfg.task(task_id)
        if not task:
            QMessageBox.warning(self, "无法执行", "任务不存在")
            return

        ok, message = CH.validate_task_template(task, check_fs=True)
        if not ok:
            QMessageBox.warning(self, "任务无效", message)
            return

        vid, _bin = self.cfg.effective_version(serial)
        command = TM.build_test_command(task, serial, app_version, vid)
        started, message, proc = TM.start_test(
            command,
            cwd=os.path.dirname(task.get("script_path", "")) or None,
            extra_env=task.get("extra_env"),
        )
        if not started or proc is None:
            self.devices[serial]["test"] = C.ST_START_ERROR
            self._refresh_row(serial)
            self.log("TEST", f"{serial} 启动测试失败：{message}", "error")
            QMessageBox.warning(self, "启动失败", message)
            return

        accumulator = TM.OutputAccumulator()
        self.test_procs[serial] = {
            "proc": proc,
            "acc": accumulator,
            "task_id": task.get("task_id", ""),
            "task_name": task.get("task_name", ""),
            "app_version": app_version,
            "scrcpy_version_id": vid,
            "start_time": time.time(),
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "timeout": int(task.get("timeout") or C.TASK_TIMEOUT_DEFAULT),
            "stopped": False,
            "timed_out": False,
            "offline": False,
        }
        self.devices[serial]["test"] = "running"
        self._refresh_row(serial)
        self.log("TEST", f"{serial} 开始执行「{task.get('task_name')}」 App 版本 {app_version}")
        threading.Thread(
            target=_stream_reader,
            args=(self.bridge, serial, "TEST", proc, accumulator),
            daemon=True,
        ).start()

    def _monitor_processes(self):
        for serial, proc in list(self.screen_procs.items()):
            code = proc.poll()
            if code is None:
                continue
            self.screen_procs.pop(serial, None)
            status = C.SC_EXITED
            self._set_screen_status(serial, status)
            self.log("SCRCPY", f"{serial} 投屏进程已退出（code={code}）", "warn" if code else "info")

        now = time.time()
        for serial, info in list(self.test_procs.items()):
            proc = info["proc"]
            code = proc.poll()
            if code is not None:
                self._finalize_test(serial, info, code)
                continue
            if now - info["start_time"] > info["timeout"]:
                info["timed_out"] = True
                self.log("TEST", f"{serial} 超过 {info['timeout']}s，终止测试", "warn")
                TM.terminate_test(proc)

    def _finalize_test(self, serial: str, info: dict, exit_code: Optional[int]):
        self.test_procs.pop(serial, None)
        acc: TM.OutputAccumulator = info["acc"]
        if info.get("offline"):
            status = C.ST_DEVICE_OFFLINE
        elif info.get("timed_out"):
            status = C.ST_TIMEOUT
        elif info.get("stopped"):
            status = C.ST_STOPPED
        else:
            status = TM.exit_code_to_status(exit_code)

        report_path = acc.report_path
        record = {
            "task_id": info["task_id"],
            "task_name": info["task_name"],
            "app_version": info["app_version"],
            "report_path": report_path,
            "start_time": info["started_at"],
            "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "exit_code": -1 if exit_code is None else int(exit_code),
        }
        self.cfg.add_recent(serial, record)

        if serial in self.devices:
            self.devices[serial]["test"] = status
            self._refresh_row(serial)

        level = C.TEST_STATUS_LEVEL.get(status, "info")
        text = f"{serial} 测试结束：{C.TEST_STATUS_TEXT.get(status, status)}（退出码 {record['exit_code']}）"
        self.log("TEST", text, level)
        if report_path and os.path.isfile(report_path):
            self.log("TEST", f"报告：{report_path}")
        elif report_path:
            self.log("TEST", f"报告路径不存在：{report_path}", "warn")

    def _stop_all_tests(self, silent: bool = False):
        running = list(self.test_procs.keys())
        if not running:
            if not silent:
                QMessageBox.information(self, "终止测试", "当前没有运行中的测试任务")
            return
        if not silent:
            answer = QMessageBox.question(self, "终止全部测试", f"将终止 {len(running)} 个测试任务，继续？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        for serial in running:
            info = self.test_procs.get(serial)
            if not info:
                continue
            info["stopped"] = True
            TM.terminate_test(info["proc"])
        self.log("TEST", f"已请求终止 {len(running)} 个测试任务", "warn")

    # -------------------------------------------------------- 弹窗入口
    def _open_settings(self):
        dialog = SettingsDialog(self.cfg, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.device_timer.start(self.cfg.poll_interval())
            self.log_view.document().setMaximumBlockCount(self.cfg.log_max_lines())
            self.scroll_box.setChecked(self.cfg.auto_scroll())
            self._poll_devices(force=True)
            self.log("SYS", "设置已保存")

    def _open_versions(self):
        VersionManagerDialog(self.cfg, self).exec()
        for serial in list(self.devices.keys()):
            self.devices[serial]["version_id"] = self.cfg.bound_version_id(serial) or self.cfg.default_version_id()
            self._refresh_row(serial)

    def _open_tasks(self):
        TaskManagerDialog(self.cfg, self).exec()

    # -------------------------------------------------------- 托盘 / 退出
    def _on_tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._show_window()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):  # noqa: N802
        if self.tray.isVisible():
            self.hide()
            self.tray.showMessage(C.APP_DISPLAY_NAME, "已最小化到菜单栏，点击图标恢复", QSystemTrayIcon.MessageIcon.Information, 2000)
            event.ignore()
        else:
            self._shutdown()
            event.accept()

    def _quit(self):
        running_screen = [s for s, p in self.screen_procs.items() if p.poll() is None]
        running_test = list(self.test_procs.keys())
        if running_screen or running_test:
            answer = QMessageBox.question(
                self,
                "退出程序",
                f"仍有 {len(running_screen)} 个投屏、{len(running_test)} 个测试在运行，退出将全部终止。继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        for serial, proc in list(self.screen_procs.items()):
            try:
                SM.stop_scrcpy(proc)
            except Exception:  # noqa: BLE001
                pass
        self.screen_procs.clear()
        for serial, info in list(self.test_procs.items()):
            try:
                TM.terminate_test(info["proc"])
            except Exception:  # noqa: BLE001
                pass
        self.test_procs.clear()
        try:
            self.tray.hide()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 自检


def _check_modules() -> Tuple[bool, str]:
    import importlib

    for name in ("config_helper", "scrcpy_manager", "test_manager", "constants"):
        importlib.import_module(name)
    return True, "4 modules"


def _check_pyside() -> Tuple[bool, str]:
    from PySide6.QtWidgets import QApplication  # noqa: F401

    import PySide6

    return True, f"PySide6 {PySide6.__version__}"


def _check_version() -> Tuple[bool, str]:
    if not C.VERSION or C.VERSION == "unknown":
        return False, "VERSION 未注入"
    return True, C.VERSION


def _check_config_roundtrip(tmp: str) -> Tuple[bool, str]:
    path = os.path.join(tmp, "config.json")
    cfg = CH.ConfigHelper(path)
    cfg.set_poll_interval(3000)
    cfg.set_adb_path("adb")
    if not os.path.isfile(path):
        return False, "配置未落盘"
    cfg2 = CH.ConfigHelper(path)
    cfg2.load()
    if cfg2.data.get("poll_interval") != 3000:
        return False, "重读值不一致"
    return True, "roundtrip"


def _check_corrupt(tmp: str) -> Tuple[bool, str]:
    path = os.path.join(tmp, "broken.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{this is not json")
    cfg = CH.ConfigHelper(path)
    data = cfg.load()
    if not cfg.corrupt_backup or not os.path.isfile(cfg.corrupt_backup):
        return False, "未生成损坏备份"
    if data.get("config_version") != C.CONFIG_VERSION:
        return False, "重建配置不完整"
    with open(path, encoding="utf-8") as f:
        json.load(f)
    return True, "backup+rebuild"


def _check_migration(tmp: str) -> Tuple[bool, str]:
    path = os.path.join(tmp, "legacy.json")
    legacy = {
        "scrcpy_version_lib": {"v1": {"bin_path": "/tmp/scrcpy", "real_version": "scrcpy 1.0"}},
        "device_note": {"SN1": "旧配置"},
        "poll_interval": 999999,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(legacy, f)
    cfg = CH.ConfigHelper(path)
    data = cfg.load()
    if data.get("config_version") != C.CONFIG_VERSION:
        return False, "config_version 未补齐"
    if "adb_bin_path" not in data or "log_max_lines" not in data:
        return False, "缺失字段未补默认"
    if data["scrcpy_version_lib"]["v1"]["bin_path"] != "/tmp/scrcpy":
        return False, "版本库丢失"
    if data["device_note"].get("SN1") != "旧配置":
        return False, "备注丢失"
    if data["poll_interval"] != C.POLL_INTERVAL_MAX:
        return False, "轮询间隔未 clamp"
    return True, "migrated"


def _check_clamp(tmp: str) -> Tuple[bool, str]:
    cfg = CH.ConfigHelper(os.path.join(tmp, "clamp.json"))
    low = cfg.set_poll_interval(10)
    high = cfg.set_poll_interval(999999)
    if low != C.POLL_INTERVAL_MIN or high != C.POLL_INTERVAL_MAX:
        return False, f"clamp 结果 {low}/{high}"
    return True, f"{low}/{high}"


def _check_task_rules(tmp: str) -> Tuple[bool, str]:
    task = CH.normalize_task_template(
        {
            "task_id": "a",
            "task_name": "t",
            "script_path": "/tmp/x.py",
            "python_interpreter": "/tmp/python",
            "report_root": "/tmp/reports",
            "timeout": 0,
        }
    )
    if task["timeout"] != C.TASK_TIMEOUT_DEFAULT:
        return False, f"timeout 未回退：{task['timeout']}"
    ok, _msg = CH.validate_task_templates([{"task_id": "a"}, {"task_id": "a"}])
    if ok:
        return False, "重复 task_id 未被拒绝"
    return True, "fallback+unique"


def _check_safe_segment(tmp: str) -> Tuple[bool, str]:
    r1 = CH.safe_path_segment("../x")
    r2 = CH.safe_path_segment("a/b")
    r3 = CH.safe_path_segment("bad\x00name")
    if ".." in r1 or "/" in r1:
        return False, f"未清洗：{r1}"
    if "/" in r2 or "\\" in r2:
        return False, f"未清洗：{r2}"
    if "\x00" in r3:
        return False, "控制字符未清洗"
    return True, f"{r1}|{r2}"


def _check_output_parse(tmp: str) -> Tuple[bool, str]:
    acc = TM.OutputAccumulator()
    acc.feed(b"REPORT_PA")
    if acc.report_path:
        return False, "不完整行被误解析"
    acc.feed(b"TH:/tmp/first.html\n")
    if acc.report_path != "/tmp/first.html":
        return False, f"跨块解析失败：{acc.report_path!r}"
    acc.feed(b"\x1b[32mREPORT_PATH:/tmp/last.html\x1b[0m\n")
    if acc.report_path != "/tmp/last.html":
        return False, f"未取最后一条或 ANSI 未剥离：{acc.report_path!r}"
    return True, "chunk+last+ansi"


def _check_exit_mapping(tmp: str) -> Tuple[bool, str]:
    if TM.exit_code_to_status(0) != C.ST_SUCCESS:
        return False, "0 未映射为 success"
    if TM.exit_code_to_status(1) != C.ST_FAILED:
        return False, "1 未映射为 failed"
    if TM.exit_code_to_status(130, stopped=True) != C.ST_STOPPED:
        return False, "130 未映射为 stopped"
    return True, "0/1/130"


def _check_device_diff(tmp: str) -> Tuple[bool, str]:
    old = {"SN1": {"state": "device", "note": "", "screen": "stopped", "test": "idle"}}
    new = {
        "SN1": {"state": "offline", "note": "", "screen": "stopped", "test": "idle"},
        "SN2": {"state": "device", "note": "", "screen": "stopped", "test": "idle"},
    }
    diff = compute_device_diff(old, new)
    if diff["added"] != ["SN2"]:
        return False, f"added 错误：{diff['added']}"
    if diff["removed"]:
        return False, f"removed 错误：{diff['removed']}"
    if not diff["changed"] or diff["changed"][0][0] != "SN1":
        return False, "changed 未识别状态变化"
    delta = diff["changed"][0][1]
    if delta.get("state") != ("device", "offline"):
        return False, f"delta 错误：{delta}"
    return True, "add/remove/change"


def run_selftest() -> int:
    total = 12
    tmp = tempfile.mkdtemp(prefix="stp-selftest-")
    items: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
        ("import modules", _check_modules),
        ("import pyside6", _check_pyside),
        ("version info", _check_version),
        ("config roundtrip", lambda: _check_config_roundtrip(tmp)),
        ("config corrupt recovery", lambda: _check_corrupt(tmp)),
        ("config migration", lambda: _check_migration(tmp)),
        ("poll interval clamp", lambda: _check_clamp(tmp)),
        ("task template rules", lambda: _check_task_rules(tmp)),
        ("path segment safety", lambda: _check_safe_segment(tmp)),
        ("report marker parse", lambda: _check_output_parse(tmp)),
        ("exit code mapping", lambda: _check_exit_mapping(tmp)),
        ("device diff", lambda: _check_device_diff(tmp)),
    ]

    print(f"SELFTEST BEGIN {C.VERSION}")
    failures: List[str] = []
    try:
        for index, (name, func) in enumerate(items, start=1):
            try:
                ok, detail = func()
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            if ok:
                print(f"[{index}/{total}] {name:.<30} PASS  ({detail})")
            else:
                print(f"[{index}/{total}] {name:.<30} FAIL: {detail}")
                failures.append(name)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"SELFTEST FAIL: {failures[0]}")
        return 1
    print("SELFTEST OK")
    return 0


# ---------------------------------------------------------------- 入口


def _acquire_instance() -> Tuple[bool, Optional[QLockFile]]:
    lock = QLockFile(os.path.join(C.config_dir(), "panel.lock"))
    lock.setStaleLockTime(0)
    if lock.tryLock(200):
        return True, lock
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if socket.waitForConnected(1000):
        socket.write(b"show")
        socket.flush()
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()
    return False, lock


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv if argv is None else argv)
    if "--selftest" in args:
        return run_selftest()

    app = QApplication(args)
    app.setApplicationName(C.APP_NAME)
    app.setApplicationDisplayName(C.APP_DISPLAY_NAME)
    app.setApplicationVersion(C.VERSION)

    os.makedirs(C.config_dir(), exist_ok=True)

    acquired, lock = _acquire_instance()
    if not acquired:
        QMessageBox.information(None, C.APP_DISPLAY_NAME, "已有一个实例正在运行，请从菜单栏图标打开。")
        return 0

    cfg = CH.ConfigHelper()
    cfg.load()

    window = MainWindow(cfg)

    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    if server.listen(SERVER_NAME):
        server.newConnection.connect(lambda: _handle_new_connection(server, window))

    def _excepthook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            path = os.path.join(C.config_dir(), f"crash-{datetime.now():%Y%m%d-%H%M%S}.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
                f.write("\n--- last logs ---\n")
                f.write("\n".join(window.log_lines[-C.CRASH_LOG_KEEP_LINES :]))
        except Exception:  # noqa: BLE001
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook
    window.show()
    return app.exec()


def _handle_new_connection(server: QLocalServer, window: "MainWindow"):
    client = server.nextPendingConnection()
    if client is None:
        return
    client.waitForReadyRead(1000)
    client.readAll()
    window._show_window()
    client.disconnectFromServer()


if __name__ == "__main__":
    raise SystemExit(main())
