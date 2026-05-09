# tenk_app.py  (REVISED: crash-proof + diagnostics + remove deprecated DPI attribute)
"""
10,000 (family rules) Strategy Lab - Frontend GUI
=================================================

Depends on tenk_core.py in same folder.

Key fixes in this revision:
- Removes deprecated AA_EnableHighDpiScaling call (was only a warning, but noisy)
- Installs a global exception hook + Qt message handler so exceptions don't silently close the app
- Wraps plot updates in try/except and logs exceptions instead of crashing
- Adds explicit, discoverable diagnostics:
  - Help -> Run Backend Smoke Test
  - Help -> Copy Startup Diagnostics
  - Diagnostics group with buttons
"""

from __future__ import annotations

import os
import sys
import json
import traceback
import csv
import time
import platform
import warnings
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

# --- Qt imports ---
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

# --- matplotlib imports ---
import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar

# --- backend import ---
try:
    import tenk_core as core
except Exception as e:
    core = None
    _IMPORT_ERR = e
else:
    _IMPORT_ERR = None


APP_ORG = "tenk"
APP_NAME = "TenThousand Strategy Lab"


# =========================
# Utility
# =========================

def show_error(parent: QtWidgets.QWidget, title: str, message: str, details: str = "") -> None:
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    if details:
        box.setDetailedText(details)
    box.exec()

def show_info(parent: QtWidgets.QWidget, title: str, message: str) -> None:
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Information)
    box.setWindowTitle(title)
    box.setText(message)
    box.exec()

def safe_int(s: Any, default: int = 0) -> int:
    try:
        return int(s)
    except Exception:
        return default


# =========================
# Ladder Table Model
# =========================

LADDER_COLUMNS = [
    ("Priority", "priority"),
    ("Enabled", "enabled"),
    ("Rule Type", "rule_type"),
    ("p1", "p1"),
    ("p2", "p2"),
    ("Note", "note"),
]

RULE_TYPES = [rt.value for rt in core.RuleType] if core else []


