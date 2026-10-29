"""Vocalworks desktop app: pick a song, record or upload your vocal, download your cover."""
import shutil
import sys

import soundfile as sf
import traceback
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog, QInputDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSlider, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from app import assistant
from app import lyrics
from app import profiles
from app import pipeline
from app import recorder as rec

STATUS_TEXT = {"imported": "not prepared", "analyzed": "ready for vocals", "in progress": "in progress",
               "awaiting feedback": "cover ready", "approved": "approved", "on hold": "on hold"}


def dark_mode():
    """Whether Windows (and so the app) is using the dark theme."""
    return QApplication.palette().window().color().lightness() < 128


# text colors that read well on the window background: (light theme, dark theme)
COLORS = {"faint": ("#8a8a8a", "#8c8c8c"), "current": ("#1565c0", "#64b5f6"), "next": ("#404040", "#d6d6d6"),
          "status": ("#606060", "#a6a6a6"), "notice": ("#a06000", "#f0b050")}


def color(name):
    return COLORS[name][dark_mode()]


class Worker(QObject):
    """Runs fn(log=...) on a thread; lines go to `log`, the result to `done`, errors to `failed`."""
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(log=self.log.emit))
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.failed.emit(str(e) if isinstance(e, pipeline.PipelineError) else traceback.format_exc())


class KaraokeView(QWidget):
    """Previous line, the current line (large), and the next two, following the song's time."""

    def __init__(self):
        super().__init__()
        self.lines = []
        self.current = None
        v = QVBoxLayout(self)
        v.addStretch(1)
        self.rows = []
        for size, bold in ((12, False), (22, True), (14, False), (12, False)):
            lab = QLabel("")
            lab.setAlignment(Qt.AlignCenter)
            lab.setWordWrap(True)
            font = QFont(lab.font())
            font.setPointSize(size)
            font.setBold(bold)
            lab.setFont(font)
            v.addWidget(lab)
            self.rows.append(lab)
        v.addStretch(1)
        self.apply_theme()
        self.set_lines([])

    def apply_theme(self):
        # on the window's own background: a fixed light one showed as white bars behind each line in
        # dark mode (Windows 11 style paints a stylesheet background only behind the labels)
        for lab, name in zip(self.rows, ("faint", "current", "next", "faint")):
            lab.setStyleSheet(f"color: {color(name)}; background: transparent;")

    def set_lines(self, lines):
        self.lines = [(t, w) for t, w in lines if w.strip()]
        self.current = None
        self.show_time(-1.0)

    def show_time(self, t):
        if not self.lines:
            texts = ["", "No lyrics yet", "Find them online, load a .lrc file, or paste them below.", ""]
        else:
            i = lyrics.line_at(self.lines, t)
            if i == self.current and i >= 0:  # before the first line the countdown still changes
                return
            self.current = i
            pick = lambda k: self.lines[k][1] if 0 <= k < len(self.lines) else ""  # noqa: E731
            if i < 0:
                left = self.lines[0][0] - max(t, 0)
                texts = ["", f"(starts in {left:.0f} s)" if t >= 0 else "", pick(0), pick(1)]
            else:
                texts = [pick(i - 1), pick(i), pick(i + 1), pick(i + 2)]
        for lab, text in zip(self.rows, texts):
            lab.setText(text)


