"""Audioworks desktop app: pick a song, record or upload your vocal, download your cover."""
import shutil
import sys

import soundfile as sf
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog, QInputDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from app import assistant
from app import lyrics
from app import pipeline
from app import recorder as rec

STATUS_TEXT = {"imported": "not prepared", "analyzed": "ready for vocals", "in progress": "in progress",
               "awaiting feedback": "cover ready", "approved": "approved", "on hold": "on hold"}


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
        for size, color, bold in ((12, "#8a8a8a", False), (22, "#1565c0", True), (14, "#404040", False),
                                  (12, "#8a8a8a", False)):
            lab = QLabel("")
            lab.setAlignment(Qt.AlignCenter)
            lab.setWordWrap(True)
            font = QFont(lab.font())
            font.setPointSize(size)
            font.setBold(bold)
            lab.setFont(font)
            lab.setStyleSheet(f"color: {color};")
            v.addWidget(lab)
            self.rows.append(lab)
        v.addStretch(1)
        self.set_lines([])

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
            "Audioworks guide and plan your cover; the app then builds the cover from that plan, checked "
            "against the same rules. It runs through the assistant's official app on this PC, signed in "
            "with your own account, and Audioworks only starts it inside its own folder. The assistant "
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Audioworks")
        self.resize(1100, 720)
        self.folder = None
        self.thread = None
        self.recorder = None
        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)

        # left: songs
        self.songs = QListWidget()
        self.songs.setWordWrap(True)
        self.songs.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.songs.setSpacing(2)
        self.songs.currentItemChanged.connect(self.select_song)
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
        self.update_ai_button()
        self.refresh_songs()
        self.show_step(0)

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
        self.notice.setStyleSheet("color: #a06000;")
        self.get_btn = QPushButton("Get song and prepare")
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
        self.reprep_btn = QPushButton("Prepare again")
        self.reprep_btn.clicked.connect(self.prepare_again)
        self.ai_plan_btn = QPushButton()
        self.ai_plan_btn.clicked.connect(self.ask_assistant)
        self.song_info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        iv.addWidget(self.song_info, 1)
        row = QHBoxLayout()
        row.addWidget(self.reprep_btn)
        row.addWidget(self.ai_plan_btn)
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
        self.karaoke.setStyleSheet("background: #fafafa;")
        self.lyrics_status = QLabel("")
        self.lyrics_status.setWordWrap(True)
        self.lyrics_status.setStyleSheet("color: #606060;")
        row1 = QHBoxLayout()
        self.preview_btn = QPushButton("Play song with lyrics")
        self.preview_btn.clicked.connect(self.toggle_preview)
        earlier = QPushButton("Lyrics earlier")
        earlier.clicked.connect(lambda: self.nudge_lyrics(-0.25))
        later = QPushButton("Lyrics later")
        later.clicked.connect(lambda: self.nudge_lyrics(0.25))
        for b in (self.preview_btn, earlier, later):
            row1.addWidget(b)
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
        self.rec_btn.clicked.connect(self.toggle_record)
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
        row.addWidget(self.rec_time, 1)
        f.addRow("", row)
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
        print(line, flush=True)  # also lands in audioworks.log

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

        def job(log):
            folder = fetch(log)
            pipeline.prepare(folder, log)
            return folder

        self.run(job, lambda folder: (self.refresh_songs(folder), self.show_step(1)), "Getting the song")

    def prepare_again(self):
        if self.folder:
            folder = self.folder
            self.run(lambda log: pipeline.prepare(folder, log), lambda _: self.refresh_songs(folder), "Preparing")

    def connect_assistant(self):
        AssistantDialog(self).exec()
        self.update_ai_button()

    def update_ai_button(self):
        key = assistant.connected()
        name = assistant.ASSISTANTS[key]["name"] if key else None
        self.ai_btn.setText(f"AI assistant: {name}" if name else "Connect AI assistant")
        self.ai_plan_btn.setText(f"Ask {name} to plan this song" if name else "")
        self.ai_plan_btn.setVisible(bool(name and self.folder))

    def ask_assistant(self):
        if self.folder:
            folder = self.folder
            self.run(lambda log: assistant.study(folder, log), lambda _: self.refresh_songs(folder),
                     "Planning with your AI assistant")

    def select_song(self, item, _prev=None):
        if item:
            self.load_song(Path(item.data(Qt.UserRole)))

    def load_song(self, folder):
        self.folder = folder
        notes = self.notes()
        self.song_info.setText(pipeline.summary(notes) + f"\n\nFolder: {folder}")
        self.reprep_btn.setVisible(True)
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

    def toggle_preview(self):
        if self.preview_timer.isActive():
            self.player.stop()
            self.preview_timer.stop()
            self.preview_btn.setText("Play song with lyrics")
            self.karaoke.set_lines(self.karaoke.lines)
        elif self.folder:
            self.player.setSource(QUrl.fromLocalFile(str(self.folder / "song.mp3")))
            self.player.play()
            self.preview_timer.start(50)
            self.preview_btn.setText("Stop preview")

    # ------------------------------------------------------------ step 2

    def toggle_record(self):
        if self.recorder is None:
            if not self.folder or not self.notes().get("recommended"):
                QMessageBox.information(self, "Not ready", "Prepare the song first (step 1).")
                return
            if self.preview_timer.isActive():
                self.toggle_preview()
            self.player.stop()
            take = pipeline.next_take(self.folder)
            self.recorder = rec.Recorder(self.folder / "song.mp3", self.folder / f"{take}.wav",
                                         self.mic.currentData(), self.out.currentData())
            self.recorder.start()
            self.rec_btn.setText("Stop")
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(50)
            self.log(f"Recording {take}...")
        else:
            self.stop_record()

    def tick(self):
        r = self.recorder
        self.meter.setValue(min(100, int(r.level * 300)))
        self.karaoke.show_time(r.song_time)
        self.rec_time.setText(f"{int(r.seconds) // 60}:{int(r.seconds) % 60:02d} / "
                              f"{int(r.length) // 60}:{int(r.length) % 60:02d}")
        if r.done.is_set():
            self.stop_record()

    def stop_record(self):
        self.timer.stop()
        length = self.recorder.stop()
        peak = self.recorder.peak_db()
        self.log(f"Saved {self.recorder.out_path.name} ({length:.0f} s, loudest moment {peak:.1f} dBFS)")
        if peak > -1:
            QMessageBox.warning(self, "Too loud", "Your recording touched full scale and may be distorted. "
                                "Consider turning the mic down and recording again.")
        elif peak < -18:
            QMessageBox.information(self, "Quite quiet", "Your recording is very quiet. It will still work, "
                                    "but turning the mic up a little gives a cleaner result.")
        self.recorder = None
        self.rec_btn.setText("Record")
        self.meter.setValue(0)
        self.load_song(self.folder)

    def upload_take(self):
        if not self.folder:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose your vocal recording", "",
                                              "Audio (*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.opus *.webm)")
        if path:
            folder = self.folder
            self.run(lambda log: pipeline.add_take_file(folder, path, log), lambda _: self.load_song(folder),
                     "Adding your recording")

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


LOG_FILE = pipeline.ROOT / "audioworks.log"


def setup_logging():
    """The app runs without a console (pythonw), so send output, errors and native crash dumps to a file."""
    import faulthandler

    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    log.write(f"\n===== Audioworks started {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log
    faulthandler.enable(log)  # a hard crash still leaves a Python stack trace in the log

    def excepthook(kind, value, tb):
        text = "".join(traceback.format_exception(kind, value, tb))
        log.write(text)
        QMessageBox.critical(None, "Audioworks error", f"{value}\n\nDetails are in {LOG_FILE.name}.")

    sys.excepthook = excepthook
    return log


ICON = Path(__file__).resolve().parent / "assets" / "audioworks.ico"


def main():
    log = setup_logging()
    if sys.platform == "win32":
        # its own taskbar identity, so Windows shows the Audioworks icon instead of grouping it under Python
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Audioworks.DesktopApp")
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(ICON)))
    app.setApplicationName("Audioworks")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
