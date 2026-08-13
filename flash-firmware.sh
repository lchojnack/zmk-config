#!/bin/bash

# Script to flash firmware to XIAO-SENSE device
# Usage: ./flash-firmware.sh <build-folder>
#
# Examples:
#   ./flash-firmware.sh xiao_ble_zmk_totem_dongle    # Flash dongle
#   ./flash-firmware.sh xiao_ble_zmk_totem_left      # Flash left keyboard half
#   ./flash-firmware.sh xiao_ble_zmk_totem_right     # Flash right keyboard half
#   ./flash-firmware.sh xiao_ble_zmk_totem_trackball # Flash trackball
#   ./flash-firmware.sh xiao_ble_zmk_settings_reset  # Flash settings reset
#
# Tab completion: source completions/flash-firmware.bash

set -e

BUILD_FOLDER="${1:-}"

if [ -z "$BUILD_FOLDER" ]; then
    echo "Error: No build folder specified"
    echo "Usage: ./flash-firmware.sh <build-folder>"
    echo "Example: ./flash-firmware.sh xiao_ble_zmk_totem_dongle"
    exit 1
fi

FIRMWARE="./build/${BUILD_FOLDER}/zephyr/zmk.uf2"

if [ ! -f "$FIRMWARE" ]; then
    echo "Error: Firmware file not found: $FIRMWARE"
    echo "Build the firmware first with: ./build_local.sh build ${BUILD_FOLDER}"
    exit 1
fi

echo "Found: $FIRMWARE"
echo ""
echo "Waiting for XIAO-SENSE device (10s timeout)..."
echo "Put the device in bootloader mode (double-tap reset button)"

# Wait for device block to appear, then auto-mount if needed
MOUNT_POINT=""
DEVICE_NODE=""
TIMEOUT=10
ELAPSED=0

while [ $ELAPSED -lt $TIMEOUT ]; do
    # First check if already mounted at a known path
    for path in "/media/$USER/XIAO-SENSE" "/media/XIAO-SENSE" "/run/media/$USER/XIAO-SENSE"; do
        if [ -d "$path" ]; then
            MOUNT_POINT="$path"
            break 2
        fi
    done

    # Otherwise, locate the block device by label
    DEVICE_NODE=$(lsblk -rno NAME,LABEL | awk '$2=="XIAO-SENSE"{print "/dev/"$1; exit}')
    if [ -n "$DEVICE_NODE" ]; then
        break
    fi

    sleep 1
    ELAPSED=$((ELAPSED + 1))
    echo -n "."
done
echo ""

if [ -z "$MOUNT_POINT" ] && [ -n "$DEVICE_NODE" ]; then
    # Already mounted by another process?
    MOUNT_POINT=$(findmnt -rno TARGET "$DEVICE_NODE" || true)

    if [ -z "$MOUNT_POINT" ]; then
        echo "Mounting $DEVICE_NODE..."
        if command -v udisksctl >/dev/null 2>&1; then
            MOUNT_OUTPUT=$(udisksctl mount -b "$DEVICE_NODE" 2>&1)
            echo "$MOUNT_OUTPUT"
            # udisksctl prints "Mounted /dev/sdX1 at /path" (note: may end with a period on some versions)
            MOUNT_POINT=$(printf '%s' "$MOUNT_OUTPUT" | sed -n 's/.* at \(.*\)\.*$/\1/p' | sed 's/\.$//')
            # Fall back to querying the kernel if parsing failed
            [ -z "$MOUNT_POINT" ] && MOUNT_POINT=$(findmnt -rno TARGET "$DEVICE_NODE" || true)
        else
            MOUNT_POINT="/tmp/xiao-sense-$$"
            mkdir -p "$MOUNT_POINT"
            sudo mount "$DEVICE_NODE" "$MOUNT_POINT"
            NEEDS_UMOUNT=1
        fi
    fi
fi

if [ -z "$MOUNT_POINT" ]; then
    echo "Error: XIAO-SENSE device not found"
    echo "Please put the device in bootloader mode (double-tap reset button)"
    exit 1
fi

echo "Found device at: $MOUNT_POINT"
echo "Copying firmware..."

cp "$FIRMWARE" "$MOUNT_POINT/"
sync

echo ""
echo "✓ Firmware flashed successfully!"
echo "Device will reboot automatically"
