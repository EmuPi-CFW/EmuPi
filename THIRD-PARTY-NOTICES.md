# Third-party software

The MIT license in `LICENSE` covers only EmuPi's own files: `build.sh`,
`chroot-setup.sh`, everything under `rootfs/` (launcher, scripts, systemd units,
default configs) and the documentation.

EmuPi OS images also contain software that is NOT part of EmuPi and keeps its own
license. It is installed from the Raspberry Pi OS / Debian repositories at build time,
and its source is available from those repositories.

| Component | License (typical) |
|---|---|
| Raspberry Pi OS Lite (base system, Linux kernel, systemd, etc.) | GPL, LGPL and others, per package |
| RetroArch | GPL-3.0 |
| Nestopia libretro core | GPL-2.0 |
| FCEUmm libretro core | GPL-2.0 |
| mGBA libretro core | MPL-2.0 |
| libretro-core-info | MIT |
| Python 3 | PSF License |
| pygame / SDL2 | LGPL-2.1 / zlib |
| Roboto font | Apache-2.0 |

Exact licenses depend on the package version; check `/usr/share/doc/<package>/copyright`
on the device. Optional cores you add yourself (e.g. gpSP) keep their own licenses.

EmuPi does not include any games, ROMs or BIOS files. You are responsible for only
using ROMs you have the legal right to use.
