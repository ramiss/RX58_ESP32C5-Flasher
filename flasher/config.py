"""Static configuration for the RX58_ESP32C5 flasher.

This is a *separate* repo from the firmware, so it can't read the firmware's
git remote at runtime — the source repo is hardcoded here.  Everything about
the *flash layout* (chip, baud, offsets) is read from each release's
`flash-manifest.json` instead, so this file rarely needs to change.
"""

# GitHub repo that publishes the firmware releases.
GITHUB_OWNER = "ramiss"
GITHUB_REPO = "RX58_ESP32C5"

PRODUCT = "RX58_ESP32C5"
BOARD = "Seeed XIAO ESP32-C5"

# Shown under the product banner in the GUI.
DEVELOPER = "Richard Amiss"

# Shop / product link shown under the banner.  Leave "" until there is one:
# the GUI shows nothing while it's empty and a clickable link once it's set.
SHOP_URL = ""
SHOP_LINK_TEXT = "Buy a board"

# USB vendor IDs that indicate an attached ESP32.  Used to list devices.  The
# XIAO ESP32-C5 enumerates as Espressif's native USB Serial/JTAG (0x303A).
ESP32_VIDS = {
    0x10C4,  # Silicon Labs CP210x
    0x1A86,  # QinHeng CH340/CH341
    0x0403,  # FTDI
    0x303A,  # Espressif native USB
    0x239A,  # Adafruit
    0x2341,  # Arduino
}

# Flash layout used by "Use local files" mode: the full image the firmware
# build produces (.pio/build/xiao_c5/firmware.factory.bin), written at 0x0.
# KEEP IN SYNC with the firmware repo's partitions.csv and the manifest that
# its .github/workflows/release.yml writes.
LOCAL_LAYOUT = {
    "chip": "esp32c5",
    "baud": 460800,
    "offsets": {
        "merged": "0x0",       # bootloader (0x2000) + table (0x8000) + app (0x10000)
    },
    "app_offset": "0x10000",
}

# ESP image header chip IDs (esptool targets/*.py IMAGE_CHIP_ID): every image
# is checked against the target chip before anything is written.
IMAGE_CHIP_ID = {"esp32c5": 23, "esp32c6": 13}

# Bootloader offset inside a merged image, per chip.
BOOTLOADER_OFFSET = {"esp32c5": 0x2000, "esp32c6": 0x0}

# Highest manifest schema this build understands.  The firmware repo bumps
# the schema when a release needs a newer flasher, so older flashers refuse
# it ("please update the tool") rather than writing it wrongly.
SUPPORTED_SCHEMA = 1
