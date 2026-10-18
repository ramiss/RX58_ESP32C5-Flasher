"""Flash engine — GUI-agnostic.

Lists the firmware repo's GitHub releases, reads each release's
flash-manifest.json instead of hardcoding offsets, and drives esptool
in-process (so it works inside a PyInstaller one-file bundle, where there is
no `python -m esptool` subprocess to call).

The XIAO ESP32-C5 is flashed WITHOUT esptool's DTR/RTS reset.  With the
receiver firmware running, that reset wedges the board's USB until it is
unplugged.  Instead (the same sequence as the firmware repo's
tools/pio_upload.py):

  1. Send the console key 'B'.  The receiver firmware answers
     "download_boot=requested" and restarts into the ROM loader.  A board with
     no (or other) firmware is expected to be in the loader already: the user
     holds BOOT while plugging it in.
  2. Check the loader answers and is the right chip; flash with no reset.
  3. Clear LP_AON_FORCE_DOWNLOAD_BOOT (it survives resets) and start the new
     firmware with a watchdog reset.

Nothing here imports PySide6 — the GUI layer wraps these functions in a worker
thread and passes callbacks for logging / progress.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import config

GITHUB_API = "https://api.github.com"
_UA = "RX58_ESP32C5-Flasher"
MANIFEST_ASSET = "flash-manifest.json"

# Reset-free download entry and exit (firmware repo: src/dlboot.c,
# tools/pio_upload.py).
DLBOOT_KEY = b"B"
DLBOOT_ACK = b"download_boot=requested"
LP_AON_SYS_CFG = 0x600B1034
FORCE_DOWNLOAD_MASK = {"esp32c5": 0x60000000, "esp32c6": 0x40000000}

BOOT_HELP = ("Unplug the board, hold its BOOT button (B), plug it back in, "
             "release BOOT, click Refresh, then Flash again.")


class FlashError(Exception):
    """Any user-facing failure (network, missing asset, esptool error)."""


# ── GitHub release discovery ────────────────────────────────────────────────

def _api_get(url: str):
    req = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": _UA})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        if e.code == 404:
            raise FlashError(f"Not found on GitHub: {url}")
        if e.code == 403:
            raise FlashError("GitHub API rate limit reached. Try again in a few minutes.")
        raise FlashError(f"GitHub returned HTTP {e.code}.")
    except URLError as e:
        raise FlashError(f"Could not reach GitHub: {e.reason}. Check your internet connection.")


def list_releases(include_prereleases: bool = True, limit: int = 30) -> list[dict]:
    """Return published releases, newest first, as the raw GitHub release dicts."""
    url = f"{GITHUB_API}/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/releases?per_page={limit}"
    releases = _api_get(url)
    out = []
    for r in releases:
        if r.get("draft"):
            continue
        if r.get("prerelease") and not include_prereleases:
            continue
        out.append(r)
    return out


def assets_by_name(release: dict) -> dict[str, str]:
    """Map asset filename -> browser_download_url for a release."""
    return {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}


def get_manifest(release: dict) -> dict:
    """Fetch and validate a release's flash-manifest.json."""
    url = assets_by_name(release).get(MANIFEST_ASSET)
    if not url:
        raise FlashError(f"Release {release.get('tag_name', '?')} has no {MANIFEST_ASSET}; "
                         "it can't be flashed with this tool.")
    req = Request(url, headers={"User-Agent": _UA})
    try:
        with urlopen(req, timeout=30) as resp:
            manifest = json.loads(resp.read())
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        raise FlashError(f"Could not read {MANIFEST_ASSET}: {e}")

    schema = manifest.get("schema", 0)
    if schema > config.SUPPORTED_SCHEMA:
        raise FlashError(
            f"This release needs a newer flasher (manifest schema {schema}, "
            f"this build supports {config.SUPPORTED_SCHEMA}). Please update the tool."
        )
    return manifest


# ── Download ────────────────────────────────────────────────────────────────

