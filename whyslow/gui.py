"""Qt GUI for whyslow: live pressure gauges, a stall timeline and plain-English explanations."""
from __future__ import annotations

import sys
import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QPlainTextEdit, QPushButton, QSplitter, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

from pathlib import Path

from . import blackbox, stutter
from .analyze import THRESHOLDS, Episode, _pressure, analyze, describe
from .cli import load_log
from .sampler import Sampler

from . import theme
GREEN, AMBER, RED, BLUE = QColor(theme.MINT), QColor(theme.AMBER), QColor(theme.MAGENTA), QColor(theme.CYAN)
KIND_NAME = {"io": "Disk I/O", "pagein": "Page-ins", "ram": "Out of RAM", "cpu": "CPU"}
CAUSE_NAME = {"io": "Disk I/O", "mem": "Memory", "cpu": "CPU"}
RECOVERING_SECONDS = 30
HISTORY = 120          # seconds shown in the sparklines
ANALYZE_WINDOW = 900   # seconds of history fed to the analyzer
ANALYZE_EVERY_MS = 3000


class SampleThread(QThread):
    """Runs the (blocking) sampler off the UI thread so nvidia-smi can never freeze the window."""
    sampled = Signal(dict)

    def run(self):
        sm = Sampler()
        while not self.isInterruptionRequested():
            t0 = time.time()
            s = sm.sample()
            if s:
                self.sampled.emit(s)
            self.msleep(max(50, int(1000 - (time.time() - t0) * 1000)))


class Sparkline(QWidget):
    def __init__(self, title: str, key: str):
        super().__init__()
        self.title, self.key = title, key
        self.values: deque[float] = deque(maxlen=HISTORY)
        self.setMinimumSize(190, 92)

    def push(self, v: float):
        self.values.append(min(100.0, max(0.0, v)))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        edge = QColor(theme.CYAN); edge.setAlpha(110)
        p.setPen(QPen(edge, 1))
        p.setBrush(QColor(theme.PANEL))
        p.drawRoundedRect(r, 10, 10)

        thr = THRESHOLDS[self.key]
        cur = self.values[-1] if self.values else 0.0
        color = RED if cur >= thr else AMBER if cur >= thr * 0.5 else GREEN

        p.setPen(QColor(theme.CYAN))
        f = QFont("JetBrains Mono"); f.setStyleHint(QFont.Monospace); f.setPointSizeF(self.font().pointSizeF() * 0.85)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        p.setFont(f)
        p.drawText(QRectF(r.left() + 10, r.top() + 6, r.width() - 20, 18), Qt.AlignLeft, self.title)
        f.setPointSizeF(f.pointSizeF() * 1.9); f.setBold(True)
        p.setFont(f); p.setPen(color)
        p.drawText(QRectF(r.left() + 10, r.top() + 6, r.width() - 20, 30), Qt.AlignRight, f"{cur:.0f}%")

        plot = QRectF(r.left() + 8, r.top() + 40, r.width() - 16, r.height() - 48)
        # threshold line
        y_thr = plot.bottom() - plot.height() * thr / 100
        dash = QColor(theme.MAGENTA); dash.setAlpha(120)
        pen = QPen(dash, 1, Qt.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(plot.left(), y_thr), QPointF(plot.right(), y_thr))
        if len(self.values) < 2:
            return
        step = plot.width() / (HISTORY - 1)
        x0 = plot.right() - step * (len(self.values) - 1)
        pts = [QPointF(x0 + i * step, plot.bottom() - plot.height() * v / 100)
               for i, v in enumerate(self.values)]
        fill = QPolygonF(pts + [QPointF(pts[-1].x(), plot.bottom()), QPointF(pts[0].x(), plot.bottom())])
        c = QColor(color); c.setAlpha(60)
        p.setPen(Qt.NoPen); p.setBrush(QBrush(c)); p.drawPolygon(fill)
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        glow = QColor(color); glow.setAlpha(55)
        p.setBrush(Qt.NoBrush); p.setPen(QPen(glow, 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)); p.drawPath(path)
        p.setPen(QPen(color, 1.8)); p.drawPath(path)


