# ZMK Config for Totem Keyboard

ZMK firmware configuration for the Totem 38-key split keyboard, run in dongle mode
with a Ploopy Adept trackball as a third peripheral.

Four devices, all Seeeduino XIAO BLE:

| device | role | has battery |
|---|---|---|
| dongle | split central, USB HID to the host, ZMK Studio | no (USB powered) |
| left / right | split peripherals, key matrix | yes |
| trackball | split peripheral, PMW3610 sensor, no keys | yes |

The keymap and every input processor live on the **dongle** - it is the central.
The peripherals only ship key positions and sensor deltas.

## Layout

**Miryoku layout** with QWERTY base layer and **VI-style (hjkl)** navigation, mouse
and media layers.

### Base Layer (QWERTY)

```
Q     W     E     R     T  |  Y     U     I      O      P
GUI-A ALT-S CTRL-D SFT-F G  |  H  SFT-J CTRL-K RALT-L GUI-'
Z     X     C     V     B  |  N     M     ,      .      /
```

**Thumbs (layer-tap):**
```
MEDIA-ESC  NAV-SPACE  MOUSE-TAB  |  SYM-RET  NUM-BSPC  FUN-DEL
```

### Layers

- **BASE (0)**: QWERTY with home row mods (GACS order)
- **NAV (1)**: VI-style arrows on the home row, clipboard keys on the top row
- **MOUSE (2)**: mouse movement in hjkl positions, clicks on the right thumbs
- **MEDIA (3)**: Prev / Vol- / Vol+ / Next on the home row. `&bapp` on the inner
  top keys (T and Y) types the battery percentage of the half you press it on
- **NUM (4)**, **SYM (5)**, **FUN (6)**: number pad, shifted symbols, F-keys
- **BUTTON (7)**: auto-activated by trackball motion; clicks on the thumbs, `&mo 8`
  on the inner index keys, everything else `&trans`
- **SCROLL (8)**: same bindings; being on this layer switches the trackball from
  pointer to scroll

## Homerow Mods

- **Flavor**: `balanced`, **tapping term**: 280ms, **prior idle**: 150ms
- **Quick tap**: 250ms - tap a key then press it again within this window to get a
  held tap the OS auto-repeats (`a` then hold `a` -> `aaaaaaaa`). A *first* hold is
  always the modifier, so this is the only way to repeat a homerow key
- **Both hands**: opposite-hand trigger only (`hold-trigger-key-positions` +
  `hold-trigger-on-release`), so a same-hand *roll* resolves as a tap and typing
  "as" cannot produce `LGUI+s`

One-handed shortcuts still work. The positional filter only applies while the
hold-tap is undecided (`behavior_hold_tap.c:514` - it returns early when no other
key was pressed yet), so holding `D` past the 280ms tapping term resolves it to
`LCTRL` on the timer, and `Ctrl+C` on the left hand behaves normally from there.
What the filter prevents is a fast roll being mistaken for a shortcut.

## Pointer

Tuned for a single 3840x2160 27" display (163 PPI) on X11, where the pointer space
is unscaled - 1 HID count = 1 physical pixel.

