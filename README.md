# EmuPi

A tiny retro-gaming OS for the **Raspberry Pi Zero / Zero W**.

## Build (on Ubuntu/Debian, needs internet and ~12 GB free)

    sudo ./build.sh

Output: `out/EmuPi-OS-1.0.img` and `.img.xz`. Flash with Raspberry Pi Imager
(Use custom), balenaEtcher, or `dd`. Use an SD card of 8 GB or more.

The script downloads Raspberry Pi OS Lite (armhf, ARMv6 compatible), adds an exFAT
`EMUROMS` partition, enters the image with qemu, installs RetroArch, the NES and GBA
cores and the launcher, and sets up autostart.

Useful variables: `BASE_URL` / `BASE_IMG` (different base image), `ROOT_EXTRA_MB`,
`ROMS_MB`, `EMUPI_PASS`, `EMUPI_SSH=0`, `VIDEO_MODE=` (empty = auto HDMI mode),
`COMPRESS=0`.

## Using it

1. Flash and put the SD card in the Pi. The first boot takes a little longer: the
   `EMUROMS` partition grows to fill the card (only if it is still empty).
2. Put ROMs on the `EMUROMS` drive (it shows up on Windows/macOS/Linux):
   `nes/` for `.nes`, `gba/` for `.gba` (zips work too). Saves go to `saves/`, states to `states/`.
3. Controls: arrows or D-pad to move, Enter/A to select, Esc/B to go back.
   In a game: **Esc** or **Start+Select** on a gamepad quits to the launcher.
   F2/F4 save/load state, F1 opens the RetroArch menu.
4. Controller buttons differ between pads. Override in `EMUROMS/emupi.json`, e.g.
   `{"ok_buttons":[1],"back_buttons":[2]}` (also `pgup_buttons`, `pgdn_buttons`).

Wi-Fi (Zero W): rename `emupi-wifi.txt.example` on the boot partition to
`emupi-wifi.txt`, fill in SSID/PASS/COUNTRY. SSH login: `emupi` / `emupi`
(change it with `passwd`, or set `EMUPI_PASS` when building). Ctrl+Alt+F2 gives a console.

## Performance notes

The Zero is a single-core 1 GHz ARMv6. NES runs well. GBA uses mGBA, which plays many
games full speed but not all. For better GBA speed, build a gpSP libretro core for
ARMv6 and drop `gpsp_libretro.so` into `EMUROMS/cores/`; the launcher prefers it automatically.
Likewise a `fceumm_libretro.so` is preferred over Nestopia for NES.
A lower `VIDEO_MODE` (e.g. `640x480@60`) also helps.

## Troubleshooting

- **Logs are written to the SD card's boot partition**, folder `emupi-logs/` (plug the card into any PC):
  `boot.log` (timeline), `session.log`, `launcher.log`, `retroarch.log`, `diag-after-boot.txt`
  (full snapshot ~25 s after boot), `diag-launcher-failed.txt`, `diag-before-poweroff.txt`
  (only if EmuPi itself requested a shutdown) and `last-shutdown.txt` (what was happening when the
  system started going down). If the Pi "powers off", `boot.log` + `last-shutdown.txt` say who asked.
- On the Pi: `/tmp/emupi-launcher.log`, `/tmp/emupi-retroarch.log`, `journalctl -b -u emupi`.
- If the launcher cannot start, the error is printed on the screen and retried; EmuPi never powers off by itself.
- Black screen in a game: in `~/.config/retroarch/retroarch.cfg` try `video_driver = "sdl2"`.
- No sound: set `audio_device = "hw:1"` (see `aplay -l`). USB audio and HDMI are auto-detected.
- "No core installed" toast: check the build log's "cores found" list; package names can differ between Raspberry Pi OS releases.

## Layout

    build.sh            host-side image builder
    chroot-setup.sh     runs inside the image (packages, user, services)
    rootfs/             files copied into the image
      opt/emupi/launcher/launcher.py   the frontend (pygame, 640x360 canvas)
      opt/emupi/bin/emupi-session      launcher/RetroArch loop
      opt/emupi/bin/emupi-firstboot    partition grow, SSH keys, Wi-Fi file

## License

EmuPi's own code is released under the MIT License (see `LICENSE`). Bundled third-party
software (RetroArch, libretro cores, Raspberry Pi OS, etc.) keeps its own licenses; see
`THIRD-PARTY-NOTICES.md`. The license files are also installed on the image in
`/usr/share/doc/emupi/`.
