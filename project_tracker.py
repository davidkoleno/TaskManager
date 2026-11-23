import os
import sys
import sqlite3
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QMimeData
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QTextEdit, QListWidget, QListWidgetItem, QDialog,
    QFormLayout, QComboBox, QMessageBox, QTabWidget, QInputDialog, QFrame,
    QScrollArea
)

# Change this to a company share that both PCs can read.
SHARED_FOLDER = Path(r"\\YOUR-SERVER\YOUR-SHARE\ProjectTracker")
LOCAL_FOLDER = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "ProjectTracker"
LOCAL_DB = LOCAL_FOLDER / "projects.sqlite3"
SNAPSHOT = SHARED_FOLDER / "projects_snapshot.sqlite3"
STATUSES = ["Backlog", "To Do", "In Progress", "Blocked", "In Review", "Completed"]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db(path, readonly=False):
    if readonly:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        db = sqlite3.connect(path)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
    db.row_factory = sqlite3.Row
    return db


def setup_db(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, client TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Backlog', priority TEXT NOT NULL DEFAULT 'Medium',
            description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            category TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            title TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS time_entries (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            task_id TEXT REFERENCES tasks(id), started_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL, notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS timer_state (
            id INTEGER PRIMARY KEY CHECK(id=1), project_id TEXT REFERENCES projects(id),
            task_id TEXT REFERENCES tasks(id), accumulated_seconds INTEGER NOT NULL DEFAULT 0,
            started_at TEXT, running INTEGER NOT NULL DEFAULT 0, checkpoint_at TEXT
        );
        INSERT OR IGNORE INTO timer_state(id) VALUES (1);
        CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
        CREATE INDEX IF NOT EXISTS idx_time_project ON time_entries(project_id);
    """)
    # Never count shutdown/crash time as billable work.
    db.execute("UPDATE timer_state SET running=0, started_at=NULL WHERE id=1")
    db.commit()


def publish_snapshot(db):
    if "YOUR-SERVER" in str(SHARED_FOLDER):
        raise RuntimeError("Set SHARED_FOLDER to your actual company network path first.")
    SHARED_FOLDER.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tracker-publish-") as tmp:
        safe_local = Path(tmp) / "snapshot.sqlite3"
        with sqlite3.connect(safe_local) as backup_db:
            db.backup(backup_db)
        pending = SHARED_FOLDER / f".snapshot-{uuid.uuid4().hex}.tmp"
        try:
            shutil.copyfile(safe_local, pending)
            os.replace(pending, SNAPSHOT)  # Same-share replacement.
        finally:
            pending.unlink(missing_ok=True)


def load_viewer_db():
    if not SNAPSHOT.is_file():
        raise FileNotFoundError(f"No published snapshot found at:\n{SNAPSHOT}")
    LOCAL_FOLDER.mkdir(parents=True, exist_ok=True)
    destination = LOCAL_FOLDER / "manager_snapshot.sqlite3"
    pending = LOCAL_FOLDER / "manager_snapshot.pending"
    shutil.copyfile(SNAPSHOT, pending)
    os.replace(pending, destination)
    return connect_db(destination, readonly=True)


class DropColumn(QListWidget):
    moved = Signal(str, str)

    def __init__(self, status, viewer=False):
        super().__init__()
        self.status = status
        self.viewer = viewer
        self.setSpacing(5)
        if not viewer:
            self.setDragEnabled(True)
            self.setAcceptDrops(True)
            self.setDropIndicatorShown(True)

    def startDrag(self, _actions):
        item = self.currentItem()
        if not item or self.viewer:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(str(item.data(Qt.ItemDataRole.UserRole)))
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasText() and not self.viewer:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasText() and not self.viewer:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasText() and not self.viewer:
            self.moved.emit(event.mimeData().text(), self.status)
            event.acceptProposedAction()
        else:
            event.ignore()


class ProjectDialog(QDialog):
    changed = Signal()

    def __init__(self, db, project_id, viewer=False, parent=None):
        super().__init__(parent)
        self.db, self.project_id, self.viewer = db, project_id, viewer
        p = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        self.setWindowTitle(p["title"])
        self.resize(650, 550)
        root = QVBoxLayout(self)
        header = QLabel(f"{p['title']}  |  {p['status']}  |  {p['priority']} priority")
        header.setStyleSheet("font-weight: bold; font-size: 16px")
        root.addWidget(header)
        tabs = QTabWidget()
        root.addWidget(tabs)

        details = QWidget()
        form = QFormLayout(details)
        self.title_edit = QLineEdit(p["title"])
        self.client_edit = QLineEdit(p["client"])
        self.description_edit = QTextEdit(p["description"])
        self.priority_edit = QComboBox()
        self.priority_edit.addItems(["Low", "Medium", "High", "Critical"])
        self.priority_edit.setCurrentText(p["priority"])
        for widget in (self.title_edit, self.client_edit, self.description_edit, self.priority_edit):
            widget.setEnabled(not viewer)
        form.addRow("Project", self.title_edit)
        form.addRow("Client", self.client_edit)
        form.addRow("Priority", self.priority_edit)
        form.addRow("Description", self.description_edit)
        if not viewer:
            save = QPushButton("Save Details")
            save.clicked.connect(self.save_details)
            form.addRow(save)
        tabs.addTab(details, "Details")

        notes_tab = QWidget()
        notes_layout = QVBoxLayout(notes_tab)
        self.notes_list = QListWidget()
        notes_layout.addWidget(self.notes_list)
        self.note_category = QComboBox()
        self.note_category.addItems(["Progress", "Blocker", "Resolution", "Next Step", "General"])
        self.note_body = QTextEdit()
        self.note_body.setPlaceholderText("What changed? What did you work on?")
        self.note_body.setMaximumHeight(95)
        if not viewer:
            notes_layout.addWidget(self.note_category)
            notes_layout.addWidget(self.note_body)
            add_note = QPushButton("Add Note")
            add_note.clicked.connect(self.add_note)
            notes_layout.addWidget(add_note)
        tabs.addTab(notes_tab, "Activity")

        tasks_tab = QWidget()
        task_layout = QVBoxLayout(tasks_tab)
        self.task_list = QListWidget()
        task_layout.addWidget(self.task_list)
        if not viewer:
            task_row = QHBoxLayout()
            self.task_title = QLineEdit()
            self.task_title.setPlaceholderText("New task")
            task_add = QPushButton("Add Task")
            task_add.clicked.connect(self.add_task)
            task_row.addWidget(self.task_title)
            task_row.addWidget(task_add)
            task_layout.addLayout(task_row)
            self.task_list.itemChanged.connect(self.task_toggled)
        tabs.addTab(tasks_tab, "Tasks")

        report_tab = QWidget()
        report_layout = QVBoxLayout(report_tab)
        self.report_format = QComboBox()
        self.report_format.addItems(["Workfront", "OneNote"])
        self.report_format.currentTextChanged.connect(self.render_report)
        self.report_text = QTextEdit()
        report_layout.addWidget(self.report_format)
        report_layout.addWidget(self.report_text)
        copy = QPushButton("Copy Report to Clipboard")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report_text.toPlainText()))
        report_layout.addWidget(copy)
        tabs.addTab(report_tab, "Exports")

        self.reload()

    def reload(self):
        self.notes_list.clear()
        for n in self.db.execute("SELECT * FROM notes WHERE project_id=? ORDER BY created_at DESC", (self.project_id,)):
            self.notes_list.addItem(f"{n['created_at'][:16]}  [{n['category']}]\n{n['body']}")
        self.task_list.blockSignals(True)
        self.task_list.clear()
        for t in self.db.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at", (self.project_id,)):
            item = QListWidgetItem(t["title"])
            item.setData(Qt.ItemDataRole.UserRole, t["id"])
            if not self.viewer:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if t["done"] else Qt.CheckState.Unchecked)
            self.task_list.addItem(item)
        self.task_list.blockSignals(False)
        self.render_report()

    def save_details(self):
        if not self.title_edit.text().strip():
            return
        with self.db:
            self.db.execute("UPDATE projects SET title=?,client=?,priority=?,description=?,updated_at=? WHERE id=?",
                            (self.title_edit.text().strip(), self.client_edit.text().strip(),
                             self.priority_edit.currentText(), self.description_edit.toPlainText(),
                             utc_now(), self.project_id))
        self.changed.emit()
        self.setWindowTitle(self.title_edit.text().strip())

    def add_note(self):
        body = self.note_body.toPlainText().strip()
        if not body:
            return
        with self.db:
            self.db.execute("INSERT INTO notes VALUES (?,?,?,?,?)",
                            (uuid.uuid4().hex, self.project_id, self.note_category.currentText(), body, utc_now()))
        self.note_body.clear()
        self.reload()
        self.changed.emit()

    def add_task(self):
        title = self.task_title.text().strip()
        if not title:
            return
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?)",
                            (uuid.uuid4().hex, self.project_id, title, 0, utc_now()))
        self.task_title.clear()
        self.reload()
        self.changed.emit()

    def task_toggled(self, item):
        with self.db:
            self.db.execute("UPDATE tasks SET done=? WHERE id=?",
                            (int(item.checkState() == Qt.CheckState.Checked), item.data(Qt.ItemDataRole.UserRole)))
        self.changed.emit()

    def render_report(self):
        p = self.db.execute("SELECT * FROM projects WHERE id=?", (self.project_id,)).fetchone()
        notes = self.db.execute("SELECT category,body,created_at FROM notes WHERE project_id=? ORDER BY created_at DESC LIMIT 25", (self.project_id,)).fetchall()
        tasks = self.db.execute("SELECT title,done FROM tasks WHERE project_id=?", (self.project_id,)).fetchall()
        total = self.db.execute("SELECT COALESCE(SUM(duration_seconds),0) FROM time_entries WHERE project_id=?", (self.project_id,)).fetchone()[0]
        if self.report_format.currentText() == "Workfront":
            lines = [f"Project: {p['title']}", f"Status: {p['status']}",
                     f"Priority: {p['priority']}", f"Recorded time: {total / 3600:.2f} hours", "", "Recent updates:"]
            lines += [f"- [{n['category']}] {n['body']}" for n in notes] or ["- No updates recorded"]
            lines += ["", "Open tasks:"] + [f"- {t['title']}" for t in tasks if not t["done"]]
        else:
            lines = [p["title"], f"Status: {p['status']}", f"Time tracked: {total / 3600:.2f} hours", "", "PROJECT NOTES"]
            lines += [f"{n['created_at'][:10]} | {n['category']} | {n['body']}" for n in notes]
            lines += ["", "CHECKLIST"] + [f"{'[x]' if t['done'] else '[ ]'} {t['title']}" for t in tasks]
        self.report_text.setPlainText("\n".join(lines))


class MainWindow(QMainWindow):
    def __init__(self, viewer=False):
        super().__init__()
        self.viewer = viewer
        LOCAL_FOLDER.mkdir(parents=True, exist_ok=True)
        self.db = load_viewer_db() if viewer else connect_db(LOCAL_DB)
        if not viewer:
            setup_db(self.db)
        self.setWindowTitle("Project Tracker" + (" — Manager View" if viewer else ""))
        self.resize(1250, 750)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        toolbar = QHBoxLayout()
        root.addLayout(toolbar)
        if not viewer:
            add = QPushButton("+ New Project")
            add.clicked.connect(self.add_project)
            toolbar.addWidget(add)
            publish = QPushButton("Publish Now")
            publish.clicked.connect(self.publish)
            toolbar.addWidget(publish)
        else:
            refresh = QPushButton("Refresh Published Data")
            refresh.clicked.connect(self.refresh_viewer)
            toolbar.addWidget(refresh)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search projects...")
        self.search.textChanged.connect(self.refresh_board)
        toolbar.addWidget(self.search)
        self.columns = {}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root.addWidget(scroll, 1)
        content = QWidget()
        board = QHBoxLayout(content)
        board.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(content)
        for status in STATUSES:
            col_widget = QFrame()
            col_widget.setMinimumWidth(185)
            col_widget.setFrameShape(QFrame.Shape.StyledPanel)
            col_layout = QVBoxLayout(col_widget)
            heading = QLabel(status)
            heading.setStyleSheet("font-size: 15px; font-weight: bold")
            col_layout.addWidget(heading)
            col = DropColumn(status, viewer)
            col.itemDoubleClicked.connect(self.open_project)
            col.moved.connect(self.move_project)
            col_layout.addWidget(col)
            self.columns[status] = col
            board.addWidget(col_widget)
        if not viewer:
            timer_row = QHBoxLayout()
            root.addLayout(timer_row)
            self.timer_project = QComboBox()
            self.timer_project.currentIndexChanged.connect(self.load_timer_tasks)
            self.timer_task = QComboBox()
            self.timer_label = QLabel("00:00:00")
            self.timer_button = QPushButton("Start")
            self.timer_button.clicked.connect(self.toggle_timer)
            self.stop_button = QPushButton("Stop & Save")
            self.stop_button.clicked.connect(self.stop_timer)
            for w in (QLabel("Timer:"), self.timer_project, self.timer_task, self.timer_label,
                      self.timer_button, self.stop_button):
                timer_row.addWidget(w)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.timer_tick)
            self.timer.start(1000)
            self.publisher = QTimer(self)
            self.publisher.timeout.connect(lambda: self.publish(quiet=True))
            self.publisher.start(60 * 60 * 1000)
            self._tick_base = None
            self.reload_timer()
        self.statusBar().showMessage("Read-only published data" if viewer else "Local data saved automatically")
        self.refresh_board()

    def refresh_board(self):
        query = self.search.text().lower().strip()
        for col in self.columns.values():
            col.clear()
        for p in self.db.execute("SELECT * FROM projects ORDER BY updated_at DESC"):
            if query and query not in (p["title"] + " " + p["client"]).lower():
                continue
            item = QListWidgetItem(f"{p['title']}\n{p['client']}  •  {p['priority']}")
            item.setData(Qt.ItemDataRole.UserRole, p["id"])
            item.setSizeHint(item.sizeHint().expandedTo(item.sizeHint()))
            if p["status"] in self.columns:
                self.columns[p["status"]].addItem(item)
        if not self.viewer:
            current_id = self.timer_project.currentData()
            self.timer_project.blockSignals(True)
            self.timer_project.clear()
            for p in self.db.execute("SELECT id,title FROM projects ORDER BY title"):
                self.timer_project.addItem(p["title"], p["id"])
            idx = self.timer_project.findData(current_id)
            if idx >= 0:
                self.timer_project.setCurrentIndex(idx)
            self.timer_project.blockSignals(False)
            self.load_timer_tasks()

    def add_project(self):
        title, ok = QInputDialog.getText(self, "New Project", "Project name:")
        if not ok or not title.strip():
            return
        now = utc_now()
        pid = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO projects(id,title,created_at,updated_at) VALUES (?,?,?,?)",
                            (pid, title.strip(), now, now))
        self.refresh_board()
        self.show_project(pid)

    def move_project(self, project_id, new_status):
        if self.viewer:
            return
        p = self.db.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
        if p and p["status"] != new_status:
            with self.db:
                self.db.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", (new_status, utc_now(), project_id))
                self.db.execute("INSERT INTO notes VALUES (?,?,?,?,?)",
                                (uuid.uuid4().hex, project_id, "General",
                                 f"Status changed: {p['status']} → {new_status}", utc_now()))
        self.refresh_board()

    def open_project(self, item):
        self.show_project(item.data(Qt.ItemDataRole.UserRole))

    def show_project(self, project_id):
        dialog = ProjectDialog(self.db, project_id, self.viewer, self)
        dialog.changed.connect(self.refresh_board)
        dialog.exec()
        self.refresh_board()

    def load_timer_tasks(self):
        if self.viewer:
            return
        current = self.timer_task.currentData()
        self.timer_task.clear()
        self.timer_task.addItem("General project work", None)
        pid = self.timer_project.currentData()
        if pid:
            for t in self.db.execute("SELECT id,title FROM tasks WHERE project_id=? ORDER BY title", (pid,)):
                self.timer_task.addItem(t["title"], t["id"])
        idx = self.timer_task.findData(current)
        if idx >= 0:
            self.timer_task.setCurrentIndex(idx)

    def reload_timer(self):
        row = self.db.execute("SELECT * FROM timer_state WHERE id=1").fetchone()
        self._tick_base = time.monotonic() if row["running"] else None
        self.timer_button.setText("Pause" if row["running"] else "Start")
        self.timer_project.setEnabled(not bool(row["running"]))
        self.timer_task.setEnabled(not bool(row["running"]))
        if row["project_id"]:
            idx = self.timer_project.findData(row["project_id"])
            if idx >= 0:
                self.timer_project.setCurrentIndex(idx)
                j = self.timer_task.findData(row["task_id"])
                if j >= 0:
                    self.timer_task.setCurrentIndex(j)
        self.timer_tick()

    def timer_tick(self):
        row = self.db.execute("SELECT * FROM timer_state WHERE id=1").fetchone()
        seconds = row["accumulated_seconds"]
        if row["running"] and self._tick_base is not None:
            seconds += int(time.monotonic() - self._tick_base)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        self.timer_label.setText(f"{h:02}:{m:02}:{s:02}")
        if row["running"] and self._tick_base is not None and seconds % 10 == 0:
            with self.db:
                self.db.execute("UPDATE timer_state SET accumulated_seconds=?,checkpoint_at=? WHERE id=1",
                                (seconds, utc_now()))
            self._tick_base = time.monotonic()

    def toggle_timer(self):
        row = self.db.execute("SELECT * FROM timer_state WHERE id=1").fetchone()
        if row["running"]:
            elapsed = int(time.monotonic() - self._tick_base)
            with self.db:
                self.db.execute("UPDATE timer_state SET running=0,accumulated_seconds=accumulated_seconds+?,checkpoint_at=? WHERE id=1",
                                (elapsed, utc_now()))
        else:
            pid = row["project_id"] or self.timer_project.currentData()
            if not pid:
                QMessageBox.information(self, "No Project", "Create a project first.")
                return
            task_id = row["task_id"] if row["project_id"] else self.timer_task.currentData()
            with self.db:
                self.db.execute("UPDATE timer_state SET project_id=?,task_id=?,running=1,started_at=COALESCE(started_at,?),checkpoint_at=? WHERE id=1",
                                (pid, task_id, utc_now(), utc_now()))
        self.reload_timer()

    def stop_timer(self):
        row = self.db.execute("SELECT * FROM timer_state WHERE id=1").fetchone()
        if not row["project_id"]:
            return
        elapsed = int(time.monotonic() - self._tick_base) if row["running"] and self._tick_base is not None else 0
        total = row["accumulated_seconds"] + elapsed
        with self.db:
            if total > 0:
                self.db.execute("INSERT INTO time_entries VALUES (?,?,?,?,?,?)",
                                (uuid.uuid4().hex, row["project_id"], row["task_id"], row["started_at"] or utc_now(), total, ""))
            self.db.execute("UPDATE timer_state SET project_id=NULL,task_id=NULL,accumulated_seconds=0,started_at=NULL,running=0,checkpoint_at=NULL WHERE id=1")
        self.reload_timer()
        self.statusBar().showMessage(f"Saved {total // 60} minutes", 5000)

    def publish(self, quiet=False):
        try:
            publish_snapshot(self.db)
            self.statusBar().showMessage(f"Published: {datetime.now().strftime('%I:%M %p')}")
        except Exception as exc:
            self.statusBar().showMessage(f"Publish failed: {exc}")
            if not quiet:
                QMessageBox.warning(self, "Publish Failed", str(exc))

    def refresh_viewer(self):
        try:
            replacement = load_viewer_db()
            old = self.db
            self.db = replacement
            old.close()
            self.refresh_board()
            self.statusBar().showMessage("Published snapshot loaded")
        except Exception as exc:
            QMessageBox.warning(self, "Refresh Failed", str(exc))

    def closeEvent(self, event):
        if not self.viewer:
            row = self.db.execute("SELECT running FROM timer_state WHERE id=1").fetchone()
            if row["running"] and self._tick_base is not None:
                elapsed = int(time.monotonic() - self._tick_base)
                with self.db:
                    self.db.execute("UPDATE timer_state SET running=0, accumulated_seconds=accumulated_seconds+?, checkpoint_at=? WHERE id=1",
                                    (elapsed, utc_now()))
                self._tick_base = None
            try:
                publish_snapshot(self.db)
            except Exception as exc:
                choice = QMessageBox.question(self, "Publish Failed",
                    f"Latest work is safely saved locally, but publishing failed:\n{exc}\n\nClose anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if choice != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
        self.db.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    viewer = "--viewer" in sys.argv
    try:
        window = MainWindow(viewer)
    except Exception as exc:
        QMessageBox.critical(None, "Project Tracker", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
