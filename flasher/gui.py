"""PySide6 GUI for the RX58_ESP32C5 flasher.

Thin window over flasher.engine.  All network / serial work runs in a Worker
QThread so the UI stays responsive; the window only reads widget state, builds
a job closure, and renders log / progress signals.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QCheckBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import __version__, config, engine
from .engine import FlashError
from .resources import asset_path

CACHE_DIR = Path.home() / ".rx58-esp32c5-flasher" / "cache"
FLASH_MODE = "full"     # the manifest's flash mode this tool writes


class Worker(QThread):
    log = Signal(str)
    progress = Signal(float)        # 0..1, or -1 for "indeterminate / busy"
    done = Signal(bool, str)        # (ok, message)

    def __init__(self, job):
        super().__init__()
        self._job = job

    def run(self):
        try:
            self._job(self.log.emit, self.progress.emit)
            self.done.emit(True, "Flash complete — receiver(s) restarted.")
        except FlashError as e:
            self.done.emit(False, str(e))
        except Exception as e:  # last-resort guard so the thread never dies silently
            self.done.emit(False, f"Unexpected error: {e}")


class ReleaseLoader(QThread):
    """Fetches the GitHub release list off the UI thread so the window can show
    immediately and stay responsive while GitHub is queried."""
    loaded = Signal(list)   # list[dict] of release dicts, newest first
    failed = Signal(str)

    def __init__(self, include_prereleases: bool):
        super().__init__()
        self._include_prereleases = include_prereleases

    def run(self):
        try:
            self.loaded.emit(engine.list_releases(include_prereleases=self._include_prereleases))
        except FlashError as e:
            self.failed.emit(str(e))


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{config.PRODUCT} Flasher {__version__}")
        self.setMinimumWidth(720)
        icon = asset_path("icon.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self._releases: list[dict] = []
        self._worker: Worker | None = None
        self._rel_loader: ReleaseLoader | None = None
        self._build_ui()
        self.refresh_ports()
        self.refresh_releases()   # populate the version dropdown on startup

    # ── UI construction ─────────────────────────────────────────────────────
    def _build_ui(self):
        outer = QHBoxLayout(self)

        # Left column: board image, with developer credit (and shop link, once
        # there is one) stacked beneath it.
        left = QVBoxLayout()
        banner_pix = QPixmap(str(asset_path("product.png")))
        if not banner_pix.isNull():
            banner = QLabel()
            banner.setPixmap(banner_pix.scaledToHeight(400, Qt.SmoothTransformation))
            banner.setAlignment(Qt.AlignHCenter)
            left.addWidget(banner)

        board = QLabel(config.BOARD)
        board.setAlignment(Qt.AlignHCenter)
        left.addWidget(board)
        credit = QLabel(f"Developed by {config.DEVELOPER}")
        credit.setAlignment(Qt.AlignHCenter)
        left.addWidget(credit)

        if config.SHOP_URL:
            shop = QLabel(f'<a href="{config.SHOP_URL}">{config.SHOP_LINK_TEXT}</a>')
            shop.setOpenExternalLinks(True)   # opens in the system browser
            shop.setAlignment(Qt.AlignHCenter)
            left.addWidget(shop)

        left.addStretch()
        outer.addLayout(left, 0)

        # Right: all controls.
        root = QVBoxLayout()
        outer.addLayout(root, 1)

        root.addWidget(QLabel(f"<b>{config.PRODUCT} firmware flasher</b> — RX5808 replacement "
                              f"on a {config.BOARD}"))

        # Source: GitHub vs local files
        src_box = QGroupBox("Firmware source")
        src_lay = QHBoxLayout(src_box)
        self.rb_github = QRadioButton("Download from GitHub")
        self.rb_local = QRadioButton("Use a local file")
        self.rb_github.setChecked(True)
        src_grp = QButtonGroup(self)
        src_grp.addButton(self.rb_github)
        src_grp.addButton(self.rb_local)
        src_lay.addWidget(self.rb_github)
        src_lay.addWidget(self.rb_local)
        src_lay.addStretch()
        root.addWidget(src_box)

        # Stacked source panels
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_github_panel())
        self.stack.addWidget(self._build_local_panel())
        root.addWidget(self.stack)
        self.rb_github.toggled.connect(lambda on: self.stack.setCurrentIndex(0 if on else 1))

        # Options.  Every flash writes the full image (bootloader, partition
        # table, app): right for a new, bricked or working board alike, since
        # the receiver keeps no settings in flash.
        opt_box = QGroupBox("Options")
        opt_lay = QVBoxLayout(opt_box)
        opt_lay.addWidget(QLabel("Writes the full image: works for a new, bricked or running board."))
        self.cb_erase = QCheckBox("Erase the entire flash first (slower; clears everything)")
        opt_lay.addWidget(self.cb_erase)
        root.addWidget(opt_box)

        # Devices: only ESP32 ports are listed; check one or more to flash them all.
        port_box = QGroupBox("Devices (ESP32)")
        port_lay = QVBoxLayout(port_box)
        head = QHBoxLayout()
        head.addWidget(QLabel("Check each board to flash:"))
        head.addStretch()
        btn_ports = QPushButton("Refresh")
        btn_ports.clicked.connect(self.refresh_ports)
        head.addWidget(btn_ports)
        port_lay.addLayout(head)
        self.port_list = QListWidget()
        self.port_list.setMaximumHeight(120)
        port_lay.addWidget(self.port_list)
        hint = QLabel("New board, or one that won't connect? Hold <b>BOOT</b> (B) while "
                      "plugging it in, then click Refresh.")
        hint.setWordWrap(True)
        port_lay.addWidget(hint)
        root.addWidget(port_box)

        # Flash button
        self.btn_flash = QPushButton("Flash")
        self.btn_flash.setMinimumHeight(36)
        self.btn_flash.clicked.connect(self.start_flash)
        root.addWidget(self.btn_flash)

        # Progress + log
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(180)
        root.addWidget(self.log_view, 1)

    def _build_github_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Version:"))
        row = QHBoxLayout()
        self.release_combo = QComboBox()
        self.release_combo.setMinimumWidth(420)
        self.btn_refresh_rel = QPushButton("Refresh")
        self.btn_refresh_rel.clicked.connect(self.refresh_releases)
        row.addWidget(self.release_combo, 1)
        row.addWidget(self.btn_refresh_rel)
        lay.addLayout(row)
        self.cb_prerelease = QCheckBox("Include pre-releases (beta)")
        self.cb_prerelease.toggled.connect(self.refresh_releases)
        lay.addWidget(self.cb_prerelease)
        return w

    def _build_local_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        # The label is built from the same constant the flash job uses, so it
        # cannot drift out of sync with what actually gets written.
        off = config.LOCAL_LAYOUT["offsets"]
        self.row_merged = self._file_row(f"Full image (offset {off['merged']}):")
        lay.addLayout(self.row_merged["layout"])
        note = QLabel("firmware.factory.bin from a build (.pio\\build\\xiao_c5), or "
                      f"{config.PRODUCT}-merged.bin from a release.")
        note.setWordWrap(True)
        lay.addWidget(note)
        return w

    def _file_row(self, label: str) -> dict:
        lay = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setMinimumWidth(180)
        edit = QLineEdit()
        btn = QPushButton("Browse…")

        def browse():
            path, _ = QFileDialog.getOpenFileName(self, "Select .bin", "", "Firmware images (*.bin);;All files (*)")
            if path:
                edit.setText(path)

        btn.clicked.connect(browse)
        lay.addWidget(lbl)
        lay.addWidget(edit, 1)
        lay.addWidget(btn)
        return {"layout": lay, "label": lbl, "edit": edit, "btn": btn}

    # ── Data refresh ──────────────────────────────────────────────────────────
    def refresh_ports(self):
        self.port_list.clear()
        # Only ESP32-like ports (matched by USB vendor id) are offered.
        ports = [p for p in engine.list_ports() if p[2]]
        if not ports:
            item = QListWidgetItem("(no ESP32 boards found — plug one in and click Refresh)")
            item.setFlags(Qt.NoItemFlags)
            self.port_list.addItem(item)
            return
        for device, desc, _likely in ports:
            item = QListWidgetItem(f"{device} — {desc}")
            item.setData(Qt.UserRole, device)
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            # Pre-check a lone board only: with several ESP32s attached, the
            # user picks which ones to flash.
            item.setCheckState(Qt.Checked if len(ports) == 1 else Qt.Unchecked)
            self.port_list.addItem(item)

    def selected_ports(self) -> list[str]:
        """Devices the user has checked, in list order."""
        out = []
        for i in range(self.port_list.count()):
            item = self.port_list.item(i)
            if (item.flags() & Qt.ItemIsUserCheckable) and item.checkState() == Qt.Checked:
                device = item.data(Qt.UserRole)
                if device:
                    out.append(device)
        return out

    def refresh_releases(self):
        if self._rel_loader and self._rel_loader.isRunning():
            return
        self.btn_refresh_rel.setEnabled(False)
        self.release_combo.clear()
        self.release_combo.addItem("Loading versions…", userData=-1)
        self.log("Querying GitHub for releases…")
        self._rel_loader = ReleaseLoader(self.cb_prerelease.isChecked())
        self._rel_loader.loaded.connect(self._on_releases_loaded)
        self._rel_loader.failed.connect(self._on_releases_failed)
        self._rel_loader.start()

    def _on_releases_loaded(self, releases: list):
        self._releases = releases
        self.btn_refresh_rel.setEnabled(True)
        self.release_combo.clear()
        if not releases:
            self.release_combo.addItem("(no releases found)", userData=-1)
            self.log("No releases published yet.")
            return
        # Releases come newest-first, so index 0 is the latest.
        for i, r in enumerate(releases):
            tag = r.get("tag_name", "?")
            label = f"{config.PRODUCT} {tag}"
            if i == 0:
                label += " (latest)"
            if r.get("prerelease"):
                label += " — beta"
            self.release_combo.addItem(label, userData=i)
        self.release_combo.setCurrentIndex(0)   # default to the latest
        self.log(f"Found {len(releases)} release(s).")

    def _on_releases_failed(self, message: str):
        self._releases = []
        self.btn_refresh_rel.setEnabled(True)
        self.release_combo.clear()
        self.release_combo.addItem("(could not load releases)", userData=-1)
        self.log(f"[ERROR] {message}")

    # ── Logging / progress slots ────────────────────────────────────────────
    def log(self, text: str):
        self.log_view.appendPlainText(text)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_progress(self, frac: float):
        if frac < 0:
            self.progress.setRange(0, 0)  # busy / indeterminate
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(frac * 100))

    # ── Flash orchestration ───────────────────────────────────────────────────
    def start_flash(self):
        if self._worker and self._worker.isRunning():
            return
        ports = self.selected_ports()
        if not ports:
            self.log("[ERROR] No board selected. Plug one in, click Refresh, and check it.")
            return
        erase = self.cb_erase.isChecked()

        try:
            job = self._build_github_job(ports, erase) if self.rb_github.isChecked() \
                else self._build_local_job(ports, erase)
        except FlashError as e:
            self.log(f"[ERROR] {e}")
            return

        self.log_view.clear()
        self.log(f"=== Flashing {len(ports)} board(s): {', '.join(ports)} ===")
        self.btn_flash.setEnabled(False)
        self.set_progress(-1)
        self._worker = Worker(job)
        self._worker.log.connect(self.log)
        self._worker.progress.connect(self.set_progress)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, message: str):
        self.set_progress(1.0 if ok else 0.0)
        self.log("")
        self.log(("SUCCESS — " if ok else "FAILED — ") + message)
        self.btn_flash.setEnabled(True)

    @staticmethod
    def _flash_each(chip, baud, ports, pairs, log, erase):
        """Flash the same images to every selected port in turn. One board
        failing doesn't abort the rest; failures are collected and reported."""
        failures = []
        for n, port in enumerate(ports, 1):
            log("")
            log(f"--- Board {n}/{len(ports)}: {port} ---")
            try:
                engine.flash(chip, baud, port, pairs, log_cb=log, erase=erase)
                log(f"  {port}: done.")
            except FlashError as e:
                failures.append(port)
                log(f"  [ERROR] {port}: {e}")
        if failures:
            raise FlashError(
                f"{len(failures)} of {len(ports)} board(s) failed: {', '.join(failures)}"
            )

    def _build_github_job(self, ports: list[str], erase: bool):
        idx = self.release_combo.currentData()
        if idx is None or idx < 0 or idx >= len(self._releases):
            raise FlashError("No release selected. Click Refresh and choose one.")
        release = self._releases[idx]
        manifest = engine.get_manifest(release)
        keys = manifest.get("flash_modes", {}).get(FLASH_MODE)
        if not keys:
            raise FlashError(f"This release doesn't define a '{FLASH_MODE}' flash mode.")

        assets = engine.assets_by_name(release)
        # Validate up front so the user gets an immediate, clear error.
        for key in keys:
            spec = manifest.get("assets", {}).get(key)
            if not spec:
                raise FlashError(f"Release lacks a '{key}' asset.")
            if spec["name"] not in assets:
                raise FlashError(f"Release is missing asset file {spec['name']}.")
        tag = release.get("tag_name", "release")
        chip, baud = manifest["chip"], int(manifest["baud"])
        app_offset = manifest.get("layout", {}).get("app_offset", config.LOCAL_LAYOUT["app_offset"])

        def job(log, progress):
            cache = CACHE_DIR / tag
            pairs = []
            for key in keys:
                spec = manifest["assets"][key]
                name, offset = spec["name"], spec["offset"]
                dest = cache / name
                if dest.exists() and dest.stat().st_size > 0:
                    log(f"Using cached {name}")
                else:
                    engine.download(assets[name], dest, progress_cb=progress, log_cb=log)
                engine.check_image(dest, offset, chip, app_offset)
                pairs.append((offset, dest))
            progress(-1)
            self._flash_each(chip, baud, ports, pairs, log, erase)

        return job

    def _build_local_job(self, ports: list[str], erase: bool):
        off = config.LOCAL_LAYOUT["offsets"]["merged"]
        path = self.row_merged["edit"].text().strip().strip('"')
        if not path:
            raise FlashError("Please select the full image (.bin) to flash.")
        p = Path(path)
        if not p.is_file():
            raise FlashError(f"File not found: {path}")
        chip = config.LOCAL_LAYOUT["chip"]
        baud = int(config.LOCAL_LAYOUT["baud"])
        engine.check_image(p, off, chip, config.LOCAL_LAYOUT["app_offset"])
        pairs = [(off, p)]

        def job(log, progress):
            progress(-1)
            log(f"Local file — writing at {off}: {p.name}")
            self._flash_each(chip, baud, ports, pairs, log, erase)

        return job


def main():
    app = QApplication([])
    win = MainWindow()
    win.show()
    return app.exec()