class AssistantDialog(QDialog):
    """Connect the user's own AI assistant: install its official app, sign in, choose it."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Connect AI assistant")
        self.setMinimumWidth(620)
        v = QVBoxLayout(self)
        intro = QLabel(
            "Optional. Connect an AI assistant you already use and it will study each new song with the "
            "Vocalworks guide and plan your cover; the app then builds the cover from that plan, checked "
            "against the same rules. It runs through the assistant's official app on this PC, signed in "
            "with your own account, and Vocalworks only starts it inside its own folder. The assistant "
            "itself works online. Without one, everything is decided automatically from the measurements.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        grid = QGridLayout()
        self.rows = {}
        for r, (key, a) in enumerate(assistant.ASSISTANTS.items()):
            name = QLabel(f"<b>{a['name']}</b><br><span style='color:#666'>{a['account']}</span>")
            state = QLabel("")
            inst = QPushButton("Install")
            inst.clicked.connect(lambda _=False, k=key: self.install(k))
            login = QPushButton("Sign in")
            login.clicked.connect(lambda _=False, k=key: self.sign_in(k))
            use = QPushButton("Use this one")
            use.clicked.connect(lambda _=False, k=key: self.use(k))
            for c, w in enumerate((name, state, inst, login, use)):
                grid.addWidget(w, r, c)
            self.rows[key] = (state, inst, login, use)
        v.addLayout(grid)
        self.current = QLabel("")
        v.addWidget(self.current)
        bar = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        self.off = QPushButton("Disconnect")
        self.off.clicked.connect(self.disconnect)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bar.addWidget(refresh)
        bar.addWidget(self.off)
        bar.addStretch(1)
        bar.addWidget(close)
        v.addLayout(bar)
        self.refresh()

    def refresh(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            on = assistant.connected()
            for key, (state, inst, login, use) in self.rows.items():
                st = assistant.status(key)
                state.setText(("connected" if key == on and st == "ready" else st).capitalize())
                inst.setVisible(st == "not installed")
                login.setVisible(st != "not installed")
                login.setText("Sign in again" if st == "ready" else "Sign in")
                use.setEnabled(st == "ready" and key != on)
            self.current.setText(f"Connected: {assistant.ASSISTANTS[on]['name']}" if on else "No assistant connected.")
            self.off.setEnabled(bool(on))
        finally:
            QApplication.restoreOverrideCursor()

    def install(self, key):
        if not assistant.node_available():
            QMessageBox.information(self, "Node.js needed",
                                    "The assistants' apps install with Node.js. Get it from https://nodejs.org "
                                    "(the LTS version), then click Install again.")
            return
        assistant.install(key)
        QMessageBox.information(self, "Installing", "A window opened to install it. When it finishes, close "
                                "that window and click Refresh.")

    def sign_in(self, key):
        assistant.sign_in(key)
        QMessageBox.information(self, "Sign in", assistant.ASSISTANTS[key]["login_hint"]
                                + "\n\nThen click Refresh here.")

    def use(self, key):
        assistant.connect(key)
        self.refresh()

    def disconnect(self):
        assistant.disconnect()
        self.refresh()


class VolumeSlider(QWidget):
    """A volume slider for a play button: starts at 100 each time playback starts, changes it live.
    `changed` carries the gain to apply (100 -> 1.0), on a curve that sounds even across the slider."""
    changed = Signal(float)

    def __init__(self):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setMinimumWidth(90)
        self.slider.setToolTip("Volume")
        self.value = QLabel()
        self.value.setMinimumWidth(34)
        h.addWidget(QLabel("Volume"))
        h.addWidget(self.slider, 1)
        h.addWidget(self.value)
        self.slider.valueChanged.connect(self.moved)
        self.reset()

    def reset(self):
        self.slider.setValue(100)
        self.moved(100)

    def moved(self, v):
        self.value.setText(f"{v}%")
        self.changed.emit(self.gain())

    def gain(self):
        return (self.slider.value() / 100) ** 2


class NewProfileDialog(QDialog):
    """Name a new profile (a singer on one setup) and pick its devices."""

    def __init__(self, parent, mics, outs):
        super().__init__(parent)
        self.setWindowTitle("New vocal profile")
        self.setMinimumWidth(520)
        f = QFormLayout(self)
        intro = QLabel("A profile is one singer on one setup: the mic, the room and how far you sing from the "
                       "mic. Singing somewhere else or into another mic? Make a separate profile for it, and "
                       "choose \"Same singer as\" so your voice carries over.")
        intro.setWordWrap(True)
        f.addRow(intro)
        self.singer = QLineEdit()
        self.singer.setPlaceholderText("Your name")
        self.setup = QLineEdit()
        self.setup.setPlaceholderText("e.g. Home, headset mic")
        self.same = QComboBox()
        self.same.addItem("No, a new singer", None)
        for pid, name, _ in profiles.list_profiles():
            self.same.addItem(name, pid)
        self.mic, self.out = QComboBox(), QComboBox()
        for box, items in ((self.mic, mics), (self.out, outs)):
            for i in range(items.count()):
                box.addItem(items.itemText(i), items.itemData(i))
            box.setCurrentIndex(items.currentIndex())
        f.addRow("Singer", self.singer)
        f.addRow("Setup", self.setup)
        f.addRow("Same singer as", self.same)
        f.addRow("Microphone", self.mic)
        f.addRow("Headphones", self.out)
        bar = QHBoxLayout()
        ok, cancel = QPushButton("Create and record"), QPushButton("Cancel")
        ok.clicked.connect(self.check)
        cancel.clicked.connect(self.reject)
        bar.addStretch(1)
        bar.addWidget(cancel)
        bar.addWidget(ok)
        f.addRow(bar)

    def check(self):
        if not self.singer.text().strip() or not self.setup.text().strip():
            QMessageBox.information(self, "Names needed", "Give the singer and the setup a name.")
            return
        self.accept()


class CalibrationDialog(QDialog):
    """The guided recording: six short recordings, each measured straight away."""

    def __init__(self, parent, profile, mic, out):
        super().__init__(parent)
        self.profile, self.mic, self.out = profile, mic, out
        self.recorder = self.schedule = None
        self.latency = 0.0
        self.setWindowTitle(f"Record the profile: {profiles.display_name(profile)}")
        self.setMinimumSize(760, 460)
        h = QHBoxLayout(self)
        self.steps = QListWidget()
        self.steps.setMaximumWidth(210)
        self.steps.currentRowChanged.connect(self.show_step)
        h.addWidget(self.steps)
        right = QVBoxLayout()
        h.addLayout(right, 1)
        self.title = QLabel()
        self.title.setFont(QFont(self.title.font().family(), 13, QFont.Bold))
        self.say = QLabel()
        self.say.setWordWrap(True)
        self.big = QLabel()
        self.big.setAlignment(Qt.AlignCenter)
        self.big.setWordWrap(True)
        big_font = QFont(self.big.font())
        big_font.setPointSize(18)
        self.big.setFont(big_font)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        tip = QLabel("Wear headphones, and keep the mic and your distance from it the same as when you sing "
                     "songs: the profile describes exactly that setup.")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {color('status')};")
        self.rec = QPushButton("Record")
        self.rec.setMinimumHeight(34)
        self.rec.clicked.connect(self.toggle)
        self.next = QPushButton("Next step  ->")
        self.next.clicked.connect(self.go_next)
        for w in (self.title, self.say, self.big, self.meter, self.result_label):
            right.addWidget(w)
        right.addStretch(1)
        right.addWidget(tip)
        bar = QHBoxLayout()
        bar.addWidget(self.rec)
        bar.addStretch(1)
        bar.addWidget(self.next)
        right.addLayout(bar)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.refresh_list()
        first = next((i for i, st in enumerate(profiles.STEPS) if st["id"] not in profile["calibration"]), 0)
        self.steps.setCurrentRow(first)
        self.show_step(first)

    def refresh_list(self):
        row = self.steps.currentRow()
        self.steps.blockSignals(True)
        self.steps.clear()
        for st in profiles.STEPS:
            cal = self.profile["calibration"].get(st["id"])
            mark = "  (carried over)" if cal and cal.get("carried_from") else ""
            self.steps.addItem(("\u2713 " if cal else "    ") + st["title"] + mark)
        if row >= 0:
            self.steps.setCurrentRow(row)
        self.steps.blockSignals(False)

    def current(self):
        return profiles.STEPS[max(self.steps.currentRow(), 0)]

    def show_step(self, i):
        if i < 0 or self.recorder is not None:
            return
        st = profiles.STEPS[i]
        self.title.setText(f"Step {i + 1} of {len(profiles.STEPS)}: {st['title']}")
        self.say.setText(st["say"])
        self.big.setText(st["big"])
        done = st["id"] in self.profile["calibration"]
        self.rec.setText("Record again" if done else "Record")
        self.result_label.setText("Done. You can record it again, or go on." if done else "")
        self.next.setText("Finish" if i == len(profiles.STEPS) - 1 else "Next step  ->")
        self.next.setEnabled(done)

    def toggle(self):
        if self.recorder is not None:  # cancel: nothing is kept
            self.timer.stop()
            self.recorder.stream.stop()
            self.recorder.stream.close()
            self.recorder = None
            self.meter.setValue(0)
            self.rec.setText("Record")
            self.result_label.setText("Cancelled.")
            self.steps.setEnabled(True)
            return
        st = self.current()
        play, self.schedule = profiles.backing(st["id"], self.profile)
        path = profiles.clip_path(self.profile, st["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        self.recorder = rec.Recorder(play, path, self.mic, self.out, preroll=profiles.PREROLL, song_volume=1.0,
                                     sr=profiles.SR)
        self.recorder.start()
        self.rec.setText("Cancel")
        self.next.setEnabled(False)
        self.steps.setEnabled(False)
        self.result_label.setText("Recording...")
        self.timer.start(50)

    def tick(self):
        r = self.recorder
        self.meter.setValue(min(100, int(r.level * 300)))
        if r.done.is_set():
            self.timer.stop()
            self.latency = float(sum(r.stream.latency)) if r.stream is not None else 0.0
            r.stop()
            self.recorder = None
            self.meter.setValue(0)
            self.steps.setEnabled(True)
            self.measure()

    def measure(self):
        step = self.current()["id"]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            _, message, ok = profiles.measure_step(self.profile, step, self.schedule, self.latency)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            self.profile["calibration"].pop(step, None)
            profiles.save(self.profile)
        self.result_label.setText(("\u2713 " if ok else "Please record this again: ") + message)
        self.rec.setText("Record again")
        self.next.setEnabled(ok)
        self.refresh_list()

    def go_next(self):
        i = self.steps.currentRow()
        missing = [st for st in profiles.STEPS if st["id"] not in self.profile["calibration"]]
        if i < len(profiles.STEPS) - 1:
            self.steps.setCurrentRow(i + 1)
        elif missing:
            self.steps.setCurrentRow(profiles.STEPS.index(missing[0]))
        else:
            self.accept()

    def reject(self):
        if self.recorder is not None:
            self.toggle()
        super().reject()


class ProfileDialog(QDialog):
    """Vocal profiles: pick the active one, see what it knows, make new ones."""

    def __init__(self, parent):
        super().__init__(parent)
        self.main = parent
        self.setWindowTitle("Singer: vocal profiles")
        self.setMinimumSize(780, 420)
        v = QVBoxLayout(self)
        intro = QLabel("A vocal profile describes one singer on one setup. Record it once with the guided "
                       "recording; after that, every take you sing with it teaches it more. (For now profiles "
                       "are learned and shown; they don't change how covers are made yet.)")
        intro.setWordWrap(True)
        v.addWidget(intro)
        h = QHBoxLayout()
        v.addLayout(h, 1)
        self.list = QListWidget()
        self.list.setMaximumWidth(270)
        self.list.currentItemChanged.connect(self.show_card)
        h.addWidget(self.list)
        self.card = QLabel()
        self.card.setWordWrap(True)
        self.card.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.card.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box = QGroupBox("Your voice")
        bv = QVBoxLayout(box)
        bv.addWidget(self.card, 1)
        h.addWidget(box, 1)
        bar = QHBoxLayout()
        self.new_btn = QPushButton("New profile...")
        self.new_btn.clicked.connect(self.new_profile)
        self.record_btn = QPushButton("Record the profile")
        self.record_btn.clicked.connect(self.record)
        self.use_btn = QPushButton("Sing as this profile")
        self.use_btn.clicked.connect(self.use_selected)
        self.none_btn = QPushButton("Sing without a profile")
        self.none_btn.clicked.connect(self.use_none)
        self.delete_btn = QPushButton("Delete...")
        self.delete_btn.clicked.connect(self.delete)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        for w in (self.new_btn, self.record_btn, self.use_btn, self.none_btn, self.delete_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(close)
        v.addLayout(bar)
        self.refresh()

    def selected(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def refresh(self, select=None):
        on = profiles.active()
        select = select or self.selected() or on
        self.list.blockSignals(True)
        self.list.clear()
        for pid, name, done in profiles.list_profiles():
            item = QListWidgetItem(name + ("   (singing as)" if pid == on else "")
                                   + ("" if done else "   (unfinished)"))
            item.setData(Qt.UserRole, pid)
            self.list.addItem(item)
            if pid == select:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self.none_btn.setEnabled(on is not None)
        self.show_card()

    def show_card(self, *_):
        pid = self.selected()
        p = profiles.load(pid) if pid else None
        self.card.setText(profiles.describe(p) if p else "No profiles yet. Click \"New profile...\" to record one.")
        for w in (self.record_btn, self.delete_btn):
            w.setEnabled(p is not None)
        self.record_btn.setText("Record the profile" if p and not profiles.complete(p) else "Record again...")
        self.use_btn.setEnabled(bool(p and profiles.complete(p) and pid != profiles.active()))

    def new_profile(self):
        d = NewProfileDialog(self, self.main.mic, self.main.out)
        if d.exec() != QDialog.Accepted:
            return
        p = profiles.new(d.singer.text(), d.setup.text(),
                         {"mic": d.mic.currentText(), "headphones": d.out.currentText()}, d.same.currentData())
        self.refresh(p["id"])
        self.calibrate(p, d.mic.currentData(), d.out.currentData())

    def record(self):
        p = profiles.load(self.selected())
        if p:
            self.calibrate(p, self.main.mic.currentData(), self.main.out.currentData())

    def calibrate(self, p, mic, out):
        if self.main.recorder is not None:
            QMessageBox.information(self, "Busy", "Finish the recording in step 2 first.")
            return
        CalibrationDialog(self, p, mic, out).exec()
        p = profiles.load(p["id"])
        if p and profiles.complete(p) and profiles.active() is None:
            profiles.set_active(p["id"])  # the first finished profile becomes the one you sing as
        self.refresh(p["id"] if p else None)

    def use_selected(self):
        profiles.set_active(self.selected())
        self.refresh()

    def use_none(self):
        profiles.set_active(None)
        self.refresh()

    def delete(self):
        p = profiles.load(self.selected())
        if p and QMessageBox.question(self, "Delete profile", f"Move the profile {profiles.display_name(p)} and "
                                      "its recordings to the Recycle Bin?") == QMessageBox.Yes:
            if profiles.active() == p["id"]:
                profiles.set_active(None)
            profiles.delete(p["id"])
            self.refresh()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vocalworks")
        self.resize(1100, 720)
        self.folder = None
        self.thread = None
        self.recorder = None
        self.player = QMediaPlayer()
        self.playing = None  # which play button the shared player is serving: "preview" or "result"
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)

        # left: songs
        self.songs = QListWidget()
        self.songs.setWordWrap(True)
        self.songs.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.songs.setSpacing(2)
        self.songs.currentItemChanged.connect(self.select_song)
        self.singer_btn = QPushButton()
        self.singer_btn.clicked.connect(self.open_profiles)
        self.ai_btn = QPushButton()
        self.ai_btn.clicked.connect(self.connect_assistant)
        new_btn = QPushButton("+ New song")
        new_btn.clicked.connect(self.new_song)
        left = QWidget()
        left.setMinimumWidth(250)
        lv = QVBoxLayout(left)
        title = QLabel("Songs")
        title.setFont(QFont(title.font().family(), 12, QFont.Bold))
        lv.addWidget(title)
        lv.addWidget(self.songs)
        lv.addWidget(self.singer_btn)
        lv.addWidget(self.ai_btn)
        lv.addWidget(new_btn)

        # right: steps
        self.step_buttons = QButtonGroup(self)
        steps_bar = QHBoxLayout()
        for i, name in enumerate(("1  Song", "2  Vocals", "3  Result")):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setMinimumHeight(34)
            self.step_buttons.addButton(b, i)
            steps_bar.addWidget(b)
        self.step_buttons.idClicked.connect(self.show_step)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.build_step1())
        self.pages.addWidget(self.build_step2())
        self.pages.addWidget(self.build_step3())

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setVisible(False)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QFont("Consolas", 9))

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addLayout(steps_bar)
        rv.addWidget(self.pages, 3)
        rv.addWidget(self.busy)
        rv.addWidget(QLabel("Log"))
        rv.addWidget(self.log_view, 2)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([260, 840])
        self.setCentralWidget(split)
        self.apply_theme()
        self.update_singer()
        self.update_ai_button()
        self.refresh_songs()
        self.show_step(0)

    def apply_theme(self):
        """Colors that follow Windows' light or dark theme (also when it changes while the app is open)."""
        self.karaoke.apply_theme()
        self.lyrics_status.setStyleSheet(f"color: {color('status')};")
        self.notice.setStyleSheet(f"color: {color('notice')};")

    def changeEvent(self, event):
        if event.type() in (QEvent.PaletteChange, QEvent.ThemeChange) and hasattr(self, "karaoke"):
            self.apply_theme()
        super().changeEvent(event)

    # ------------------------------------------------------------ step pages

    def build_step1(self):
        page = QWidget()
        v = QVBoxLayout(page)
        box = QGroupBox("Get a new song")
        f = QFormLayout(box)
        self.source = QComboBox()
        self.source.addItems(["Local file", "YouTube", "SoundCloud"])
        self.source.currentIndexChanged.connect(self.source_changed)
        self.url = QLineEdit()
        self.url.setPlaceholderText("Paste the song's link")
        self.browse = QPushButton("Choose audio file...")
        self.browse.clicked.connect(self.pick_song_file)
        self.picked_file = QLabel("")
        self.notice = QLabel("Only download songs you have the rights to use. Downloading may be against the "
                             "platform's terms of service.")
        self.notice.setWordWrap(True)
        self.get_btn = QPushButton("Get song")
        self.get_btn.setMinimumHeight(32)
        self.get_btn.clicked.connect(self.get_song)
        f.addRow("Source", self.source)
        f.addRow("Link", self.url)
        f.addRow("", self.browse)
        f.addRow("", self.picked_file)
        f.addRow("", self.notice)
        f.addRow("", self.get_btn)
        v.addWidget(box)
        info = QGroupBox("About this song")
        iv = QVBoxLayout(info)
        self.song_info = QLabel("Pick a song on the left, or get a new one above.")
        self.song_info.setWordWrap(True)
        self.song_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Prepare: split, measure, find lyrics. "with <assistant>" also has the connected AI plan the cover.
        self.prep_btn = QPushButton("Prepare")
        self.prep_btn.clicked.connect(self.prepare_song)
        self.prep_ai_btn = QPushButton()
        self.prep_ai_btn.clicked.connect(self.prepare_song_with_assistant)
        self.song_info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        iv.addWidget(self.song_info, 1)
        row = QHBoxLayout()
        row.addWidget(self.prep_btn)
        row.addWidget(self.prep_ai_btn)
        row.addStretch(1)
        iv.addLayout(row)
        v.addWidget(info, 1)
        self.source_changed(0)
        return page

    def build_lyrics_panel(self):
        box = QGroupBox("Lyrics")
        v = QVBoxLayout(box)
        self.karaoke = KaraokeView()
        self.karaoke.setMinimumHeight(220)
        self.lyrics_status = QLabel("")
        self.lyrics_status.setWordWrap(True)
        row1 = QHBoxLayout()
        self.preview_btn = QPushButton("Play song with lyrics")
        self.preview_btn.clicked.connect(self.toggle_preview)
        earlier = QPushButton("Lyrics earlier")
        earlier.clicked.connect(lambda: self.nudge_lyrics(-0.25))
        later = QPushButton("Lyrics later")
        later.clicked.connect(lambda: self.nudge_lyrics(0.25))
        for b in (self.preview_btn, earlier, later):
            row1.addWidget(b)
        self.preview_volume = VolumeSlider()
        self.preview_volume.changed.connect(self.set_preview_volume)
        row_volume = QHBoxLayout()
        row_volume.addWidget(self.preview_volume)
        row2 = QHBoxLayout()
        find = QPushButton("Find online")
        find.clicked.connect(self.find_lyrics)
        load = QPushButton("Load .lrc / text file...")
        load.clicked.connect(self.load_lyrics_file)
        paste = QPushButton("Paste lyrics...")
        paste.clicked.connect(self.paste_lyrics)
        for b in (find, load, paste):
            row2.addWidget(b)
        v.addWidget(self.karaoke, 1)
        v.addWidget(self.lyrics_status)
        v.addLayout(row1)
        v.addLayout(row_volume)
        v.addLayout(row2)
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(
            lambda: self.karaoke.show_time(self.player.position() / 1000))
        return box

    def build_step2(self):
        page = QWidget()
        outer = QHBoxLayout(page)
        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(left, 1)
        outer.addWidget(self.build_lyrics_panel(), 1)
        box = QGroupBox("Record in the app")
        f = QFormLayout(box)
        self.mic = QComboBox()
        self.out = QComboBox()
        ins, outs = rec.devices()
        for i, name in ins:
            self.mic.addItem(name, i)
        for i, name in outs:
            self.out.addItem(name, i)
        self.rec_btn = QPushButton("Record")
        self.rec_btn.setMinimumHeight(36)
        self.rec_btn.clicked.connect(self.record_with_song)
        # the same, singing over the backing track alone (the instrumental "Prepare" split out)
        self.rec_inst_btn = QPushButton("Record (instrumental)")
        self.rec_inst_btn.setMinimumHeight(36)
        self.rec_inst_btn.clicked.connect(self.record_with_instrumental)
        # appears once the last lyric line has been sung, so the singer needn't wait out the outro
        self.finish_btn = QPushButton("Finish early (lyrics done)")
        self.finish_btn.setMinimumHeight(36)
        self.finish_btn.clicked.connect(self.finish_early)
        self.finish_btn.setVisible(False)
        self.rec_time = QLabel("")
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        tip = QLabel("Wear headphones so the music doesn't get into the mic. Recording starts one second "
                     "before the song. Keep your loudest notes out of the red: aim for the meter's upper "
                     "half, never full.")
        tip.setWordWrap(True)
        f.addRow("Microphone", self.mic)
        f.addRow("Headphones", self.out)
        f.addRow("", tip)
        f.addRow("Level", self.meter)
        row = QHBoxLayout()
        row.addWidget(self.rec_btn)
        row.addWidget(self.rec_inst_btn)
        row.addWidget(self.finish_btn)
        row.addWidget(self.rec_time, 1)
        self.rec_volume = VolumeSlider()  # the backing while recording
        self.rec_volume.changed.connect(self.set_record_volume)
        f.addRow("", row)
        f.addRow("", self.rec_volume)
        self.singing_as = QLabel()
        self.singing_as.setWordWrap(True)
        f.addRow("", self.singing_as)
        v.addWidget(box)

        up = QGroupBox("Or upload a recording")
        uv = QVBoxLayout(up)
        ul = QLabel("Start recording yourself first, then start the song, and sing along the whole way.")
        ul.setWordWrap(True)
        upload_btn = QPushButton("Upload vocal file...")
        upload_btn.clicked.connect(self.upload_take)
        uv.addWidget(ul)
        uv.addWidget(upload_btn, 0, Qt.AlignLeft)
        v.addWidget(up)

        tk = QGroupBox("Your takes")
        tv = QVBoxLayout(tk)
        self.take_list = QListWidget()
        nxt = QPushButton("Use selected take  ->")
        nxt.clicked.connect(lambda: self.show_step(2))
        tv.addWidget(self.take_list)
        tv.addWidget(nxt, 0, Qt.AlignRight)
        v.addWidget(tk, 1)
        return page

    def build_step3(self):
        page = QWidget()
        v = QVBoxLayout(page)
        box = QGroupBox("Make your cover")
        f = QFormLayout(box)
        self.take_label = QLabel("")
        auto = QLabel("Everything is matched to the original automatically: pitch correction, timing, your "
                      "vocal level and the vocal's sound and effects.")
        auto.setWordWrap(True)
        self.make_btn = QPushButton("Make my cover")
        self.make_btn.setMinimumHeight(36)
        self.make_btn.clicked.connect(self.make_cover)
        f.addRow("Take", self.take_label)
        f.addRow("", auto)
        f.addRow("", self.make_btn)
        v.addWidget(box)

        res = QGroupBox("Your cover")
        rv = QVBoxLayout(res)
        self.result = QLabel("Not made yet.")
        self.result.setWordWrap(True)
        self.result.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        row = QHBoxLayout()
        self.play_cover = QPushButton("Play cover")
        self.play_cover.clicked.connect(lambda: self.play("cover"))
        self.play_vocal = QPushButton("Play vocal only")
        self.play_vocal.clicked.connect(lambda: self.play("vocal"))
        stop = QPushButton("Stop")
        stop.clicked.connect(self.player.stop)
        for b in (self.play_cover, self.play_vocal, stop):
            row.addWidget(b)
        self.result_volume = VolumeSlider()
        self.result_volume.changed.connect(self.set_result_volume)
        row.addWidget(self.result_volume)
        row.addStretch(1)
        row2 = QHBoxLayout()
        self.save_cover = QPushButton("Download cover (WAV + MP3)...")
        self.save_cover.clicked.connect(self.download_cover)
        self.save_vocal = QPushButton("Download vocal only...")
        self.save_vocal.clicked.connect(self.download_vocal)
        self.approve_btn = QPushButton("Sounds good: keep and tidy up")
        self.approve_btn.clicked.connect(self.approve)
        row2.addWidget(self.save_cover)
        row2.addWidget(self.save_vocal)
        row2.addStretch(1)
        row2.addWidget(self.approve_btn)
        rv.addWidget(self.result, 1)
        rv.addLayout(row)
        rv.addLayout(row2)
        v.addWidget(res, 1)
        return page

    # ------------------------------------------------------------ helpers

    def log(self, line):
        self.log_view.appendPlainText(line)
        print(line, flush=True)  # also lands in vocalworks.log

    def run(self, fn, on_done, label):
        if self.thread is not None:
            QMessageBox.information(self, "Busy", "Please wait for the current job to finish.")
            return
        self.log(f"--- {label} ---")
        self.busy.setVisible(True)
        self.on_done = on_done
        self.thread = QThread()
        self.worker = Worker(fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        # Window methods with queued connections: these must run on the GUI thread. (A lambda here
        # runs on the worker thread, which then tries to wait on itself and Qt aborts the app.)
        self.worker.log.connect(self.log, Qt.QueuedConnection)
        self.worker.done.connect(self.finish, Qt.QueuedConnection)
        self.worker.failed.connect(self.fail, Qt.QueuedConnection)
        self.thread.start()

    def _end(self):
        self.thread.quit()
        self.thread.wait()
        self.thread = None
        self.worker = None
        self.busy.setVisible(False)

    def finish(self, result):
        self._end()
        on_done, self.on_done = self.on_done, None
        on_done(result)

    def fail(self, message):
        self._end()
        self.log(message)
        QMessageBox.warning(self, "Something went wrong", message[-1500:])

    def notes(self):
        return pipeline.songfile.load(self.folder) if self.folder else {}

    def refresh_songs(self, select=None):
        self.songs.blockSignals(True)
        self.songs.clear()
        for folder, title, status in pipeline.list_songs():
            item = QListWidgetItem(f"{title}\n   {STATUS_TEXT.get(status, status)}")
            item.setData(Qt.UserRole, str(folder))
            self.songs.addItem(item)
            if select and Path(select) == folder:
                self.songs.setCurrentItem(item)
        self.songs.blockSignals(False)
        if select:
            self.load_song(Path(select))

    def show_step(self, i):
        if i > 0 and not self.folder:
            QMessageBox.information(self, "Pick a song", "Choose or get a song first.")
            i = 0
        elif i > 0 and not self.prepared():
            QMessageBox.information(self, "Prepare the song", "Prepare the song first, with the buttons under "
                                    "\"About this song\".")
            i = 0
        if i == 2:
            self.prepare_step3()
        self.pages.setCurrentIndex(i)
        self.step_buttons.button(i).setChecked(True)

    # ------------------------------------------------------------ step 1

    def new_song(self):
        self.songs.clearSelection()
        self.folder = None
        self.song_info.setText("Get a new song above.")
        self.update_ai_button()
        self.show_step(0)

    def source_changed(self, i):
        local = i == 0
        self.url.setVisible(not local)
        self.browse.setVisible(local)
        self.picked_file.setVisible(local)
        self.notice.setVisible(not local)

    def pick_song_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose the song", "", "Audio (*.mp3 *.wav *.flac *.ogg *.m4a "
                                              "*.aac *.opus *.webm *.wma *.aiff *.aif)")
        if path:
            self.picked_file.setText(path)

    def get_song(self):
        if self.source.currentIndex() == 0:
            path = self.picked_file.text()
            if not path:
                return self.pick_song_file()
            fetch = lambda log: pipeline.import_file(path, log)  # noqa: E731
        else:
            url = self.url.text().strip()
            if not url:
                QMessageBox.information(self, "Link needed", "Paste the song's link first.")
                return
            fetch = lambda log: pipeline.download(url, log)  # noqa: E731

        self.run(fetch, self.song_received, "Getting the song")

    def song_received(self, folder):
        self.refresh_songs(folder)
        self.log("Song added. Prepare it with the buttons under \"About this song\".")

    def prepared(self):
        return bool(self.notes().get("analysis"))

    def prepare_song(self, with_assistant=False):
        if self.folder:
            folder = self.folder
            self.run(lambda log: pipeline.prepare(folder, log, with_assistant=with_assistant),
                     lambda _: self.refresh_songs(folder),
                     "Preparing with your AI assistant" if with_assistant else "Preparing")

    def prepare_song_with_assistant(self):
        self.prepare_song(with_assistant=True)

    def open_profiles(self):
        ProfileDialog(self).exec()
        self.update_singer()

    def update_singer(self):
        pid = profiles.active()
        p = profiles.load(pid) if pid else None
        name = profiles.display_name(p) if p else None
        self.singer_btn.setText(f"Singer: {name}" if name else "Singer: create a vocal profile")
        self.singing_as.setText(f"Singing as {name}: your takes teach this profile." if name else
                                "No vocal profile: create one with \"Singer\" in the left panel, so your takes "
                                "can teach it your voice.")
        self.singing_as.setStyleSheet(f"color: {color('status')};")

    def tag_take(self, take, uploaded=False):
        """Note which profile a take was sung with, so processing it teaches that profile."""
        pid = profiles.active()
        if pid and take:
            notes = self.notes()
            tk = notes.setdefault("takes", {}).setdefault(take, {})
            tk["profile"] = pid
            tk["uploaded"] = uploaded
            pipeline.songfile.save(self.folder, notes)

    def connect_assistant(self):
        AssistantDialog(self).exec()
        self.update_ai_button()

    def update_ai_button(self):
        key = assistant.connected()
        name = assistant.ASSISTANTS[key]["name"] if key else None
        self.ai_btn.setText(f"AI assistant: {name}" if name else "Connect AI assistant")
        again = " again" if self.folder and self.prepared() else ""
        self.prep_btn.setText(f"Prepare{again}")
        self.prep_btn.setVisible(bool(self.folder))
        self.prep_ai_btn.setText(f"Prepare{again} with {name}" if name else "")
        self.prep_ai_btn.setVisible(bool(name and self.folder))

    def select_song(self, item, _prev=None):
        if item:
            self.load_song(Path(item.data(Qt.UserRole)))

    def load_song(self, folder):
        self.folder = folder
        notes = self.notes()
        self.song_info.setText(pipeline.summary(notes) + f"\n\nFolder: {folder}")
        self.update_ai_button()
        self.take_list.clear()
        for t in pipeline.takes(folder):
            self.take_list.addItem(t)
        if self.take_list.count():
            self.take_list.setCurrentRow(self.take_list.count() - 1)
        self.load_lyrics_view()
        self.update_result()

    # ------------------------------------------------------------ lyrics

    def load_lyrics_view(self):
        self.karaoke.set_lines(lyrics.load(self.folder) if self.folder else [])
        info = self.notes().get("lyrics") or {}
        if not self.karaoke.lines:
            self.lyrics_status.setText("")
        else:
            text = f"{len(self.karaoke.lines)} lines, from {info.get('source', 'lyrics.lrc')}"
            if info.get("approximate"):
                text += ". Timing is approximate: use Earlier/Later if it drifts."
            elif "shift_s" in info:
                text += f", lined up with this version of the song ({info.get('match_pct', '?')}% of lines matched)."
            self.lyrics_status.setText(text)

    def _save_lyrics_info(self, info):
        notes = self.notes()
        if info:
            notes["lyrics"] = info
            pipeline.songfile.save(self.folder, notes)

    def find_lyrics(self):
        if not self.folder:
            return
        folder, notes = self.folder, self.notes()
        self.run(lambda log: pipeline.find_lyrics(folder, notes, log),
                 lambda info: (self._save_lyrics_info(info), self.load_lyrics_view()), "Finding lyrics")

    def load_lyrics_file(self):
        if not self.folder:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose lyrics", "", "Lyrics (*.lrc *.txt)")
        if path:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            folder = self.folder
            self.run(lambda log: lyrics.from_user(folder, text, log),
                     lambda info: (self._save_lyrics_info(info), self.load_lyrics_view()), "Loading lyrics")

    def paste_lyrics(self):
        if not self.folder:
            return
        text, ok = QInputDialog.getMultiLineText(self, "Paste lyrics", "One line per sung line (timed .lrc text "
                                                 "works too):")
        if ok and text.strip():
            folder = self.folder
            self.run(lambda log: lyrics.from_user(folder, text, log),
                     lambda info: (self._save_lyrics_info(info), self.load_lyrics_view()), "Timing your lyrics")

    def nudge_lyrics(self, seconds):
        if not self.folder or not self.karaoke.lines:
            return
        lyrics.nudge(self.folder, seconds)
        notes = self.notes()
        info = notes.setdefault("lyrics", {})
        info["nudge_s"] = round(info.get("nudge_s", 0.0) + seconds, 2)
        pipeline.songfile.save(self.folder, notes)
        self.karaoke.set_lines(lyrics.load(self.folder))
        self.log(f"Lyrics moved {'earlier' if seconds < 0 else 'later'} (total {info['nudge_s']:+.2f} s)")

    def set_preview_volume(self, gain):
        if self.playing == "preview":
            self.audio.setVolume(gain)

    def set_result_volume(self, gain):
        if self.playing == "result":
            self.audio.setVolume(gain)

    def set_record_volume(self, gain):
        if self.recorder is not None:
            self.recorder.gain = gain

    def toggle_preview(self):
        if self.preview_timer.isActive():
            self.player.stop()
            self.preview_timer.stop()
            self.preview_btn.setText("Play song with lyrics")
            self.karaoke.set_lines(self.karaoke.lines)
        elif self.folder:
            self.player.setSource(QUrl.fromLocalFile(str(self.folder / "song.mp3")))
            self.playing = "preview"
            self.preview_volume.reset()
            self.player.play()
            self.preview_timer.start(50)
            self.preview_btn.setText("Stop preview")

    # ------------------------------------------------------------ step 2

    def record_with_song(self):
        self.toggle_record(instrumental=False)

    def record_with_instrumental(self):
        self.toggle_record(instrumental=True)

    def toggle_record(self, instrumental=False):
        if self.recorder is None:
            if not self.folder or not self.notes().get("recommended"):
                QMessageBox.information(self, "Not ready", "Prepare the song first (step 1).")
                return
            # the instrumental is split from the song sample for sample, so a take sung over it lines up
            # exactly like one sung over the original
            backing = self.folder / ("song_instrumental.wav" if instrumental else "song.mp3")
            if not backing.exists():
                QMessageBox.information(self, "Not ready", "Prepare the song again (step 1): its "
                                        "instrumental is missing.")
                return
            if self.preview_timer.isActive():
                self.toggle_preview()
            self.player.stop()
            take = pipeline.next_take(self.folder)
            self.recorder = rec.Recorder(backing, self.folder / f"{take}.wav",
                                         self.mic.currentData(), self.out.currentData(), song_volume=1.0)
            self.recorder.backing = "instrumental" if instrumental else "original"
            self.rec_volume.reset()
            self.finish_at = lyrics.sung_end(self.karaoke.lines,
                                             self.notes().get("analysis", {}).get("vocal_sections"))
            self.recorder.start()
            active, other = (self.rec_inst_btn, self.rec_btn) if instrumental else (self.rec_btn, self.rec_inst_btn)
            active.setText("Stop")
            other.setEnabled(False)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(50)
            self.log(f"Recording {take} over the {'instrumental' if instrumental else 'original song'}...")
        else:
            self.stop_record()

    def tick(self):
        r = self.recorder
        self.meter.setValue(min(100, int(r.level * 300)))
        self.karaoke.show_time(r.song_time)
        self.rec_time.setText(f"{int(r.seconds) // 60}:{int(r.seconds) % 60:02d} / "
                              f"{int(r.length) // 60}:{int(r.length) % 60:02d}")
        song_left = r.length - r.preroll - r.song_time
        self.finish_btn.setVisible(self.finish_at is not None and r.song_time >= self.finish_at and song_left > 2)
        if r.done.is_set():
            self.stop_record()

    def finish_early(self):
        if self.recorder is not None:
            t = self.recorder.song_time
            self.log(f"Lyrics done: finishing at {int(t) // 60}:{int(t) % 60:02d} instead of waiting for the "
                     "end of the song")
            self.stop_record()

    def stop_record(self):
        self.timer.stop()
        length = self.recorder.stop()
        peak = self.recorder.peak_db()
        self.log(f"Saved {self.recorder.out_path.name} ({length:.0f} s, loudest moment {peak:.1f} dBFS)")
        notes = self.notes()  # which backing it was sung over, for reference
        notes.setdefault("takes", {}).setdefault(self.recorder.out_path.stem, {})["recorded_over"] = self.recorder.backing
        pipeline.songfile.save(self.folder, notes)
        self.tag_take(self.recorder.out_path.stem)
        if peak > -1:
            QMessageBox.warning(self, "Too loud", "Your recording touched full scale and may be distorted. "
                                "Consider turning the mic down and recording again.")
        elif peak < -18:
            QMessageBox.information(self, "Quite quiet", "Your recording is very quiet. It will still work, "
                                    "but turning the mic up a little gives a cleaner result.")
        self.recorder = None
        self.finish_btn.setVisible(False)
        self.rec_btn.setText("Record")
        self.rec_inst_btn.setText("Record (instrumental)")
        self.rec_btn.setEnabled(True)
        self.rec_inst_btn.setEnabled(True)
        self.meter.setValue(0)
        self.load_song(self.folder)

    def upload_take(self):
        if not self.folder:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose your vocal recording", "",
                                              "Audio (*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.opus *.webm)")
        if path:
            folder = self.folder
            self.run(lambda log: pipeline.add_take_file(folder, path, log), self.take_uploaded,
                     "Adding your recording")

    def take_uploaded(self, take):
        self.tag_take(take, uploaded=True)
        self.load_song(self.folder)

    # ------------------------------------------------------------ step 3

    def current_take(self):
        item = self.take_list.currentItem()
        return item.text() if item else None

    def prepare_step3(self):
        take = self.current_take()
        self.take_label.setText(take or "no take yet: record or upload one in step 2")
        self.update_result()

    def make_cover(self):
        take = self.current_take()
        if not take:
            QMessageBox.information(self, "No take", "Record or upload a take in step 2 first.")
            return
        opts = {}  # the engine decides every setting from the song's analysis
        folder = self.folder
        self.player.stop()
        self.player.setSource(QUrl())  # release the file so it can be overwritten
        self.run(lambda log: pipeline.process(folder, take, opts, log),
                 lambda _: (self.refresh_songs(folder), self.update_result()), f"Making your cover from {take}")

    def cover_files(self):
        if not self.folder:
            return None, None
        name = self.folder.name.replace("-", "_")
        return self.folder / f"{name}_my_cover.wav", self.folder / f"{name}_my_cover.mp3"

    def update_result(self):
        wav, _ = self.cover_files()
        ready = bool(wav and wav.exists())
        for b in (self.play_cover, self.play_vocal, self.save_cover, self.save_vocal, self.approve_btn):
            b.setEnabled(ready)
        if not ready:
            self.result.setText("Not made yet.")
            return
        notes = self.notes()
        fin = notes.get("final", {})
        # covers made in chat record different (or no) result fields than app-made ones: use what's
        # there, take the original's loudness from the analysis and channels/length from the file
        r = notes.get("takes", {}).get(fin.get("take", ""), {}).get("results") or {}
        made = "in the app" if fin.get("made_with") == "app" else "with Claude"
        lines = [f"Made {made} from {fin.get('take', 'your take')} ({fin.get('version', 'main')} version). "
                 f"Status: {STATUS_TEXT.get(notes.get('status'), notes.get('status'))}."]
        info = sf.info(str(wav))
        facts = ["mono" if info.channels == 1 else "stereo", f"{int(info.duration) // 60}:{int(info.duration) % 60:02d}"]
        original = r.get("original_lufs", r.get("original_song_lufs", notes.get("analysis", {}).get("loudness_lufs")))
        if "song_lufs" in r:
            facts.insert(0, f"loudness {r['song_lufs']} LUFS" + (f" (original {original})" if original is not None else ""))
        if "peak_dbfs" in r:
            facts.append(f"peak {r['peak_dbfs']} dBFS")
        text = ", ".join(facts)
        lines.append(text[0].upper() + text[1:] + ".")
        if "vocal_vs_instrumental_lu" in r:
            line = f"Your vocal sits {r['vocal_vs_instrumental_lu']:+.1f} dB vs the music"
            if "original_lu" in r:
                line += f" (the original singer: {r['original_lu']:+.1f})"
            lines.append(line + ".")
        if r.get("note"):
            lines.append(r["note"])
        self.result.setText("\n".join(lines))

    def play(self, which):
        wav, _ = self.cover_files()
        path = wav if which == "cover" else self.folder / "my_vocal_processed.wav"
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.playing = "result"
        self.result_volume.reset()
        self.player.play()

    def download_cover(self):
        wav, mp3 = self.cover_files()
        target = QFileDialog.getExistingDirectory(self, "Save your cover to...")
        if target:
            for p in (wav, mp3):
                shutil.copy2(p, Path(target) / p.name)
            self.log(f"Saved {wav.name} and {mp3.name} to {target}")

    def download_vocal(self):
        src = self.folder / "my_vocal_processed.wav"
        name = f"{self.folder.name.replace('-', '_')}_my_vocal.wav"
        path, _ = QFileDialog.getSaveFileName(self, "Save your vocal", name, "WAV (*.wav)")
        if path:
            shutil.copy2(src, path)
            self.log(f"Saved your vocal to {path}")

    def approve(self):
        if QMessageBox.question(self, "Keep this cover?", "Mark this cover as final and move the working files "
                                "to the Recycle Bin? The song, its parts, your recordings, your vocal and the "
                                "cover are kept.") != QMessageBox.Yes:
            return
        self.player.stop()
        self.player.setSource(QUrl())
        folder = self.folder
        self.run(lambda log: pipeline.approve(folder, log), lambda _: self.refresh_songs(folder), "Tidying up")