def _make_icon(color: QColor) -> QIcon:
    pm = QPixmap(64, 64); pm.fill(Qt.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(color); p.setPen(Qt.NoPen); p.drawEllipse(6, 6, 52, 52)
    p.setPen(QPen(Qt.white, 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline(QPolygonF([QPointF(14, 36), QPointF(24, 36), QPointF(30, 20), QPointF(38, 48),
                              QPointF(44, 32), QPointF(50, 32)]))
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, seed: list[dict] | None = None, state_dir=None):
        super().__init__()
        self.state_dir = state_dir
        self.setWindowTitle("WhySlow")
        self.resize(900, 620)
        self.samples: deque[dict] = deque(maxlen=ANALYZE_WINDOW)
        self.episodes: list[Episode] = []
        self._notified: set[float] = set()
        self._ep_key: tuple = ()
        self.crashes: list[dict] = []
        self._crash_key: tuple | None = None
        self._crash_notified: set[str] = set()
        self.rows: list[tuple[str, object]] = []

        root = QWidget(); self.setCentralWidget(root)
        lay = QVBoxLayout(root); lay.setSpacing(12); lay.setContentsMargins(16, 12, 16, 12)

        head = QHBoxLayout()
        title = QLabel("WHYSLOW"); title.setObjectName("neon-title")
        glow = QGraphicsDropShadowEffect(title); glow.setColor(QColor(theme.CYAN)); glow.setBlurRadius(22); glow.setOffset(0, 0)
        title.setGraphicsEffect(glow)
        sub = QLabel("// WHY DID MY PC JUST STUTTER?"); sub.setObjectName("neon-sub")
        head.addWidget(title); head.addSpacing(14); head.addWidget(sub, 1, Qt.AlignBottom)
        lay.addLayout(head)

        self.banner = QLabel(); self.banner.setWordWrap(True)
        self.banner.setMargin(12); self.banner.setFrameShape(QFrame.StyledPanel)
        bf = self.banner.font(); bf.setPointSizeF(bf.pointSizeF() * 1.25); bf.setBold(True)
        self.banner.setFont(bf)
        lay.addWidget(self.banner)

        row = QHBoxLayout(); row.setSpacing(12)
        self.gauges = {"cpu": Sparkline("CPU contention", "cpu"),
                       "mem": Sparkline("Memory pressure", "mem"),
                       "io": Sparkline("Disk I/O stall", "io")}
        for g in self.gauges.values():
            row.addWidget(g)
        lay.addLayout(row)

        split = QSplitter(Qt.Horizontal)
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self._section("CRASHES + SLOWDOWNS"))
        self.list = QListWidget(); self.list.currentRowChanged.connect(self._show_episode)
        # clicking the already-selected row emits no currentRowChanged, so also listen for clicks
        self.list.itemClicked.connect(lambda it: self._show_episode(self.list.row(it)))
        ll.addWidget(self.list)
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self._section("WHAT HAPPENED"))
        self.detail = QPlainTextEdit(readOnly=True)
        mono = QFont(self.font()); mono.setPointSizeF(mono.pointSizeF() * 1.05)
        self.detail.setFont(mono)
        rl.addWidget(self.detail)
        split.addWidget(left); split.addWidget(right); split.setSizes([300, 600])
        lay.addWidget(split, 1)

        self.status = QLabel(); lay.addWidget(self.status)

        tools = self.menuBar().addMenu("&Tools")
        a1 = QAction("Analyze game stutter (MangoHud log)…", self); a1.triggered.connect(self._pick_game_log)
        a2 = QAction("How to record frame times…", self); a2.triggered.connect(lambda: self.detail.setPlainText(stutter.HOW_TO))
        a3 = QAction("Check hardware for slowness gotchas", self); a3.triggered.connect(self._show_hardware)
        a4 = QAction("What is draining my battery?", self); a4.triggered.connect(self._show_battery)
        a6 = QAction("Create shareable report…", self); a6.triggered.connect(self._share_report)
        tools.addAction(a6)
        a5 = QAction("Why was my boot slow?", self); a5.triggered.connect(self._show_boot)
        tools.addAction(a5); tools.addAction(a4); tools.addAction(a3); tools.addAction(a1); tools.addAction(a2)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(_make_icon(GREEN), self)
            menu = QMenu()
            show = QAction("Show WhySlow", self); show.triggered.connect(self._raise)
            quit_ = QAction("Quit", self); quit_.triggered.connect(QApplication.quit)
            menu.addAction(show); menu.addSeparator(); menu.addAction(quit_)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda r: self._raise() if r == QSystemTrayIcon.Trigger else None)
            self.tray.show()

        for s in seed or []:
            self.on_sample(s, notify=False)
        self._refresh_banner()
        self.timer = QTimer(self, interval=ANALYZE_EVERY_MS, timeout=self._analyze)
        self.timer.start()
        self._analyze()

    # ---- data in -----------------------------------------------------
    def on_sample(self, s: dict, notify: bool = True):
        self.samples.append(s)
        p = _pressure(s)
        for k, g in self.gauges.items():
            g.push(p[k])
        gb = s["mem"].get("MemAvailable", 0) / 1048576
        temp = f" · CPU {s['temp']:.0f}°C" if s.get("temp") else ""
        self.status.setText(f"CPU {s['cpu_util']:.0f}% busy · {gb:.1f} GB RAM available · "
                            f"load {s['load']:.1f}{temp}   —   sampling live")

    # ---- analysis ----------------------------------------------------
    def _refresh_crashes(self):
        """Pick up crash reports written by the daemon; notify once per new report."""
        incs = [i for i in blackbox.list_incidents(self.state_dir) if i["scope"] == "system"]
        key = tuple((i["path"], i["seen"]) for i in incs)
        if key != self._crash_key:
            self._crash_key, self.crashes = key, incs
            self._rebuild_list()
            self._refresh_banner()
        for i in incs:
            if not i["seen"] and i["path"] not in self._crash_notified:
                self._crash_notified.add(i["path"])
                if self.tray:
                    when = time.strftime("%H:%M", time.localtime(i["last_t"]))
                    self.tray.showMessage(f"Your system ended unexpectedly at {when}",
                                          i["verdict"]["headline"][:160], QSystemTrayIcon.Critical, 15000)

    def _rebuild_list(self):
        self.rows = [("crash", c) for c in self.crashes] + [("ep", e) for e in self.episodes]
        sel = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for kind, obj in self.rows:
            if kind == "crash":
                when = time.strftime("%a %H:%M", time.localtime(obj["last_t"]))
                mark = "● " if not obj["seen"] else ""
                it = QListWidgetItem(f"{mark}⚠ {when}  ·  Unexpected shutdown  ·  {obj['verdict']['category']}")
                it.setForeground(RED)
            else:
                when = time.strftime("%H:%M:%S", time.localtime(obj.start))
                also = "".join(f" + {CAUSE_NAME[c]}" for c, _ in obj.secondary)
                it = QListWidgetItem(f"{when}  ·  {KIND_NAME.get(obj.kind, obj.cause)}{also}  ·  {obj.duration:.0f}s")
                it.setForeground(RED if obj.severity > 20 else AMBER)
            self.list.addItem(it)
        if self.rows:
            # Select with signals still blocked: only a *user* click may mark a crash as seen.
            self.list.setCurrentRow(min(max(sel, 0), len(self.rows) - 1))
        self.list.blockSignals(False)
        if self.rows:
            self._show_episode(self.list.currentRow(), mark=False)
        else:
            self.detail.setPlainText("Nothing to explain — no crashes or slowdowns in the recent history.")

    def _analyze(self):
        self._refresh_crashes()
        if len(self.samples) < 3:
            return
        self.episodes = analyze(list(self.samples))
        key = tuple((e.start, round(e.end)) for e in self.episodes)
        if key != self._ep_key:
            self._ep_key = key
            self._rebuild_list()
        self._refresh_banner()
        self._maybe_notify()

    @staticmethod
    def _section(text: str) -> QLabel:
        l = QLabel(text); l.setObjectName("section")
        return l

    def _live_episode(self, within: float = 4) -> Episode | None:
        now = self.samples[-1]["t"] if self.samples else time.time()
        return next((e for e in self.episodes if now - e.end <= within), None)

    def _refresh_banner(self):
        live = self._live_episode()
        if live:
            text, color = f"⚠  Slowing down right now — {live.headline}", RED
        elif (unseen := [c for c in self.crashes if not c["seen"]]):
            when = time.strftime("%a %H:%M", time.localtime(unseen[0]["last_t"]))
            text, color = f"⚠  Your system ended unexpectedly at {when} — see the report below", RED
        elif (rec := self._live_episode(RECOVERING_SECONDS)):
            ago = self.samples[-1]["t"] - rec.end
            text, color = f"…  Recovering — the last slowdown ended {self._ago(ago)} ago", AMBER
        elif self.episodes:
            ago = (self.samples[-1]["t"] - max(e.end for e in self.episodes)) if self.samples else 0
            text, color = f"✓  Running smoothly now (last slowdown {self._ago(ago)} ago)", GREEN
        else:
            text, color = "✓  Running smoothly — no slowdowns detected", GREEN
        c = QColor(color); c.setAlpha(40)
        self.banner.setText(text)
        self.banner.setStyleSheet(f"QLabel {{ background: rgba({c.red()},{c.green()},{c.blue()},{c.alpha()});"
                                  f" border: 1px solid {color.name()}; border-radius: 8px; color: {color.name()}; }}")
        glow = self.banner.graphicsEffect()
        if glow is None:
            glow = QGraphicsDropShadowEffect(self.banner); glow.setBlurRadius(18); glow.setOffset(0, 0)
            self.banner.setGraphicsEffect(glow)
        glow.setColor(color)
        if self.tray:
            self.tray.setIcon(_make_icon(color))
            self.tray.setToolTip(text)

    @staticmethod
    def _ago(sec: float) -> str:
        return f"{sec:.0f}s" if sec < 90 else f"{sec / 60:.0f}m" if sec < 5400 else f"{sec / 3600:.1f}h"

    def _maybe_notify(self):
        live = self._live_episode()
        if live and self.tray and live.start not in self._notified and live.duration >= 3 and live.severity >= 5:
            self._notified.add(live.start)
            top = f" Likely: {live.culprits[0].app}." if live.culprits else ""
            self.tray.showMessage("Your system is slowing down", live.headline.split(":")[0] + "." + top,
                                  QSystemTrayIcon.Warning, 8000)

    def _show_episode(self, row: int, mark: bool = True):
        if not (0 <= row < len(self.rows)):
            return
        kind, e = self.rows[row]
        if kind == "crash":
            self.detail.setPlainText("\n".join(blackbox.describe_incident(e)))
            if mark and not e["seen"]:
                blackbox.mark_seen(e["path"])
                QTimer.singleShot(0, self._refresh_crashes)   # reload outside this signal handler
            return
        start = time.strftime("%H:%M:%S", time.localtime(e.start))
        head = f"{start}  ·  {e.duration:.0f}s long  ·  peak stall {e.peak:.0f}%\n\n"
        self.detail.setPlainText(head + "\n".join(describe(e)))

    # ---- game stutter ------------------------------------------------
    def _pick_game_log(self):
        from PySide6.QtWidgets import QFileDialog
        logs = stutter.find_logs()
        start = str(logs[0].parent) if logs else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose a MangoHud frame-time log", start, "MangoHud logs (*.csv)")
        if path:
            self.show_stutter_report(path)

    def _share_report(self):
        from . import share
        dlg = QDialog(self); dlg.setWindowTitle("Shareable report"); dlg.resize(780, 620)
        lay = QVBoxLayout(dlg)
        note = QLabel("Preview. Nothing is uploaded: copy it or save it, then paste it where you want help. "
                      "Read it first; personal details are removed automatically but you are the final check.")
        note.setWordWrap(True); lay.addWidget(note)
        strict = QCheckBox("Also hide names of programs that are not well-known"); lay.addWidget(strict)
        box = QPlainTextEdit(readOnly=True); lay.addWidget(box, 1)
        row = QHBoxLayout(); lay.addLayout(row)
        copy, save, close = QPushButton("Copy to clipboard"), QPushButton("Save as…"), QPushButton("Close")
        for b in (copy, save): row.addWidget(b)
        row.addStretch(1); row.addWidget(close)

        def refresh():
            try:
                box.setPlainText(share.build(strict=strict.isChecked(), samples=list(self.samples) or None))
                copy.setEnabled(True); save.setEnabled(True)
            except share.LeakError as e:
                box.setPlainText(str(e)); copy.setEnabled(False); save.setEnabled(False)
        strict.toggled.connect(lambda _: refresh())
        copy.clicked.connect(lambda: QApplication.clipboard().setText(box.toPlainText()))
        def do_save():
            path, _ = QFileDialog.getSaveFileName(dlg, "Save report", "whyslow-report.md", "Markdown (*.md);;Text (*.txt)")
            if path:
                Path(path).write_text(box.toPlainText() + "\n")
        save.clicked.connect(do_save); close.clicked.connect(dlg.accept)
        refresh(); dlg.exec()

    def _show_boot(self):
        from .cli import boot_report
        self.detail.setPlainText(boot_report())

    def _show_battery(self):
        from .cli import battery_report
        self.detail.setPlainText(battery_report())

    def _show_hardware(self):
        from . import hwcheck
        self.detail.setPlainText(hwcheck.render(*hwcheck.run_checks()))

    def show_stutter_report(self, path: str, samples: list[dict] | None = None):
        """Explain a MangoHud log using whyslow's recorded history for that session."""
        try:
            fr = stutter.parse(path)
        except (OSError, stutter.LogError) as e:
            self.detail.setPlainText(f"Cannot read that log:\n{e}")
            return
        if samples is None:
            samples = [s for s in load_log(int(time.time() - fr.t[0]) + 60) if fr.t[0] - 10 <= s["t"] <= fr.t[-1] + 10]
        note = ""
        if not samples:
            note = ("Note: whyslow has no recorded system data for this session, so causes cannot be identified.\n"
                    "(Keep the background daemon running while you play; it stores about two hours.)\n\n")
        elif samples[0]["t"] > fr.t[0] + 30:
            note = f"Note: whyslow's recording starts {samples[0]['t'] - fr.t[0]:.0f}s into this session.\n\n"
        self.list.blockSignals(True); self.list.clearSelection(); self.list.setCurrentRow(-1); self.list.blockSignals(False)
        self.detail.setPlainText(note + stutter.report(fr, samples))

    # ---- window behaviour -------------------------------------------
    def _raise(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def closeEvent(self, ev):
        if self.tray and self.tray.isVisible():
            self.hide(); ev.ignore()   # keep watching from the tray
        else:
            ev.accept()


SERVER_NAME = "whyslow-gui"


def _already_running() -> bool:
    """If another instance is up, ask it to raise its window and report True."""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(b"raise"); sock.flush(); sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def run(minimized: bool = False) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("WhySlow")
    app.setDesktopFileName("io.github.metalmike17.WhySlow")
    theme.apply(app)
    app.setQuitOnLastWindowClosed(False)
    if _already_running():
        return 0
    QLocalServer.removeServer(SERVER_NAME)   # clear a stale socket from a crashed run
    server = QLocalServer(); server.listen(SERVER_NAME)
    cutoff = time.time() - ANALYZE_WINDOW
    win = MainWindow(seed=[s for s in load_log(ANALYZE_WINDOW) if s["t"] >= cutoff])
    thread = SampleThread()
    thread.sampled.connect(win.on_sample)
    thread.start()
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win._raise()))
    if not (minimized and win.tray):
        win.show()
    app.aboutToQuit.connect(lambda: (thread.requestInterruption(), thread.wait(3000)))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
