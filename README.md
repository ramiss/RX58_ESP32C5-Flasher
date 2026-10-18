# RX58_ESP32C5 Flasher

A portable Windows GUI tool for flashing the
[RX58_ESP32C5](https://github.com/ramiss/RX58_ESP32C5) firmware onto a Seeed
XIAO ESP32-C5. It pulls any published release straight from GitHub, or
flashes a local `.bin` you browse to.

**[Download RX58_ESP32C5-Flasher.exe](https://github.com/ramiss/RX58_ESP32C5-Flasher/releases/latest/download/RX58_ESP32C5-Flasher.exe)**
(Windows). No installation: it's a single self-contained file (Python,
esptool and the UI are all bundled).

## Looking for the firmware?

This repo is only the flasher. **Everything else lives in the firmware repo,
[ramiss/RX58_ESP32C5](https://github.com/ramiss/RX58_ESP32C5):**

- **What it is:** firmware that turns a XIAO ESP32-C5 into a drop-in
  replacement for the RX5808 5.8 GHz FPV receiver module, with a
  multi-channel mode that measures up to 8 channels at once.
- **[README](https://github.com/ramiss/RX58_ESP32C5#readme):** wiring, the
  RSSI filter, flashing on other platforms, building from source.
- **[Host interface datasheet](https://github.com/ramiss/RX58_ESP32C5/blob/main/docs/RX58_ESP32C5_SPI_Interface.md):**
  how a lap timer or scanner talks to it.
- **[Releases](https://github.com/ramiss/RX58_ESP32C5/releases):** the
  firmware images this tool downloads.
- **Issues** with the receiver itself belong there; issues with this tool
  belong here.

## Using it

1. Plug the XIAO ESP32-C5 into a USB port with a **data** USB-C cable (not
   charge-only). **A new board** (or one that doesn't answer): unplug it,
   hold its **BOOT** button (marked B), plug it in, release BOOT.
2. Run `RX58_ESP32C5-Flasher.exe`.
3. Pick a release (or browse for a local file), check the board in
   **Devices**, and click **Flash**.

Every flash writes the full image (bootloader, partition table and app at
`0x0`), so the same steps work for a new, bricked or running board. The
receiver keeps no settings in flash, so nothing is lost.

## How it flashes without unplugging

esptool's usual DTR/RTS reset wedges the XIAO ESP32-C5's USB while the
receiver firmware runs, so this tool never uses it. Instead it sends the
firmware's console key `B` (the firmware restarts into its ROM loader),
flashes with no reset, clears the force-download flag and starts the new
firmware with a watchdog reset: the same sequence as the firmware repo's
`tools/pio_upload.py`. Before writing it checks that the board's ROM loader
reports an ESP32-C5 and that the image is built for one; a different ESP32
on the same PC is left alone and restarted.

## How it stays compatible with the firmware

The flasher does **not** hardcode flash offsets. It reads each
[release](https://github.com/ramiss/RX58_ESP32C5/releases)'s
`flash-manifest.json` (written by the firmware repo's
`.github/workflows/release.yml`) and refuses manifests with a newer `schema`
than it understands.

## Developing

```
pip install -r requirements.txt
python run.py
```

- `python tools/make_assets.py` regenerates the board image and icon from
  `tools/source/xiao_esp32c5.png` (needs Pillow).
- **Build .exe locally** VSCode task, or `pyinstaller flasher.spec` →
  `dist/RX58_ESP32C5-Flasher.exe`.
- **Deploy to GitHub** / **Release** VSCode tasks push to `main` / tag a
  version; `.github/workflows/build.yml` builds the exe and attaches it to the
  release.
