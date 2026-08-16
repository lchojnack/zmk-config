#!/usr/bin/env bash
#
# Put the Totem's pointer on libinput's flat acceleration profile.
#
# The firmware already applies a velocity curve (&pointer_accel on the dongle,
# tuned for a 3840x2160 27" panel). libinput's default "adaptive" profile applies a
# second curve on top - it decelerates slow movement and adds its own gain at speed
# - so the two multiply and the firmware tuning stops predicting what the hand
# feels. Flat means unit gain: the firmware curve is the only curve.
#
# Usage:
#   ./setup-pointer.sh            # apply to the running session
#   ./setup-pointer.sh --status   # show current state, change nothing
#   ./setup-pointer.sh --persist  # also install an xorg.conf.d rule (needs sudo)
#   ./setup-pointer.sh --revert   # back to libinput's defaults, remove the rule
#
# The session change is lost when the device re-enumerates (replug, dongle reset)
# or on logout; --persist survives both.

set -euo pipefail

DEVICE_NAME="${DEVICE_NAME:-ZMK Project TOTEM Mouse}"
XORG_RULE="/etc/X11/xorg.conf.d/50-zmk-totem-pointer.conf"
PROP="libinput Accel Profile Enabled"
# libinput exposes the profiles as a bitmask in a fixed order: adaptive, flat.
FLAT="0, 1"

info() { echo -e "\033[1;34m[INFO]\033[0m $1"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $1"; }
err() { echo -e "\033[0;31m[ERROR]\033[0m $1" >&2; }
ok() { echo -e "\033[0;32m[OK]\033[0m $1"; }

command -v xinput >/dev/null 2>&1 || {
    err "xinput not found. This script is for X11; under Wayland set the flat"
    err "profile through your compositor instead."
    exit 1
}

if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
    warn "Session is Wayland - xinput will not affect it."
fi

# Every pointer whose name matches; a keyboard can expose more than one.
mapfile -t IDS < <(xinput list --id-only "$DEVICE_NAME" 2>/dev/null || true)
if [ ${#IDS[@]} -eq 0 ]; then
    err "No input device named \"$DEVICE_NAME\"."
    err "Is the dongle plugged in? Check the exact name with: xinput list"
    err "Override with: DEVICE_NAME='...' $0"
    exit 1
fi

show() {
    local id="$1"
    local current
    current=$(xinput list-props "$id" | sed -n "s/.*$PROP ([0-9]*):[[:space:]]*//p")
    local speed
    speed=$(xinput list-props "$id" | sed -n "s/.*libinput Accel Speed ([0-9]*):[[:space:]]*//p")
    case "$current" in
        "$FLAT") echo "  id $id: profile=flat (adaptive off), speed=${speed:-n/a}" ;;
        "") echo "  id $id: no libinput acceleration property (not a pointer?)" ;;
        *) echo "  id $id: profile=adaptive, speed=${speed:-n/a}  <- firmware curve is being doubled" ;;
    esac
}

prop_default() { # $1 = device id, $2 = property name; read libinput's own default
    xinput list-props "$1" | sed -n "s/.*$2 Default ([0-9]*):[[:space:]]*//p"
}

case "${1:-}" in
    --status)
        info "Current state of \"$DEVICE_NAME\":"
        for id in "${IDS[@]}"; do show "$id"; done
        [ -f "$XORG_RULE" ] && ok "Persistent rule installed: $XORG_RULE" \
            || info "No persistent rule; run with --persist to survive replug/logout."
        exit 0
        ;;
    --revert)
        for id in "${IDS[@]}"; do
            xinput list-props "$id" | grep -q "$PROP" || continue
            # Restore what libinput itself reports as the default, rather than
            # assuming adaptive - the driver is the authority on its own defaults.
            profile=$(prop_default "$id" "$PROP")
            speed=$(prop_default "$id" "libinput Accel Speed")
            if [ -n "$profile" ]; then
                # shellcheck disable=SC2086 # deliberate word splitting: "1, 0" -> two args
                xinput set-prop "$id" "$PROP" ${profile//,/ }
            fi
            [ -n "$speed" ] && xinput set-prop "$id" "libinput Accel Speed" "$speed"
            ok "Reverted id $id to libinput defaults"
        done
        if [ -f "$XORG_RULE" ]; then
            info "Removing $XORG_RULE (needs sudo)"
            sudo rm -f "$XORG_RULE" && ok "Removed"
        fi
        info "Now:"
        for id in "${IDS[@]}"; do show "$id"; done
        warn "The firmware curve and libinput's now stack again - see the notes at"
        warn "the top of this script."
        exit 0
        ;;
    --persist | "") ;;
    *)
        err "Unknown argument: $1"
        sed -n '5,20p' "$0" | sed 's/^# \?//'
        exit 1
        ;;
esac

for id in "${IDS[@]}"; do
    if xinput list-props "$id" | grep -q "$PROP"; then
        xinput set-prop "$id" "$PROP" 0 1
        # Speed 0 is libinput's neutral point; anything else reintroduces host gain.
        xinput set-prop "$id" "libinput Accel Speed" 0 2>/dev/null || true
        ok "Applied to id $id"
    fi
done
info "Now:"
for id in "${IDS[@]}"; do show "$id"; done

if [ "${1:-}" = "--persist" ]; then
    info "Installing $XORG_RULE (needs sudo)"
    sudo tee "$XORG_RULE" >/dev/null <<EOF
# Managed by zmk-config/setup-pointer.sh
# The Totem's dongle applies its own acceleration curve in firmware; keep libinput
# from applying a second one on top.
Section "InputClass"
    Identifier  "ZMK TOTEM flat pointer"
    MatchProduct "$DEVICE_NAME"
    MatchDriver "libinput"
    Option "AccelProfile" "flat"
    Option "AccelSpeed" "0"
EndSection
EOF
    ok "Installed. Takes effect for new devices; already-applied above for this session."
fi