class LadderTableModel(QtCore.QAbstractTableModel):
    def __init__(self, rows: List["core.LadderRuleRow"], parent=None):
        super().__init__(parent)
        self._rows = rows

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return len(LADDER_COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return LADDER_COLUMNS[section][0]
        return str(section + 1)

    def flags(self, index: QtCore.QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemIsEnabled
        col_name = LADDER_COLUMNS[index.column()][1]
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if col_name == "enabled":
            flags |= Qt.ItemIsUserCheckable | Qt.ItemIsEditable
        else:
            flags |= Qt.ItemIsEditable
        return flags

    def data(self, index: QtCore.QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col_name = LADDER_COLUMNS[index.column()][1]

        if role == Qt.DisplayRole or role == Qt.EditRole:
            if col_name == "priority":
                return row.priority
            if col_name == "enabled":
                return "" if role == Qt.DisplayRole else row.enabled
            if col_name == "rule_type":
                return row.rule_type.value
            if col_name == "p1":
                return row.p1
            if col_name == "p2":
                return row.p2
            if col_name == "note":
                return row.note
        if role == Qt.CheckStateRole and col_name == "enabled":
            return Qt.Checked if row.enabled else Qt.Unchecked
        return None

    def setData(self, index: QtCore.QModelIndex, value, role: int = Qt.EditRole) -> bool:
        if not index.isValid():
            return False
        r = self._rows[index.row()]
        col_name = LADDER_COLUMNS[index.column()][1]

        try:
            if col_name == "enabled":
                if role == Qt.CheckStateRole:
                    r.enabled = (value == Qt.Checked)
                elif role == Qt.EditRole:
                    r.enabled = bool(value)
                else:
                    return False
            elif col_name == "priority":
                r.priority = int(value)
            elif col_name == "rule_type":
                r.rule_type = core.RuleType(str(value))
            elif col_name == "p1":
                r.p1 = float(value)
            elif col_name == "p2":
                r.p2 = float(value)
            elif col_name == "note":
                r.note = str(value)
            else:
                return False
        except Exception:
            return False

        self.dataChanged.emit(index, index, [role])
        return True

    def insertRow(self, row: int, parent=QtCore.QModelIndex(), obj: Optional["core.LadderRuleRow"] = None) -> bool:
        self.beginInsertRows(parent, row, row)
        if obj is None:
            obj = core.LadderRuleRow(priority=10*(row+1), enabled=True, rule_type=core.RuleType.MAX_POINTS, p1=0.0, p2=0.0, note="")
        self._rows.insert(row, obj)
        self.endInsertRows()
        return True

    def removeRow(self, row: int, parent=QtCore.QModelIndex()) -> bool:
        if row < 0 or row >= len(self._rows):
            return False
        self.beginRemoveRows(parent, row, row)
        del self._rows[row]
        self.endRemoveRows()
        return True

    def moveRowUp(self, row: int) -> None:
        if row <= 0 or row >= len(self._rows):
            return
        self.beginMoveRows(QtCore.QModelIndex(), row, row, QtCore.QModelIndex(), row-1)
        self._rows[row-1], self._rows[row] = self._rows[row], self._rows[row-1]
        self.endMoveRows()

    def moveRowDown(self, row: int) -> None:
        if row < 0 or row >= len(self._rows)-1:
            return
        self.beginMoveRows(QtCore.QModelIndex(), row, row, QtCore.QModelIndex(), row+2)
        self._rows[row+1], self._rows[row] = self._rows[row], self._rows[row+1]
        self.endMoveRows()

    def rows(self) -> List["core.LadderRuleRow"]:
        return self._rows


class RuleTypeDelegate(QtWidgets.QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        cb = QtWidgets.QComboBox(parent)
        cb.addItems(RULE_TYPES)
        cb.setEditable(False)
        return cb

    def setEditorData(self, editor, index):
        val = index.data(Qt.DisplayRole)
        i = editor.findText(str(val))
        if i >= 0:
            editor.setCurrentIndex(i)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)


# =========================
# Plot Panels
# =========================

class PlotPanel(QtWidgets.QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.figure = Figure(figsize=(6, 4), dpi=100)
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, self)
        self.ax = self.figure.add_subplot(111)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)

    def clear(self):
        self.figure.clear()
        self.ax = self.figure.add_subplot(111)
        self.ax.set_title(self.title)
        self.canvas.draw_idle()

    def set_message(self, msg: str):
        self.clear()
        self.ax.text(0.5, 0.5, msg, ha="center", va="center", transform=self.ax.transAxes)
        self.ax.set_axis_off()
        self.canvas.draw_idle()


# =========================
# Background Worker
# =========================

class SimWorker(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal(object)  # SimResult
    failed = QtCore.Signal(str, str)  # message, traceback

    def __init__(self, ruleset: "core.Ruleset", strategy: "core.StrategyConfig", simcfg: "core.SimConfig", cancel: "core.CancelToken"):
        super().__init__()
        self.ruleset = ruleset
        self.strategy = strategy
        self.simcfg = simcfg
        self.cancel = cancel

    @QtCore.Slot()
    def run(self):
        try:
            def cb(done: int, total: int):
                self.progress.emit(done, total)
            sim = core.simulate_games(self.ruleset, self.strategy, self.simcfg, progress_cb=cb, cancel_token=self.cancel)
            self.finished.emit(sim)
        except Exception as e:
            tb = traceback.format_exc()
            self.failed.emit(str(e), tb)


# =========================
# Strategy / Ruleset Editor
# =========================

class StrategyEditor(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._updating = False

        self.ruleset = core.default_ruleset()
        self.strategy = core.default_strategy_config(300)

        self._build_ui()
        self.load_from_objects(self.ruleset, self.strategy)

    def _build_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        v = QtWidgets.QVBoxLayout(content)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(10)

        # Ruleset
        self.grp_rules = QtWidgets.QGroupBox("Ruleset (Adjustable)")
        v.addWidget(self.grp_rules)
        form_r = QtWidgets.QFormLayout(self.grp_rules)

        self.sp_target = QtWidgets.QSpinBox(); self.sp_target.setRange(1000, 500000); self.sp_target.setSingleStep(250)
        self.sp_entry = QtWidgets.QSpinBox(); self.sp_entry.setRange(0, 50000); self.sp_entry.setSingleStep(50)
        self.sp_endzone = QtWidgets.QSpinBox(); self.sp_endzone.setRange(0, 500000); self.sp_endzone.setSingleStep(50)

        self.sp_single1 = QtWidgets.QSpinBox(); self.sp_single1.setRange(0, 5000); self.sp_single1.setSingleStep(10)
        self.sp_single5 = QtWidgets.QSpinBox(); self.sp_single5.setRange(0, 2000); self.sp_single5.setSingleStep(10)

        self.sp_small_straight = QtWidgets.QSpinBox(); self.sp_small_straight.setRange(0, 50000); self.sp_small_straight.setSingleStep(50)
        self.sp_large_straight = QtWidgets.QSpinBox(); self.sp_large_straight.setRange(0, 50000); self.sp_large_straight.setSingleStep(50)

        self.chk_hot_optional = QtWidgets.QCheckBox("Hot dice reroll is optional")
        self.chk_hot_only_if_all_taken = QtWidgets.QCheckBox("Hot dice only if you actually take all dice")

        form_r.addRow("Target score", self.sp_target)
        form_r.addRow("Entry threshold (must reach this in-turn to bank)", self.sp_entry)
        form_r.addRow("End-zone starts at total score ≥", self.sp_endzone)
        form_r.addRow("Single 1 points", self.sp_single1)
        form_r.addRow("Single 5 points", self.sp_single5)
        form_r.addRow("5-straight score", self.sp_small_straight)
        form_r.addRow("6-straight score", self.sp_large_straight)
        form_r.addRow(self.chk_hot_optional)
        form_r.addRow(self.chk_hot_only_if_all_taken)

        self.btn_reset_rules = QtWidgets.QPushButton("Reset Ruleset to Defaults")
        form_r.addRow(self.btn_reset_rules)

        # Strategy
        self.grp_strat = QtWidgets.QGroupBox("Banking Policy")
        v.addWidget(self.grp_strat)
        form_s = QtWidgets.QFormLayout(self.grp_strat)

        self.sp_bank_normal = QtWidgets.QSpinBox(); self.sp_bank_normal.setRange(0, 50000); self.sp_bank_normal.setSingleStep(25)
        self.sp_bank_post_hot = QtWidgets.QSpinBox(); self.sp_bank_post_hot.setRange(0, 50000); self.sp_bank_post_hot.setSingleStep(25)

        self.sp_low1 = QtWidgets.QSpinBox(); self.sp_low1.setRange(0, 50000); self.sp_low1.setSingleStep(25)
        self.sp_low2 = QtWidgets.QSpinBox(); self.sp_low2.setRange(0, 50000); self.sp_low2.setSingleStep(25)

        self.sp_near1_pts = QtWidgets.QSpinBox(); self.sp_near1_pts.setRange(0, 20000); self.sp_near1_pts.setSingleStep(50)
        self.sp_near1_thr = QtWidgets.QSpinBox(); self.sp_near1_thr.setRange(0, 50000); self.sp_near1_thr.setSingleStep(25)
        self.sp_near2_pts = QtWidgets.QSpinBox(); self.sp_near2_pts.setRange(0, 20000); self.sp_near2_pts.setSingleStep(50)
        self.sp_near2_thr = QtWidgets.QSpinBox(); self.sp_near2_thr.setRange(0, 50000); self.sp_near2_thr.setSingleStep(25)

        self.chk_trace = QtWidgets.QCheckBox("Enable ladder trace (debug)")
        self.chk_trace.setChecked(True)

        form_s.addRow("Bank threshold (normal)", self.sp_bank_normal)
        form_s.addRow("Bank threshold (post-hot-dice)", self.sp_bank_post_hot)
        form_s.addRow("Low dice override (1 die) — threshold >=", self.sp_low1)
        form_s.addRow("Low dice override (2 dice) — threshold >=", self.sp_low2)

        near_box = QtWidgets.QGroupBox("Near-goal overrides (points needed → bank threshold)")
        near_form = QtWidgets.QGridLayout(near_box)
        near_form.addWidget(QtWidgets.QLabel("If points_needed ≤"), 0, 0)
        near_form.addWidget(QtWidgets.QLabel("Then bank threshold ="), 0, 1)
        near_form.addWidget(self.sp_near1_pts, 1, 0)
        near_form.addWidget(self.sp_near1_thr, 1, 1)
        near_form.addWidget(self.sp_near2_pts, 2, 0)
        near_form.addWidget(self.sp_near2_thr, 2, 1)
        form_s.addRow(near_box)
        form_s.addRow(self.chk_trace)

        # Ladder
        self.grp_ladder = QtWidgets.QGroupBox("Take Policy — Priority Ladder (Option Ranking)")
        v.addWidget(self.grp_ladder)
        ladder_v = QtWidgets.QVBoxLayout(self.grp_ladder)

        self.ladder_model = LadderTableModel(self.strategy.ladder)
        self.tbl_ladder = QtWidgets.QTableView()
        self.tbl_ladder.setModel(self.ladder_model)
        self.tbl_ladder.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tbl_ladder.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tbl_ladder.horizontalHeader().setStretchLastSection(True)
        self.tbl_ladder.verticalHeader().setVisible(False)
        self.tbl_ladder.setAlternatingRowColors(True)
        self.tbl_ladder.setItemDelegateForColumn(2, RuleTypeDelegate(self.tbl_ladder))
        ladder_v.addWidget(self.tbl_ladder)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_add_rule = QtWidgets.QPushButton("Add")
        self.btn_del_rule = QtWidgets.QPushButton("Remove")
        self.btn_up_rule = QtWidgets.QPushButton("Move Up")
        self.btn_dn_rule = QtWidgets.QPushButton("Move Down")
        btn_row.addWidget(self.btn_add_rule)
        btn_row.addWidget(self.btn_del_rule)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_up_rule)
        btn_row.addWidget(self.btn_dn_rule)
        ladder_v.addLayout(btn_row)

        # Presets
        self.grp_presets = QtWidgets.QGroupBox("Presets / IO")
        v.addWidget(self.grp_presets)
        p = QtWidgets.QGridLayout(self.grp_presets)

        self.ed_strategy_name = QtWidgets.QLineEdit()
        self.btn_save_strategy = QtWidgets.QPushButton("Save Strategy JSON…")
        self.btn_load_strategy = QtWidgets.QPushButton("Load Strategy JSON…")
        self.btn_save_ruleset = QtWidgets.QPushButton("Save Ruleset JSON…")
        self.btn_load_ruleset = QtWidgets.QPushButton("Load Ruleset JSON…")
        self.btn_save_preset = QtWidgets.QPushButton("Save Combined Preset…")
        self.btn_load_preset = QtWidgets.QPushButton("Load Combined Preset…")

        p.addWidget(QtWidgets.QLabel("Strategy name"), 0, 0)
        p.addWidget(self.ed_strategy_name, 0, 1, 1, 2)
        p.addWidget(self.btn_save_strategy, 1, 0)
        p.addWidget(self.btn_load_strategy, 1, 1)
        p.addWidget(self.btn_save_ruleset, 2, 0)
        p.addWidget(self.btn_load_ruleset, 2, 1)
        p.addWidget(self.btn_save_preset, 3, 0)
        p.addWidget(self.btn_load_preset, 3, 1)

        v.addStretch(1)

        # Signals
        for w in [
            self.sp_target, self.sp_entry, self.sp_endzone,
            self.sp_single1, self.sp_single5, self.sp_small_straight, self.sp_large_straight,
            self.chk_hot_optional, self.chk_hot_only_if_all_taken,
            self.sp_bank_normal, self.sp_bank_post_hot,
            self.sp_low1, self.sp_low2,
            self.sp_near1_pts, self.sp_near1_thr, self.sp_near2_pts, self.sp_near2_thr,
            self.chk_trace, self.ed_strategy_name,
        ]:
            if isinstance(w, QtWidgets.QAbstractButton):
                w.toggled.connect(self._on_any_changed)
            elif isinstance(w, QtWidgets.QLineEdit):
                w.textChanged.connect(self._on_any_changed)
            else:
                w.valueChanged.connect(self._on_any_changed)

        self.ladder_model.dataChanged.connect(self._on_any_changed)
        self.btn_reset_rules.clicked.connect(self._reset_ruleset)

        self.btn_add_rule.clicked.connect(self._add_rule)
        self.btn_del_rule.clicked.connect(self._del_rule)
        self.btn_up_rule.clicked.connect(self._move_rule_up)
        self.btn_dn_rule.clicked.connect(self._move_rule_down)

        self.btn_save_strategy.clicked.connect(self._save_strategy_dialog)
        self.btn_load_strategy.clicked.connect(self._load_strategy_dialog)
        self.btn_save_ruleset.clicked.connect(self._save_ruleset_dialog)
        self.btn_load_ruleset.clicked.connect(self._load_ruleset_dialog)
        self.btn_save_preset.clicked.connect(self._save_preset_dialog)
        self.btn_load_preset.clicked.connect(self._load_preset_dialog)

    def _on_any_changed(self):
        if self._updating:
            return
        self.changed.emit()

    def _reset_ruleset(self):
        self.load_from_objects(core.default_ruleset(), self.strategy)
        self.changed.emit()

    def _add_rule(self):
        row = self.ladder_model.rowCount()
        self.ladder_model.insertRow(row)
        self.tbl_ladder.selectRow(row)
        self.changed.emit()

    def _del_rule(self):
        idx = self.tbl_ladder.currentIndex()
        if not idx.isValid():
            return
        self.ladder_model.removeRow(idx.row())
        self.changed.emit()

    def _move_rule_up(self):
        idx = self.tbl_ladder.currentIndex()
        if not idx.isValid():
            return
        r = idx.row()
        self.ladder_model.moveRowUp(r)
        self.tbl_ladder.selectRow(max(0, r-1))
        self.changed.emit()

    def _move_rule_down(self):
        idx = self.tbl_ladder.currentIndex()
        if not idx.isValid():
            return
        r = idx.row()
        self.ladder_model.moveRowDown(r)
        self.tbl_ladder.selectRow(min(self.ladder_model.rowCount()-1, r+1))
        self.changed.emit()

    def load_from_objects(self, ruleset: "core.Ruleset", strategy: "core.StrategyConfig"):
        self._updating = True
        try:
            self.ruleset = ruleset
            self.strategy = strategy

            self.sp_target.setValue(ruleset.target_score)
            self.sp_entry.setValue(ruleset.entry_threshold)
            self.sp_endzone.setValue(ruleset.endzone_start)
            self.sp_single1.setValue(ruleset.single_1)
            self.sp_single5.setValue(ruleset.single_5)
            self.sp_small_straight.setValue(ruleset.small_straight_score)
            self.sp_large_straight.setValue(ruleset.large_straight_score)
            self.chk_hot_optional.setChecked(ruleset.hot_dice_optional)
            self.chk_hot_only_if_all_taken.setChecked(ruleset.hot_dice_triggers_only_if_all_taken)

            self.ed_strategy_name.setText(strategy.name)
            self.sp_bank_normal.setValue(strategy.bank_threshold_normal)
            self.sp_bank_post_hot.setValue(strategy.bank_threshold_post_hot)
            self.sp_low1.setValue(strategy.low_dice_thresholds.get(1, 0))
            self.sp_low2.setValue(strategy.low_dice_thresholds.get(2, 0))

            ng = strategy.near_goal_thresholds[:] if strategy.near_goal_thresholds else [(250, 150), (500, 250)]
            while len(ng) < 2:
                ng.append((500, 250))
            self.sp_near1_pts.setValue(ng[0][0])
            self.sp_near1_thr.setValue(ng[0][1])
            self.sp_near2_pts.setValue(ng[1][0])
            self.sp_near2_thr.setValue(ng[1][1])

            self.chk_trace.setChecked(strategy.enable_trace)

            self.ladder_model.beginResetModel()
            self.ladder_model._rows = strategy.ladder
            self.ladder_model.endResetModel()
        finally:
            self._updating = False

    def to_objects(self) -> Tuple["core.Ruleset", "core.StrategyConfig"]:
        rs = core.Ruleset(
            schema_version=core.SCHEMA_VERSION,
            target_score=int(self.sp_target.value()),
            entry_threshold=int(self.sp_entry.value()),
            endzone_start=int(self.sp_endzone.value()),
            single_1=int(self.sp_single1.value()),
            single_5=int(self.sp_single5.value()),
            set_base=self.ruleset.set_base,  # leaving set_base fixed for now
            small_straight_score=int(self.sp_small_straight.value()),
            small_straights=self.ruleset.small_straights,
            large_straight_score=int(self.sp_large_straight.value()),
            large_straight=self.ruleset.large_straight,
            hot_dice_optional=bool(self.chk_hot_optional.isChecked()),
            hot_dice_triggers_only_if_all_taken=bool(self.chk_hot_only_if_all_taken.isChecked()),
            allow_three_pairs=False,
            max_turns_per_game=self.ruleset.max_turns_per_game,
            max_rolls_per_turn=self.ruleset.max_rolls_per_turn,
        )
        rs.validate()

        name = self.ed_strategy_name.text().strip() or "Strategy"
        low = {1: int(self.sp_low1.value()), 2: int(self.sp_low2.value())}
        ng = [
            (int(self.sp_near1_pts.value()), int(self.sp_near1_thr.value())),
            (int(self.sp_near2_pts.value()), int(self.sp_near2_thr.value())),
        ]
        ladder_rows = self.ladder_model.rows()

        st = core.StrategyConfig(
            schema_version=core.SCHEMA_VERSION,
            name=name,
            bank_threshold_normal=int(self.sp_bank_normal.value()),
            bank_threshold_post_hot=int(self.sp_bank_post_hot.value()),
            low_dice_thresholds=low,
            near_goal_thresholds=ng,
            bank_threshold_off_board=self.strategy.bank_threshold_off_board,
            bank_threshold_endzone=self.strategy.bank_threshold_endzone,
            bank_threshold_post_hot_off_board=self.strategy.bank_threshold_post_hot_off_board,
            bank_threshold_post_hot_endzone=self.strategy.bank_threshold_post_hot_endzone,
            ladder=ladder_rows,
            enable_trace=bool(self.chk_trace.isChecked()),
        )
        st.validate()

        self.ruleset = rs
        self.strategy = st
        return rs, st

    # dialogs (same as before)
    def _save_strategy_dialog(self):
        rs, st = self.to_objects()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Strategy JSON", "strategy.json", "JSON Files (*.json)")
        if not path:
            return
        try:
            core.save_strategy(path, st)
        except Exception as e:
            show_error(self, "Save failed", str(e), traceback.format_exc())

    def _load_strategy_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Strategy JSON", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            st = core.load_strategy(path)
            self.load_from_objects(self.ruleset, st)
            self.changed.emit()
        except Exception as e:
            show_error(self, "Load failed", str(e), traceback.format_exc())

    def _save_ruleset_dialog(self):
        rs, st = self.to_objects()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Ruleset JSON", "ruleset.json", "JSON Files (*.json)")
        if not path:
            return
        try:
            core.save_ruleset(path, rs)
        except Exception as e:
            show_error(self, "Save failed", str(e), traceback.format_exc())

    def _load_ruleset_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Ruleset JSON", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            rs = core.load_ruleset(path)
            self.load_from_objects(rs, self.strategy)
            self.changed.emit()
        except Exception as e:
            show_error(self, "Load failed", str(e), traceback.format_exc())

    def _save_preset_dialog(self):
        rs, st = self.to_objects()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Combined Preset", "preset.json", "JSON Files (*.json)")
        if not path:
            return
        try:
            obj = {"ruleset": core.ruleset_to_json(rs), "strategy": core.strategy_to_json(st)}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=2, sort_keys=True)
        except Exception as e:
            show_error(self, "Save failed", str(e), traceback.format_exc())

    def _load_preset_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Combined Preset", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            rs = core.ruleset_from_json(obj["ruleset"])
            st = core.strategy_from_json(obj["strategy"])
            self.load_from_objects(rs, st)
            self.changed.emit()
        except Exception as e:
            show_error(self, "Load failed", str(e), traceback.format_exc())


# =========================
# Main Window
# =========================

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.settings = QtCore.QSettings(APP_ORG, APP_NAME)

        self.sim_thread: Optional[QtCore.QThread] = None
        self.sim_worker: Optional[SimWorker] = None
        self.cancel_token: Optional["core.CancelToken"] = None
        self.last_sim: Optional["core.SimResult"] = None

        self._build_ui()
        self._restore_settings()
        self._post_start_checks()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)

        self.split_main = QtWidgets.QSplitter(Qt.Horizontal)
        root.addWidget(self.split_main)

        self.strategy_editor = StrategyEditor()
        self.split_main.addWidget(self.strategy_editor)

        self.split_right = QtWidgets.QSplitter(Qt.Vertical)
        self.split_main.addWidget(self.split_right)

        self.tabs = QtWidgets.QTabWidget()
        self.split_right.addWidget(self.tabs)

        self.plot_progress = PlotPanel("Mean Total Score vs Turn")
        self.plot_turns = PlotPanel("Turns to Reach Target (Histogram & CDF)")
        self.plot_hot = PlotPanel("Hot Dice Events per Game")
        self.plot_farkles = PlotPanel("Farkles per Game")
        self.plot_phases = PlotPanel("Phase Turns: Entry & End-Zone (Distributions)")
        self.plot_risk = PlotPanel("Risk by Dice Remaining (Requires Detailed Log)")

        self.tabs.addTab(self.plot_progress, "Progress")
        self.tabs.addTab(self.plot_turns, "Turns-to-Win")
        self.tabs.addTab(self.plot_hot, "Hot Dice")
        self.tabs.addTab(self.plot_farkles, "Farkles")
        self.tabs.addTab(self.plot_phases, "Phases")
        self.tabs.addTab(self.plot_risk, "Risk")

        bottom = QtWidgets.QWidget()
        self.split_right.addWidget(bottom)
        bottom_layout = QtWidgets.QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(6, 6, 6, 6)
        bottom_layout.setSpacing(8)

        grp_sim = QtWidgets.QGroupBox("Simulation Controls")
        bottom_layout.addWidget(grp_sim)
        sim_form = QtWidgets.QGridLayout(grp_sim)

        self.sp_games = QtWidgets.QSpinBox()
        self.sp_games.setRange(1, 500000)
        self.sp_games.setSingleStep(100)
        self.sp_games.setValue(1000)

        self.chk_use_seed = QtWidgets.QCheckBox("Use seed")
        self.sp_seed = QtWidgets.QSpinBox()
        self.sp_seed.setRange(0, 2**31 - 1)
        self.sp_seed.setValue(123)
        self.chk_use_seed.setChecked(True)
        self.sp_seed.setEnabled(True)

        self.chk_collect_log = QtWidgets.QCheckBox("Collect detailed event log (needed for Risk tab; can be large)")
        self.chk_collect_log.setChecked(False)

        self.btn_run = QtWidgets.QPushButton("Run")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setEnabled(False)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.lbl_speed = QtWidgets.QLabel("")

        sim_form.addWidget(QtWidgets.QLabel("Games (N)"), 0, 0)
        sim_form.addWidget(self.sp_games, 0, 1)
        sim_form.addWidget(self.chk_use_seed, 0, 2)
        sim_form.addWidget(self.sp_seed, 0, 3)
        sim_form.addWidget(self.chk_collect_log, 1, 0, 1, 4)
        sim_form.addWidget(self.btn_run, 2, 0)
        sim_form.addWidget(self.btn_stop, 2, 1)
        sim_form.addWidget(self.progress, 2, 2, 1, 2)
        sim_form.addWidget(self.lbl_speed, 3, 0, 1, 4)

        grp_diag = QtWidgets.QGroupBox("Diagnostics")
        bottom_layout.addWidget(grp_diag)
        dg = QtWidgets.QGridLayout(grp_diag)
        self.btn_backend_smoke = QtWidgets.QPushButton("Run Backend Smoke Test")
        self.btn_copy_startup_diag = QtWidgets.QPushButton("Copy Startup Diagnostics")
        self.btn_copy_diag = QtWidgets.QPushButton("Copy Last-Run Diagnostic Text")
        dg.addWidget(self.btn_backend_smoke, 0, 0)
        dg.addWidget(self.btn_copy_startup_diag, 0, 1)
        dg.addWidget(self.btn_copy_diag, 0, 2)

        grp_sum = QtWidgets.QGroupBox("Results Summary")
        bottom_layout.addWidget(grp_sum)
        sum_grid = QtWidgets.QGridLayout(grp_sum)

        self.lbl_summary = QtWidgets.QLabel("No results yet.")
        self.lbl_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn_copy_summary = QtWidgets.QPushButton("Copy Summary")
        self.btn_export_csv = QtWidgets.QPushButton("Export Summary CSV…")
        self.btn_export_plot = QtWidgets.QPushButton("Export Current Plot PNG…")

        sum_grid.addWidget(self.lbl_summary, 0, 0, 1, 4)
        sum_grid.addWidget(self.btn_copy_summary, 1, 0)
        sum_grid.addWidget(self.btn_export_csv, 1, 1)
        sum_grid.addWidget(self.btn_export_plot, 1, 2)

        grp_log = QtWidgets.QGroupBox("Log / Notes")
        bottom_layout.addWidget(grp_log)
        log_v = QtWidgets.QVBoxLayout(grp_log)
        self.txt_log = QtWidgets.QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(8000)
        log_v.addWidget(self.txt_log)

        bottom_layout.addStretch(1)

        self.split_main.setStretchFactor(0, 0)
        self.split_main.setStretchFactor(1, 1)
        self.split_right.setStretchFactor(0, 3)
        self.split_right.setStretchFactor(1, 1)

        # Signals
        self.btn_run.clicked.connect(self.on_run)
        self.btn_stop.clicked.connect(self.on_stop)
        self.chk_use_seed.toggled.connect(self.sp_seed.setEnabled)
        self.btn_copy_summary.clicked.connect(self.copy_summary)
        self.btn_export_csv.clicked.connect(self.export_csv)
        self.btn_export_plot.clicked.connect(self.export_current_plot_png)
        self.btn_copy_diag.clicked.connect(self.copy_last_run_diagnostic)
        self.btn_backend_smoke.clicked.connect(self.run_backend_smoke)
        self.btn_copy_startup_diag.clicked.connect(self.copy_startup_diagnostic)

        self._build_menu()

    def _build_menu(self):
        mbar = self.menuBar()

        m_file = mbar.addMenu("&File")
        act_quit = QtGui.QAction("Quit", self)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        m_sim = mbar.addMenu("&Simulation")
        act_run = QtGui.QAction("Run", self)
        act_run.triggered.connect(self.on_run)
        act_stop = QtGui.QAction("Stop", self)
        act_stop.triggered.connect(self.on_stop)
        m_sim.addAction(act_run)
        m_sim.addAction(act_stop)

        m_help = mbar.addMenu("&Help")
        act_about = QtGui.QAction("About / Rules Summary", self)
        act_about.triggered.connect(self.about)
        act_smoke = QtGui.QAction("Run Backend Smoke Test", self)
        act_smoke.triggered.connect(self.run_backend_smoke)
        act_copy_start = QtGui.QAction("Copy Startup Diagnostics", self)
        act_copy_start.triggered.connect(self.copy_startup_diagnostic)
        m_help.addAction(act_about)
        m_help.addSeparator()
        m_help.addAction(act_smoke)
        m_help.addAction(act_copy_start)

    def about(self):
        txt = (
            "10,000 Strategy Lab\n\n"
            "Backend: tenk_core.py\n"
            "If the app ever 'closes unexpectedly', it is usually an unhandled exception.\n"
            "This build catches exceptions and logs them in the Log panel.\n\n"
            "Try: Help → Run Backend Smoke Test\n"
        )
        show_info(self, "About", txt)

    # ===== Diagnostics =====

    def startup_diagnostic_text(self) -> str:
        lines = []
        lines.append("tenk_app startup diagnostic")
        lines.append(f"python={sys.version}")
        lines.append(f"platform={platform.platform()}")
        lines.append(f"executable={sys.executable}")
        lines.append(f"cwd={os.getcwd()}")
        lines.append(f"matplotlib={matplotlib.__version__}")
        lines.append(f"PySide6={QtCore.__version__}")
        if core is None:
            lines.append(f"core_import=FAILED: {_IMPORT_ERR}")
        else:
            lines.append("core_import=OK")
            lines.append(f"core_schema_version={core.SCHEMA_VERSION}")
        return "\n".join(lines)

    def copy_startup_diagnostic(self):
        txt = self.startup_diagnostic_text()
        QtWidgets.QApplication.clipboard().setText(txt)
        self._log("Startup diagnostics copied to clipboard.")

    def run_backend_smoke(self):
        if core is None:
            show_error(self, "Backend not available", "tenk_core.py could not be imported.", str(_IMPORT_ERR))
            return
        try:
            out = core.smoke_test()
            txt = json.dumps(out, indent=2, sort_keys=True)
            QtWidgets.QApplication.clipboard().setText(txt)
            self._log("Backend smoke test OK. Result copied to clipboard.")
            show_info(self, "Smoke Test", "Smoke test OK. JSON copied to clipboard.")
        except Exception as e:
            tb = traceback.format_exc()
            self._log("Backend smoke test FAILED:\n" + tb)
            show_error(self, "Smoke test failed", str(e), tb)

    def copy_last_run_diagnostic(self):
        if not self.last_sim:
            show_info(self, "No last-run diagnostics", "Run a simulation first.")
            return
        try:
            diag = core.diagnostic_text(self.last_sim, extra={"startup": self.startup_diagnostic_text()})
            QtWidgets.QApplication.clipboard().setText(diag)
            self._log("Last-run diagnostic text copied to clipboard.")
        except Exception as e:
            show_error(self, "Diagnostic failed", str(e), traceback.format_exc())

    # =========================
    # Startup checks
    # =========================

    def _post_start_checks(self):
        if core is None:
            show_error(
                self,
                "Backend import failed",
                "Could not import tenk_core.py. Put tenk_app.py and tenk_core.py in the same folder.",
                details=str(_IMPORT_ERR) + "\n\n" + traceback.format_exc(),
            )
            self.btn_run.setEnabled(False)
            return

        try:
            rs = core.default_ruleset()
            st = core.default_strategy_config(300)
            sim = core.simulate_games(rs, st, core.SimConfig(n_games=1, seed=1, collect_turn_records=False, collect_event_log=False, progress_every=1))
            _ = core.summarize(sim)
            self._log("Startup check OK: backend callable.")
        except Exception as e:
            show_error(self, "Backend check failed", str(e), traceback.format_exc())
            self.btn_run.setEnabled(False)

    # =========================
    # Settings persistence
    # =========================

    def _restore_settings(self):
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        state = self.settings.value("split_main")
        if state is not None:
            try:
                self.split_main.restoreState(state)
            except Exception:
                pass
        state2 = self.settings.value("split_right")
        if state2 is not None:
            try:
                self.split_right.restoreState(state2)
            except Exception:
                pass

        self.sp_games.setValue(safe_int(self.settings.value("n_games", 1000), 1000))
        use_seed = bool(int(self.settings.value("use_seed", 1)))
        self.chk_use_seed.setChecked(use_seed)
        self.sp_seed.setValue(safe_int(self.settings.value("seed", 123), 123))
        self.chk_collect_log.setChecked(bool(int(self.settings.value("collect_log", 0))))

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.sim_thread is not None:
            self.on_stop()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("split_main", self.split_main.saveState())
        self.settings.setValue("split_right", self.split_right.saveState())
        self.settings.setValue("n_games", self.sp_games.value())
        self.settings.setValue("use_seed", 1 if self.chk_use_seed.isChecked() else 0)
        self.settings.setValue("seed", self.sp_seed.value())
        self.settings.setValue("collect_log", 1 if self.chk_collect_log.isChecked() else 0)
        super().closeEvent(event)

    # =========================
    # Logging
    # =========================

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.txt_log.appendPlainText(f"[{ts}] {msg}")

    # =========================
    # Simulation controls
    # =========================

    def on_run(self):
        if core is None:
            return
        if self.sim_thread is not None:
            return

        try:
            ruleset, strategy = self.strategy_editor.to_objects()
        except Exception as e:
            show_error(self, "Invalid configuration", str(e), traceback.format_exc())
            return

        n = int(self.sp_games.value())
        seed = int(self.sp_seed.value()) if self.chk_use_seed.isChecked() else None
        collect_log = bool(self.chk_collect_log.isChecked())

        if collect_log and n > 5000:
            res = QtWidgets.QMessageBox.question(
                self,
                "Large event log",
                "Detailed event log with large N can be very large in memory.\nContinue?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            )
            if res != QtWidgets.QMessageBox.Yes:
                return

        simcfg = core.SimConfig(
            n_games=n,
            seed=seed,
            collect_turn_records=True,
            collect_event_log=collect_log,
            max_games_for_event_log=(n if collect_log else 0),
            progress_every=max(1, n // 100),
            verbose=False,
        )

        self.progress.setValue(0)
        self.lbl_speed.setText("")
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._log(f"Running {n} games (seed={seed}, event_log={'ON' if collect_log else 'OFF'})…")

        self.cancel_token = core.CancelToken()
        self.sim_thread = QtCore.QThread(self)
        self.sim_worker = SimWorker(ruleset, strategy, simcfg, self.cancel_token)
        self.sim_worker.moveToThread(self.sim_thread)

        self.sim_thread.started.connect(self.sim_worker.run)
        self.sim_worker.progress.connect(self._on_progress)
        self.sim_worker.finished.connect(self._on_finished)
        self.sim_worker.failed.connect(self._on_failed)

        self.sim_worker.finished.connect(self.sim_thread.quit)
        self.sim_worker.finished.connect(self.sim_worker.deleteLater)
        self.sim_thread.finished.connect(self.sim_thread.deleteLater)

        self._t_run_start = QtCore.QElapsedTimer()
        self._t_run_start.start()
        self.sim_thread.start()

    def on_stop(self):
        if self.cancel_token:
            self.cancel_token.cancel()
            self._log("Stop requested…")

    @QtCore.Slot(int, int)
    def _on_progress(self, done: int, total: int):
        pct = int(100 * done / max(1, total))
        self.progress.setValue(pct)
        ms = self._t_run_start.elapsed()
        if ms > 0:
            s = ms / 1000.0
            rate = done / s
            self.lbl_speed.setText(f"{done}/{total}  |  {rate:,.1f} games/sec")
        else:
            self.lbl_speed.setText(f"{done}/{total}")

    @QtCore.Slot(object)
    def _on_finished(self, sim: "core.SimResult"):
        self.last_sim = sim
        self._log("Simulation finished.")
        self.progress.setValue(100)
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)

        # Clear thread refs (thread will clean up via deleteLater)
        self.sim_thread = None
        self.sim_worker = None
        self.cancel_token = None

        # Update summary + plots safely
        try:
            self._update_summary(sim)
        except Exception:
            self._log("ERROR updating summary:\n" + traceback.format_exc())
            show_error(self, "Summary update failed", "See log for details.", traceback.format_exc())

        try:
            self._update_plots(sim)
        except Exception:
            self._log("ERROR updating plots:\n" + traceback.format_exc())
            show_error(self, "Plot update failed", "See log for details.", traceback.format_exc())

    @QtCore.Slot(str, str)
    def _on_failed(self, msg: str, tb: str):
        self._log("Simulation failed.")
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(0)

        self.sim_thread = None
        self.sim_worker = None
        self.cancel_token = None

        show_error(self, "Simulation error", msg, tb)

    # =========================
    # Summary / Exports
    # =========================

    def _update_summary(self, sim: "core.SimResult"):
        summ = core.summarize(sim)
        keys = [
            "finished_games", "games_total",
            "mean_turns", "median_turns", "p10_turns", "p90_turns",
            "mean_farkles", "mean_hot_dice", "mean_rolls",
            "seed", "strategy_name"
        ]
        lines = [f"{k}: {summ[k]}" for k in keys if k in summ]
        self.lbl_summary.setText("\n".join(lines) if lines else "No summary data.")

    def copy_summary(self):
        QtWidgets.QApplication.clipboard().setText(self.lbl_summary.text())
        self._log("Summary copied to clipboard.")

    def export_csv(self):
        if not self.last_sim:
            show_info(self, "No results", "Run a simulation first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export Summary CSV", "tenk_summary.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    "game_index", "finished", "total_score", "turns", "total_rolls",
                    "farkles", "hot_dice_events", "entered_on_turn", "endzone_entered_on_turn", "elapsed_s"
                ])
                for s in self.last_sim.summaries:
                    w.writerow([
                        s.game_index, s.finished, s.total_score, s.turns, s.total_rolls,
                        s.farkles, s.hot_dice_events, s.entered_on_turn, s.endzone_entered_on_turn, f"{s.elapsed_s:.6f}"
                    ])
            self._log(f"Exported CSV: {path}")
        except Exception as e:
            show_error(self, "Export failed", str(e), traceback.format_exc())

    def export_current_plot_png(self):
        if not self.last_sim:
            show_info(self, "No results", "Run a simulation first.")
            return
        widget = self.tabs.currentWidget()
        if not isinstance(widget, PlotPanel):
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export Plot PNG", "plot.png", "PNG Files (*.png)")
        if not path:
            return
        try:
            widget.figure.savefig(path, dpi=150, bbox_inches="tight")
            self._log(f"Exported plot: {path}")
        except Exception as e:
            show_error(self, "Export failed", str(e), traceback.format_exc())

    # =========================
    # Plot computations (safe)
    # =========================

    def _update_plots(self, sim: "core.SimResult"):
        # Each plot isolated so one failure doesn't nuke all
        for fn, name in [
            (self._plot_progress, "Progress"),
            (self._plot_turns_to_win, "Turns-to-Win"),
            (self._plot_hot, "Hot Dice"),
            (self._plot_farkles, "Farkles"),
            (self._plot_phases, "Phases"),
            (self._plot_risk, "Risk"),
        ]:
            try:
                fn(sim)
            except Exception:
                self._log(f"ERROR in plot '{name}':\n{traceback.format_exc()}")
                # Put message on that tab instead of crashing
                panel = getattr(self, f"plot_{name.lower().replace('-','').replace(' ','')}", None)
                # fallback: don't rely on name mapping; just skip
                # We can directly message known panels:
                if name == "Progress":
                    self.plot_progress.set_message("Plot error. See Log.")
                elif name == "Turns-to-Win":
                    self.plot_turns.set_message("Plot error. See Log.")
                elif name == "Hot Dice":
                    self.plot_hot.set_message("Plot error. See Log.")
                elif name == "Farkles":
                    self.plot_farkles.set_message("Plot error. See Log.")
                elif name == "Phases":
                    self.plot_phases.set_message("Plot error. See Log.")
                elif name == "Risk":
                    self.plot_risk.set_message("Plot error. See Log.")

    def _plot_progress(self, sim: "core.SimResult"):
        panel = self.plot_progress
        if not sim.turns:
            panel.set_message("No per-turn records available.")
            return

        finished = {s.game_index for s in sim.summaries if s.finished}
        per_game: Dict[int, List[int]] = {}
        for tr in sim.turns:
            if tr.game_index in finished:
                per_game.setdefault(tr.game_index, []).append(tr.total_score_end)

        if not per_game:
            panel.set_message("No finished games in results.")
            return

        max_len = max(len(seq) for seq in per_game.values())
        xs = list(range(1, max_len + 1))
        means: List[float] = []
        p10: List[float] = []
        p90: List[float] = []

        for i in range(max_len):
            vals = []
            for seq in per_game.values():
                vals.append(seq[i] if i < len(seq) else seq[-1])
            vals_sorted = sorted(vals)
            means.append(sum(vals) / len(vals))
            p10.append(vals_sorted[int(0.10 * (len(vals_sorted) - 1))])
            p90.append(vals_sorted[int(0.90 * (len(vals_sorted) - 1))])

        panel.clear()
        ax = panel.ax
        ax.plot(xs, means, label="Mean")
        ax.fill_between(xs, p10, p90, alpha=0.2, label="10–90% band")
        ax.set_xlabel("Turn #")
        ax.set_ylabel("Total score")
        ax.grid(True, alpha=0.3)
        ax.legend()
        panel.canvas.draw_idle()

    def _plot_turns_to_win(self, sim: "core.SimResult"):
        panel = self.plot_turns
        fins = [s for s in sim.summaries if s.finished]
        if not fins:
            panel.set_message("No finished games.")
            return
        turns = [s.turns for s in fins]

        hist = core.series_histogram(turns, bins=30)
        cdf = core.series_cdf(turns)

        panel.figure.clear()
        ax1 = panel.figure.add_subplot(121)
        ax2 = panel.figure.add_subplot(122)

        counts = hist.get("counts", [])
        if counts:
            ax1.bar(list(range(len(counts))), counts)
            ax1.set_title("Histogram (binned)")
            ax1.set_xlabel("Bin index")
            ax1.set_ylabel("Count")
            ax1.grid(True, alpha=0.3)
            ax1.text(0.02, 0.98, f"min={min(turns)} max={max(turns)}", transform=ax1.transAxes, va="top")
        else:
            ax1.text(0.5, 0.5, "No histogram data", ha="center", va="center")
            ax1.set_axis_off()

        ax2.plot(cdf.get("x", []), cdf.get("y", []))
        ax2.set_title("CDF")
        ax2.set_xlabel("Turns")
        ax2.set_ylabel("P(T ≤ x)")
        ax2.grid(True, alpha=0.3)
        panel.canvas.draw_idle()

    def _plot_hot(self, sim: "core.SimResult"):
        panel = self.plot_hot
        fins = [s for s in sim.summaries if s.finished]
        if not fins:
            panel.set_message("No finished games.")
            return
        xs = [s.hot_dice_events for s in fins]
        hist = core.series_histogram(xs, bins=25)

        panel.clear()
        ax = panel.ax
        counts = hist.get("counts", [])
        if not counts:
            panel.set_message("No data.")
            return
        ax.bar(list(range(len(counts))), counts)
        ax.set_xlabel("Bin index")
        ax.set_ylabel("Count")
        ax.set_title("Hot Dice Events per Game (Binned)")
        ax.grid(True, alpha=0.3)
        ax.text(0.02, 0.98, f"mean={sum(xs)/len(xs):.2f}", transform=ax.transAxes, va="top")
        panel.canvas.draw_idle()

    def _plot_farkles(self, sim: "core.SimResult"):
        panel = self.plot_farkles
        fins = [s for s in sim.summaries if s.finished]
        if not fins:
            panel.set_message("No finished games.")
            return
        xs = [s.farkles for s in fins]
        hist = core.series_histogram(xs, bins=25)

        panel.clear()
        ax = panel.ax
        counts = hist.get("counts", [])
        if not counts:
            panel.set_message("No data.")
            return
        ax.bar(list(range(len(counts))), counts)
        ax.set_xlabel("Bin index")
        ax.set_ylabel("Count")
        ax.set_title("Farkles per Game (Binned)")
        ax.grid(True, alpha=0.3)
        ax.text(0.02, 0.98, f"mean={sum(xs)/len(xs):.2f}", transform=ax.transAxes, va="top")
        panel.canvas.draw_idle()

    def _plot_phases(self, sim: "core.SimResult"):
        panel = self.plot_phases
        fins = [s for s in sim.summaries if s.finished]
        if not fins:
            panel.set_message("No finished games.")
            return

        entered = [s.entered_on_turn for s in fins if s.entered_on_turn is not None]
        endz = [s.endzone_entered_on_turn for s in fins if s.endzone_entered_on_turn is not None]

        panel.figure.clear()
        ax1 = panel.figure.add_subplot(121)
        ax2 = panel.figure.add_subplot(122)

        if entered:
            h1 = core.series_histogram(entered, bins=25)
            ax1.bar(list(range(len(h1["counts"]))), h1["counts"])
            ax1.set_title("Entered Board (turn index) — binned")
            ax1.grid(True, alpha=0.3)
        else:
            ax1.text(0.5, 0.5, "No entry events", ha="center", va="center")
            ax1.set_axis_off()

        if endz:
            h2 = core.series_histogram(endz, bins=25)
            ax2.bar(list(range(len(h2["counts"]))), h2["counts"])
            ax2.set_title("Entered End-Zone (turn index) — binned")
            ax2.grid(True, alpha=0.3)
        else:
            ax2.text(0.5, 0.5, "No end-zone events", ha="center", va="center")
            ax2.set_axis_off()

        panel.canvas.draw_idle()

    def _plot_risk(self, sim: "core.SimResult"):
        panel = self.plot_risk
        if not sim.event_log:
            panel.set_message("Enable 'Collect detailed event log' and rerun to populate this plot.")
            return

        attempts = {i: 0 for i in range(1, 7)}
        farkles = {i: 0 for i in range(1, 7)}

        for ev in sim.event_log:
            typ = ev.get("type")
            roll = ev.get("roll", [])
            k = len(roll) if isinstance(roll, list) else len(roll) if roll else 0
            if typ == "DECISION" and 1 <= k <= 6:
                attempts[k] += 1
            elif typ == "FARKLE" and 1 <= k <= 6:
                farkles[k] += 1

        xs = list(range(1, 7))
        ys = [(farkles[k] / attempts[k]) if attempts[k] > 0 else 0.0 for k in xs]

        panel.clear()
        ax = panel.ax
        ax.plot(xs, ys, marker="o")
        ax.set_title("Estimated Farkle Rate by Dice Remaining (from event log)")
        ax.set_xlabel("Dice remaining at roll")
        ax.set_ylabel("Farkle rate")
        ax.set_xticks(xs)
        ax.grid(True, alpha=0.3)
        panel.canvas.draw_idle()


# =========================
# Global exception handling
# =========================

def install_exception_hooks(window_ref_getter):
    """
    Make sure exceptions show up as dialogs/logs instead of silently killing the app.
    window_ref_getter: callable returning MainWindow or None.
    """

    def excepthook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        w = window_ref_getter()
        if w is not None:
            try:
                w._log("UNHANDLED EXCEPTION:\n" + msg)
            except Exception:
                pass
            show_error(w, "Unhandled exception", str(exc), msg)
        else:
            print(msg, file=sys.stderr)

    sys.excepthook = excepthook

    # Also capture Qt warnings/messages into stderr (and optionally log)
    def qt_message_handler(mode, context, message):
        w = window_ref_getter()
        m = f"QtMsg: {message}"
        if w is not None:
            try:
                w._log(m)
            except Exception:
                pass
        else:
            print(m, file=sys.stderr)

    try:
        QtCore.qInstallMessageHandler(qt_message_handler)
    except Exception:
        pass


# =========================
# Entry point
# =========================

def main() -> int:
    # Make deprecation warnings visible but not fatal
    warnings.simplefilter("default", DeprecationWarning)

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)

    w = MainWindow()
    install_exception_hooks(lambda: w)

    if w.size().width() < 800:
        w.resize(1200, 800)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