- Sensor at **1600 CPI** (`totem_trackball.overlay`)
- Acceleration via [zmk-pointing-acceleration-alpha](https://github.com/nuovotaka/zmk-pointing-acceleration-alpha),
  configured on the **dongle** because input processors run on the central
- Gearing: ~800 px/in of ball travel when placing precisely, ~4480 px/in on a
  flick - the full screen width in about one sweep of the 34mm ball
- Reports are capped at **125 Hz** by `&zip_report_rate_limit 8` on the trackball,
  which accumulates deltas rather than dropping them
- Scrolling is high-resolution (`CONFIG_ZMK_POINTING_SMOOTH_SCROLLING`), inverted
  so rolling up scrolls up

Two traps worth knowing before changing any of it:

- `PRESET_CUSTOM` **must** stay selected, or the acceleration driver ignores every
  devicetree tuning property.
- `sensor-dpi` is deliberately `800` while the hardware runs at 1600. The driver
  normalizes as `sensitivity * 800 / sensor-dpi`, so declaring the real value would
  cancel the CPI increase exactly.

## Battery

Every part reports its level; the dongle collects them and exposes one HID battery
report per part over USB. Read them on the host:

```bash
./zmk-battery.py              # live levels + discharge rate per part
./zmk-battery.py --min        # worst part only, for a status bar
./zmk-battery.py --rate       # rates from the log alone, no dongle needed
./zmk-battery.py --help
```

It logs to `~/.local/state/zmk-battery.csv` (only when a level changes) and renders
`zmk-battery.png`. `99-zmk-hidraw.rules` grants access to `/dev/hidraw*` without
root. A polybar module at `~/.config/polybar/scripts/totem-battery.sh` calls it
once a minute and left-click opens the graph.

This needs the vendored PR below: stock ZMK publishes battery only over the BLE
Battery Service, which a USB dongle cannot use. Note Linux registers just the first
battery of a multi-battery HID device before kernel 7.1, so `upower` and
`/sys/class/power_supply` show one arbitrary part - use the script instead.

## Local ZMK patches

Two unmerged upstream PRs are vendored in `config/zephyr/patches/`, applied by
`west patch` from `config/zephyr/patches.yml`:

| patch | what it gives us | drop when |
|---|---|---|
| [#3458](https://github.com/zmkfirmware/zmk/pull/3458) | `CONFIG_ZMK_BATTERY_REPORTING_USB` - per-part battery over USB HID | merged upstream |
| [#3382](https://github.com/zmkfirmware/zmk/pull/3382) | `CONFIG_ZMK_BLE_DISABLE_HOST_ADV` - dongle stops advertising as a pairable BLE keyboard | merged upstream |

**Order matters**: both touch `app/Kconfig`, and #3382 was generated against the
tree with #3458 already applied. Keep it second in `patches.yml`.

Because of these, `config/west.yml` pins every project to a commit SHA. A floating
`main` would move upstream out from under the patches and `west patch apply` would
start failing. To upgrade: bump one SHA, run `./build_local.sh update`, check the
patches still apply, rebuild, flash.

## Building

### Locally (recommended)

Containerised, no host toolchain required:

```bash
./build_local.sh build                              # everything
./build_local.sh build xiao_ble_zmk_totem_dongle    # one target
./build_local.sh build xiao_ble_zmk_totem_dongle -i # incremental, much faster
./build_local.sh update                             # west update + reapply patches
./build_local.sh copy                               # build/ -> artifacts/
./build_local.sh help
```

`update` is idempotent: it cleans the patched module before reapplying, since
`west update` leaves an already-patched tree dirty.

### GitHub Actions - currently broken

`.github/workflows/build.yml` calls the upstream reusable workflow, which never runs
`west patch`. Its builds therefore lack both vendored PRs and now fail outright,
because `totem_dongle.overlay` includes a header that only exists in #3458. Fix by
forking the workflow and adding a patch step, or drop CI and build locally.
`download-firmware.sh` pulls artifacts from those runs, so it is affected too.

## Flashing

Takes a **build folder name** (tab completion: `source completions/flash-firmware.bash`):

```bash
./flash-firmware.sh xiao_ble_zmk_totem_dongle
./flash-firmware.sh xiao_ble_zmk_totem_left
./flash-firmware.sh xiao_ble_zmk_totem_right
./flash-firmware.sh xiao_ble_zmk_totem_trackball
./flash-firmware.sh xiao_ble_zmk_settings_reset
```

Double-tap reset to enter the bootloader; the script waits 10s for `XIAO-SENSE` to
mount, copies the uf2, and the device reboots.

### Which part to flash for which change

| you changed | flash |
|---|---|
| keymap, layers, combos, homerow mods | dongle only |
| input processors, acceleration, scroll, auto-mouse layer | dongle only |
| battery reporting, BLE advertising, Studio | dongle only |
| sensor CPI, report rate, PMW3610 power settings | trackball only |
| key matrix, per-half GPIO | that half |
| anything in `config/totem.conf` | every part it applies to |

The keymap lives on the central, so most day-to-day edits are a dongle-only flash.

## Configuration layering (read before adding a setting)

Kconfig fragments are merged in this order, and the **last** assignment wins:

```
zmk/app/prj.conf
  -> board conf
    -> shield dir: config/boards/shields/totem/<shield>.conf
      -> config/totem.conf          (shared, keyboard-wide)
        -> config/<shield>.conf     (per part, highest priority)
```

So a value in `config/totem.conf` silently overrides the same symbol set in the
shield directory. That bit us once: the trackball's 1h sleep timeout sat in
`boards/shields/totem/totem_trackball.conf` and was overwritten by the shared 30
minutes for months. **Per-part overrides belong in `config/<shield>.conf`.**

Current sleep behaviour: idle after 30s everywhere (this stops battery sampling, so
an untouched part's reported level goes stale), deep sleep after 30 min for the
halves and 1h for the trackball. The dongle never deep-sleeps - `activity.c` gates
that on `!is_usb_power_present()`.

## Host setup (Linux)

```bash
sudo cp 99-zmk-hidraw.rules /etc/udev/rules.d/     # battery reads without root
sudo udevadm control --reload-rules && sudo udevadm trigger

./setup-pointer.sh --persist                        # libinput flat profile
./setup-pointer.sh --status                         # inspect
./setup-pointer.sh --revert                         # undo
```

The pointer script matters: libinput's default *adaptive* profile applies a second
acceleration curve on top of the firmware's, so the two multiply and the tuning
above stops predicting what the hand feels. Flat means unit gain.

## Configuration Files

- `config/totem.keymap` - keymap, shared by all builds
- `config/totem.conf` - keyboard-wide Kconfig
- `config/totem_trackball.conf` - per-part overrides that must beat `totem.conf`
- `config/boards/shields/totem/` - shield definition, per-part overlays and confs
- `config/west.yml` - dependencies, pinned to SHAs
- `config/zephyr/patches.yml` - vendored upstream PRs
- `build.yaml` - build targets (used by both local and CI builds)

## Known issues

- **Right half drains ~1%/h while idle** and needs charging every couple of days,
  while the left half on identical firmware is flat. The fault follows the XIAO
  module, not the side - it was swapped between halves and the drain moved with it.
  That module has a history of a cold solder joint. Replacement pending.
- **CI cannot build this config** - see Building above.

## References

- [Miryoku layout](https://github.com/manna-harbour/miryoku)
- [urob's timeless homerow mods](https://github.com/urob/zmk-config)
- [ZMK Firmware](https://zmk.dev/)