def download(url: str, dest: Path, progress_cb=None, log_cb=None) -> Path:
    """Download `url` to `dest`. progress_cb(fraction 0..1) and log_cb(str) optional."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if log_cb:
        log_cb(f"Downloading {dest.name} ...")
    req = Request(url, headers={"User-Agent": _UA})
    tmp = dest.with_suffix(dest.suffix + ".part")
    read = 0
    try:
        with urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    read += len(chunk)
                    if total and progress_cb:
                        progress_cb(read / total)
    except (HTTPError, URLError) as e:
        raise FlashError(f"Download failed for {dest.name}: {e}")
    tmp.replace(dest)   # only a complete download lands in the cache
    if log_cb:
        log_cb(f"  saved {read:,} bytes")
    return dest


# ── Image checks ────────────────────────────────────────────────────────────

def _check_header(blob: bytes, at: int, chip: str, what: str):
    if len(blob) < at + 16 or blob[at] != 0xE9:
        raise FlashError(f"{what}: no ESP image at offset {at:#x}. Wrong file?")
    chip_id = int.from_bytes(blob[at + 12:at + 14], "little")
    want = config.IMAGE_CHIP_ID.get(chip)
    if want is not None and chip_id != want:
        names = {v: k for k, v in config.IMAGE_CHIP_ID.items()}
        raise FlashError(f"{what} is built for {names.get(chip_id, f'chip id {chip_id}')}, "
                         f"not {chip}. Wrong file?")


def check_image(path: Path, offset: str, chip: str, app_offset: str):
    """Refuse an image that isn't for `chip` before anything is written.

    A merged image (written at 0x0) must hold the bootloader and the app at
    their offsets; an app image (written at the app offset) must be an app."""
    blob = Path(path).read_bytes()
    off = int(str(offset), 16)
    app = int(str(app_offset), 16)
    if off == 0:
        # App first: its chip ID gives the clearest "wrong file" message.
        _check_header(blob, app, chip, f"{Path(path).name} (app)")
        _check_header(blob, config.BOOTLOADER_OFFSET.get(chip, 0), chip, f"{Path(path).name} (bootloader)")
    elif off == app:
        _check_header(blob, 0, chip, Path(path).name)


# ── Serial port detection ─────────────────────────────────────────────────────

def list_ports() -> list[tuple[str, str, bool]]:
    """Return [(device, description, is_likely_esp32), ...]."""
    try:
        import serial.tools.list_ports
    except ImportError:
        return []
    out = []
    for p in serial.tools.list_ports.comports():
        likely = p.vid in config.ESP32_VIDS
        out.append((p.device, p.description or "", likely))
    # ESP32-looking ports first.
    out.sort(key=lambda t: (not t[2], t[0]))
    return out


def _port_present(port: str) -> bool:
    return any(d == port for d, _desc, _l in list_ports())


# ── esptool, in-process ──────────────────────────────────────────────────────

class _Tee(io.TextIOBase):
    """Forwards everything esptool prints to a line-oriented callback, handling
    the in-place '\\r' progress updates esptool emits during write-flash, and
    keeps a copy for parsing."""

    def __init__(self, log_cb):
        self._log_cb = log_cb
        self._buf = ""
        self.text = ""
        self._busy = False

    def write(self, s):
        if not s:
            return 0
        if self._busy:
            # The callback itself printed (stdout is redirected to us while
            # esptool runs): pass it to the real stdout instead of recursing.
            if sys.__stdout__:
                sys.__stdout__.write(s)
            return len(s)
        self._busy = True
        try:
            return self._write(s)
        finally:
            self._busy = False

    def _write(self, s):
        self._buf += s
        self.text += s
        # esptool uses both \n (lines) and \r (progress redraws); split on both.
        while True:
            idx = min((i for i in (self._buf.find("\n"), self._buf.find("\r")) if i >= 0), default=-1)
            if idx < 0:
                break
            line, self._buf = self._buf[:idx], self._buf[idx + 1:]
            if line.strip():
                self._log_cb(line)
        return len(s)

    def flush(self):
        if self._busy:
            return
        if self._buf.strip():
            self._busy = True
            try:
                self._log_cb(self._buf)
            finally:
                self._busy = False
            self._buf = ""


def _esptool(argv: list[str], log_cb=None, echo=True) -> tuple[bool, str]:
    """Run esptool with `argv`. Returns (ok, everything it printed)."""
    try:
        import esptool
    except ImportError:
        raise FlashError("esptool is not bundled with this build (developer error).")
    sink = _Tee(log_cb or (lambda _l: None))
    if log_cb and echo:
        log_cb(f"$ esptool {' '.join(argv)}")
    ok = True
    try:
        with redirect_stdout(sink), redirect_stderr(sink):
            esptool.main(argv)
    except SystemExit as e:  # esptool exits on argument/usage errors
        ok = e.code in (0, None)
    except Exception as e:   # FatalError, serial errors, sync failures, ...
        sink.write(f"{e}\n")
        ok = False
    finally:
        sink.flush()
    return ok, sink.text


def _base(chip: str, port: str, baud: int | None = None) -> list[str]:
    argv = ["--chip", chip, "--port", port]
    if baud:
        argv += ["--baud", str(baud)]
    return argv + ["--before", "no-reset"]


# ── Reset-free download entry / exit ─────────────────────────────────────────

def request_download(port: str) -> bool:
    """Send the download key to the running firmware. True if acknowledged."""
    import serial
    for _ in range(3):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout, s.write_timeout = port, 115200, 0.05, 0.5
        s.dtr = False   # asserting DTR/RTS on open would trigger the USB reset
        s.rts = False
        try:
            s.open()
        except serial.SerialException:
            time.sleep(0.5)
            continue
        seen = b""
        try:
            s.reset_input_buffer()
            s.write(DLBOOT_KEY)
            deadline = time.time() + 1.0
            while time.time() < deadline and DLBOOT_ACK not in seen:
                seen = (seen + s.read(512))[-4096:]
        except serial.SerialTimeoutException:
            pass        # nobody reads the port: the ROM loader or a wedged chip
        except serial.SerialException:
            seen += DLBOOT_ACK  # the port vanished mid-read: the restart happened
        finally:
            try:
                s.close()
            except serial.SerialException:
                pass
        if DLBOOT_ACK in seen:
            return True
        time.sleep(0.3)
    return False


def _wait_for_port(port: str, timeout: float = 10.0):
    """After a restart the USB port re-enumerates; wait until it is back."""
    time.sleep(0.5)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_present(port):
            time.sleep(1.0)
            return
        time.sleep(0.25)


def detect_loader_chip(port: str) -> str | None:
    """Ask the ROM loader on `port` which chip it is ("esp32c5", ...), without
    resetting anything. None if no loader answers."""
    ok, out = _esptool(["--chip", "auto", "--port", port, "--before", "no-reset",
                        "--after", "no-reset", "read-mac"], echo=False)
    if not ok:
        return None
    m = re.search(r"ESP32-?([A-Z]\d+)?", out)
    if not m:
        return None
    return "esp32" + (m.group(1) or "").lower()


def start_firmware(chip: str, port: str, log_cb=None) -> bool:
    """Leave the ROM loader and start the flashed firmware (no DTR/RTS reset)."""
    log = log_cb or (lambda _l: None)
    mask = FORCE_DOWNLOAD_MASK.get(chip)
    if mask is not None:
        ok, _ = _esptool(_base(chip, port) + ["--after", "no-reset", "write-mem",
                                              hex(LP_AON_SYS_CFG), "0", hex(mask)], echo=False)
        if not ok:
            log("  Could not clear the download flag; unplug and replug the board.")
            return False
    if chip == "esp32c6":
        # esptool has no watchdog reset for the C6; arm the LP watchdog by hand.
        for reg, val in (("0x600B1C18", "0x50D83AA1"),   # LP_WDT_RWDT_WPROTECT: unlock
                         ("0x600B1C04", "2000"),         # CONFIG1: stage-0 timeout
                         ("0x600B1C00", "0xD0000102")):  # CONFIG0: enable, system reset
            _esptool(_base(chip, port) + ["--after", "no-reset", "write-mem", reg, val], echo=False)
        time.sleep(1.0)
        return True
    ok, out = _esptool(_base(chip, port) + ["--after", "watchdog-reset", "read-mac"], echo=False)
    # esptool can report an error after a successful watchdog reset because the
    # port disappears while it closes it.
    if ok or "watchdog" in out.lower():
        return True
    log("  Reset failed; unplug and replug the board to start it.")
    return False


def enter_loader(chip: str, port: str, log_cb=None):
    """Get the board on `port` into its ROM loader without a DTR/RTS reset, and
    make sure it is a `chip`. Raises FlashError otherwise."""
    log = log_cb or (lambda _l: None)
    if request_download(port):
        log("  Receiver firmware restarted into the ROM loader.")
        _wait_for_port(port)
    found = detect_loader_chip(port)
    if found is None:
        raise FlashError(f"No answer from {port}: neither the receiver firmware nor the "
                         f"ROM loader. {BOOT_HELP}")
    if found != chip:
        # Not our board (e.g. another ESP32 on the bench): put it back.
        start_firmware(found, port)
        raise FlashError(f"{port} is an {found.upper()}, not an {chip.upper()}. "
                         "Left it unchanged and restarted it.")
    log(f"  ROM loader ready ({found.upper()}).")


def flash(chip: str, baud: int, port: str, pairs: list[tuple[str, str]], log_cb=None,
          erase: bool = False):
    """Flash (offset, file) pairs to `port`, entering and leaving the ROM loader
    without a DTR/RTS reset.  Raises FlashError on any failure."""
    log = log_cb or (lambda _l: None)
    enter_loader(chip, port, log)
    if erase:
        ok, _ = _esptool(_base(chip, port, baud) + ["--after", "no-reset", "erase-flash"], log)
        if not ok:
            raise FlashError("Erasing the flash failed.")
    argv = _base(chip, port, baud) + [
        "--after", "no-reset",
        "write-flash", "-z",
        "--flash-mode", "dio",      # the XIAO ESP32-C5 does not boot in QIO
        "--flash-freq", "80m",
        "--flash-size", "keep",     # the build already set it (8 MB)
    ]
    for offset, path in pairs:
        argv += [offset, str(path)]
    ok, _ = _esptool(argv, log)
    if not ok:
        raise FlashError(f"Flashing failed. If the board stopped answering: {BOOT_HELP}")
    if start_firmware(chip, port, log):
        log("  New firmware started.")