LOG_FILE = pipeline.ROOT / "vocalworks.log"


def setup_logging():
    """The app runs without a console (pythonw), so send output, errors and native crash dumps to a file."""
    import faulthandler

    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    log.write(f"\n===== Vocalworks started {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log
    faulthandler.enable(log)  # a hard crash still leaves a Python stack trace in the log

    def excepthook(kind, value, tb):
        text = "".join(traceback.format_exception(kind, value, tb))
        log.write(text)
        QMessageBox.critical(None, "Vocalworks error", f"{value}\n\nDetails are in {LOG_FILE.name}.")

    sys.excepthook = excepthook
    return log


ICON = Path(__file__).resolve().parent / "assets" / "vocalworks.ico"


APP_ID = "Vocalworks.DesktopApp"


def set_taskbar_identity(hwnd):
    """Give the window its own taskbar identity with the Vocalworks icon, name and relaunch command.

    The process-wide app ID alone isn't enough: Windows then looks for a Start menu shortcut carrying
    that ID, finds none, and shows a blank icon. These window properties tell it directly (they're also
    what a pinned taskbar button uses)."""
    import ctypes
    import uuid
    from ctypes import wintypes as wt

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("value", ctypes.c_void_p), ("pad", ctypes.c_void_p)]

    def guid(text):
        u, g = uuid.UUID(text), GUID()
        g.a, g.b, g.c = u.fields[0], u.fields[1], u.fields[2]
        g.d[:] = list(u.bytes[8:])
        return g

    store = ctypes.c_void_p()
    iid = guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")  # IPropertyStore
    if ctypes.windll.shell32.SHGetPropertyStoreForWindow(wt.HWND(hwnd), ctypes.byref(iid), ctypes.byref(store)):
        return
    vtbl = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                   ctypes.POINTER(PROPVARIANT))(vtbl[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    root = ICON.parent.parent.parent
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    relaunch = (f'"{pythonw}" -c "import os,sys;os.chdir(r\'{root}\');sys.path.insert(0,r\'{root}\');'
                f'from app.main import main;main()"')
    # System.AppUserModel.*: RelaunchCommand 2, RelaunchIconResource 3, RelaunchDisplayNameResource 4, ID 5;
    # the ID goes last, once the rest is in place
    for pid, text in ((2, relaunch), (3, f"{ICON},0"), (4, "Vocalworks"), (5, APP_ID)):
        buf = ctypes.create_unicode_buffer(text)
        pv = PROPVARIANT(vt=31, value=ctypes.cast(buf, ctypes.c_void_p))  # VT_LPWSTR; SetValue copies it
        set_value(store, ctypes.byref(PROPERTYKEY(guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid)),
                  ctypes.byref(pv))
    commit(store)
    release(store)


def main():
    log = setup_logging()
    if sys.platform == "win32":
        # its own taskbar identity, so Windows doesn't group it under Python (see set_taskbar_identity)
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(ICON)))
    app.setApplicationName("Vocalworks")
    w = MainWindow()
    if sys.platform == "win32":
        try:
            set_taskbar_identity(int(w.winId()))  # winId creates the native window before it's shown
        except Exception:  # noqa: BLE001 - only the taskbar icon depends on it
            traceback.print_exc(file=log)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
