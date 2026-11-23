#!/usr/bin/env bash
# EmuPi OS image builder. Run on Ubuntu/Debian:  ./build.sh
# Output: out/EmuPi-OS-<version>.img (+ .img.xz)
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# ---- settings (override with environment variables) ------------------------
EMUPI_VERSION="${EMUPI_VERSION:-1.0.0}"
BASE_URL="${BASE_URL:-https://downloads.raspberrypi.com/raspios_lite_armhf/images/raspios_lite_armhf-2024-11-19/2024-11-19-raspios-bookworm-armhf-lite.img.xz}"
BASE_IMG="${BASE_IMG:-}"            # path to a local .img/.img.xz to use instead of downloading
ROOT_EXTRA_MB="${ROOT_EXTRA_MB:-1024}"  # extra space added to the root filesystem
ROMS_MB="${ROMS_MB:-512}"           # initial exFAT size; grows to fill the card on first boot
EMUPI_USER="${EMUPI_USER:-emupi}"
EMUPI_PASS="${EMUPI_PASS:-emupi}"
EMUPI_HOSTNAME="${EMUPI_HOSTNAME:-emupi}"
EMUPI_SSH="${EMUPI_SSH:-1}"
VIDEO_MODE="${VIDEO_MODE:-1280x720@60}"
COMPRESS="${COMPRESS:-1}"
EMUPI_DEBUG_LOGS="${EMUPI_DEBUG_LOGS:-1}"  # 1 = persistent journal + extra diagnostics
SHOW_CONSOLE="${SHOW_CONSOLE:-1}"   # 1 = show kernel/systemd boot messages, 0 = quiet boot

log() { echo -e "\033[1;35m[EmuPi]\033[0m $*"; }
die() { echo -e "\033[1;31m[EmuPi] ERROR:\033[0m $*" >&2; exit 1; }

if [[ $EUID -ne 0 ]]; then exec sudo -E "$0" "$@"; fi

WORK="$PWD/work"; OUT="$PWD/out"; CACHE="$PWD/cache"
ROOT="$WORK/mnt"; IMG="$WORK/emupi.img"; LOOP=""
mkdir -p "$WORK" "$OUT" "$CACHE" "$ROOT"

cleanup() {
    set +e
    umount -R "$ROOT" 2>/dev/null || umount -Rl "$ROOT" 2>/dev/null
    [[ -n "$LOOP" ]] && losetup -d "$LOOP" 2>/dev/null
    [[ -f "$ROOT/etc/resolv.conf.emupi-bak" ]] && mv -f "$ROOT/etc/resolv.conf.emupi-bak" "$ROOT/etc/resolv.conf"
}
trap cleanup EXIT

# ---- 1. host dependencies ---------------------------------------------------
need=()
for c in losetup sfdisk parted e2fsck resize2fs mkfs.exfat xz curl rsync sha256sum; do
    command -v "$c" >/dev/null || need+=("$c")
done
if (( ${#need[@]} )); then
    log "Installing host tools (missing: ${need[*]})"
    apt-get update
    apt-get install -y xz-utils curl rsync e2fsprogs exfatprogs fdisk parted dosfstools util-linux coreutils
fi
if ! command -v qemu-arm-static >/dev/null && [[ ! -e /proc/sys/fs/binfmt_misc/qemu-arm ]]; then
    log "Installing qemu user emulation"
    apt-get update
    # Older Ubuntu/Debian: qemu-user-static. Newer (25.04+/26.04): qemu-user-binfmt.
    apt-get install -y qemu-user-static binfmt-support 2>/dev/null \
        || apt-get install -y qemu-user-binfmt qemu-user binfmt-support 2>/dev/null \
        || apt-get install -y qemu-user-binfmt qemu-user \
        || die "could not install qemu user emulation packages"
fi
# binfmt_misc must be mounted and have an ARM handler (WSL2 often needs this by hand)
if [[ ! -d /proc/sys/fs/binfmt_misc/ || -z "$(ls -A /proc/sys/fs/binfmt_misc 2>/dev/null)" ]]; then
    mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc none /proc/sys/fs/binfmt_misc 2>/dev/null || true
fi
if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-arm ]]; then
    update-binfmts --enable qemu-arm 2>/dev/null \
        || systemctl restart systemd-binfmt 2>/dev/null || true
fi
[[ -e /proc/sys/fs/binfmt_misc/qemu-arm ]] \
    || die "no qemu-arm binfmt handler registered. On WSL2 try: sudo mount -t binfmt_misc none /proc/sys/fs/binfmt_misc && sudo systemctl restart systemd-binfmt (or 'wsl --shutdown' and retry)"

# ---- 2. base image -----------------------------------------------------------
if [[ -z "$BASE_IMG" ]]; then
    BASE_IMG="$CACHE/$(basename "$BASE_URL")"
    if [[ ! -f "$BASE_IMG" ]]; then
        log "Downloading Raspberry Pi OS Lite (armhf)"
        curl -fL --retry 3 -C - -o "$BASE_IMG.part" "$BASE_URL" && mv "$BASE_IMG.part" "$BASE_IMG"
        if curl -fsL -o "$BASE_IMG.sha256" "$BASE_URL.sha256"; then
            (cd "$CACHE" && sha256sum -c "$(basename "$BASE_IMG").sha256") || die "checksum mismatch"
        else
            log "No checksum published for this URL; skipping verification"
        fi
    fi
fi
log "Preparing base image"
rm -f "$IMG"
case "$BASE_IMG" in
    *.xz)  xz -dc "$BASE_IMG" > "$IMG" ;;
    *.img) cp --sparse=always "$BASE_IMG" "$IMG" ;;
    *)     die "BASE_IMG must be .img or .img.xz" ;;
esac

# ---- 3. partitions: grow root, add exFAT ROM partition ---------------------
part_field() { parted -sm "$IMG" unit s print | awk -F: -v n="$1" -v f="$2" '$1==n{gsub("s","",$f); print $f}'; }
P2_START=$(part_field 2 2); P2_SIZE=$(part_field 2 4)
NEW_P2_SIZE=$(( P2_SIZE + ROOT_EXTRA_MB * 2048 ))
P3_START=$(( ( (P2_START + NEW_P2_SIZE + 8191) / 8192 ) * 8192 ))
TOTAL_SECTORS=$(( P3_START + ROMS_MB * 2048 ))
log "Resizing image (root +${ROOT_EXTRA_MB}MB, ROM partition ${ROMS_MB}MB)"
truncate -s $(( TOTAL_SECTORS * 512 )) "$IMG"
echo "$P2_START, $NEW_P2_SIZE" | sfdisk --no-reread --no-tell-kernel -N 2 "$IMG" >/dev/null
echo "$P3_START, , 7"          | sfdisk --no-reread --no-tell-kernel --append "$IMG" >/dev/null

LOOP=$(losetup -fP --show "$IMG")
for _ in $(seq 1 20); do [[ -b ${LOOP}p3 ]] && break; udevadm settle 2>/dev/null || true; sleep 0.3; done
[[ -b ${LOOP}p3 ]] || die "loop partitions did not appear (${LOOP}p3)"

e2fsck -fy "${LOOP}p2" || [[ $? -le 1 ]]
resize2fs "${LOOP}p2"
mkfs.exfat -L EMUROMS "${LOOP}p3" >/dev/null

# ---- 4. mount and install overlay ------------------------------------------
mount "${LOOP}p2" "$ROOT"
mount "${LOOP}p1" "$ROOT/boot/firmware"
log "Installing EmuPi files"
rsync -a --chown=root:root rootfs/ "$ROOT/"
chmod 0440 "$ROOT/etc/sudoers.d/emupi"
chmod +x "$ROOT"/opt/emupi/bin/* "$ROOT"/opt/emupi/launcher/launcher.py

# ---- 5. boot partition tweaks ------------------------------------------------
BOOTP="$ROOT/boot/firmware"
cmd=$(tr -d '\n' < "$BOOTP/cmdline.txt")
# Also drop anything that would run Raspberry Pi's first-run user wizard / Imager first-run script.
cmd=$(sed -E 's# init=[^ ]+##; s# systemd\.(run|run_success_action|unit)=[^ ]+##g; s# (console=[^ ]+|quiet|splash|loglevel=[0-9]|plymouth\.ignore-serial-consoles)##g' <<<" $cmd")
if [[ "$SHOW_CONSOLE" == "1" ]]; then
    cmd="$cmd console=tty1 loglevel=6 consoleblank=0"
else
    cmd="$cmd quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0"
fi
[[ -n "$VIDEO_MODE" ]] && cmd="$cmd video=HDMI-A-1:${VIDEO_MODE}"
echo "${cmd# }" > "$BOOTP/cmdline.txt"

grep -q '^dtoverlay=vc4-kms-v3d' "$BOOTP/config.txt" || printf '\ndtoverlay=vc4-kms-v3d\nmax_framebuffers=2\n' >> "$BOOTP/config.txt"
cat >> "$BOOTP/config.txt" <<'CFG'

# --- EmuPi OS ---
disable_splash=1
boot_delay=0
hdmi_force_hotplug=1
[pi0w]
dtoverlay=disable-bt
[all]
CFG
mkdir -p "$BOOTP/emupi-logs"
cat > "$BOOTP/emupi-logs/README.txt" <<'LOGREADME'
EmuPi OS writes its logs here (readable on Windows/macOS by plugging in the SD card).
  heartbeat.log         1 line/2 s for 10 min (+display.log, dmesg-live.txt): power flags, voltage, temp. LAST LINE = last moment alive
  boot.log              one line per step: firstboot, services, session, power requests
  session.log           launcher/RetroArch session loop
  launcher.log          output of the last launcher run (video driver, errors, tracebacks)
  retroarch.log         output of the last RetroArch run
  diag-after-boot.txt   full system snapshot ~25 s after boot
  diag-launcher-failed.txt   snapshot taken when the launcher fails
  diag-before-poweroff.txt   snapshot taken before any shutdown EmuPi itself requests
  emupi-state.log      emupi.service state + pending jobs + tty1 owner, every ~20 s while booting
  service-startup.txt   one line each time emupi.service starts
  last-shutdown.txt     written when the system starts to shut down: shows what asked for it
LOGREADME
cat > "$BOOTP/emupi-wifi.txt.example" <<'WIFI'
# Rename to emupi-wifi.txt and fill in. Applied on next boot, then renamed to .applied
SSID=MyNetwork
PASS=MyPassword
COUNTRY=CA
WIFI

# ---- 6. ROM partition skeleton ---------------------------------------------
mkdir -p "$WORK/roms"
mount "${LOOP}p3" "$WORK/roms"
mkdir -p "$WORK/roms"/{nes,gba,saves,states,cores}
cat > "$WORK/roms/README.txt" <<'TXT'
EmuPi OS - put your games here:
  nes/   .nes (and .zip)
  gba/   .gba (and .zip)
Saves and save states are stored in saves/ and states/.
Optional: cores/ for extra libretro cores, emupi.json for controller button mapping.
TXT
sync; umount "$WORK/roms"

# ---- 7. chroot: install packages -------------------------------------------
log "Entering chroot (this is slow under emulation; be patient)"
if command -v qemu-arm-static >/dev/null; then cp "$(command -v qemu-arm-static)" "$ROOT/usr/bin/"; fi
[[ -e "$ROOT/etc/resolv.conf" || -L "$ROOT/etc/resolv.conf" ]] && mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.emupi-bak"
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$ROOT/etc/resolv.conf"
[[ -f "$ROOT/etc/ld.so.preload" ]] && mv "$ROOT/etc/ld.so.preload" "$ROOT/etc/ld.so.preload.emupi-bak"
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"; chmod +x "$ROOT/usr/sbin/policy-rc.d"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sys "$ROOT/sys"
mount --bind /dev "$ROOT/dev"
mount -t devpts devpts "$ROOT/dev/pts"
chroot "$ROOT" /bin/true || die "cannot run ARM binaries in the chroot; qemu binfmt is not working (see README)"
cp chroot-setup.sh "$ROOT/tmp/chroot-setup.sh"
chroot "$ROOT" env EMUPI_USER="$EMUPI_USER" EMUPI_PASS="$EMUPI_PASS" EMUPI_HOSTNAME="$EMUPI_HOSTNAME" \
    EMUPI_SSH="$EMUPI_SSH" EMUPI_VERSION="$EMUPI_VERSION" EMUPI_DEBUG_LOGS="$EMUPI_DEBUG_LOGS" /bin/bash /tmp/chroot-setup.sh

# ---- 8. cleanup -------------------------------------------------------------
log "Finalizing"
rm -f "$ROOT/usr/sbin/policy-rc.d" "$ROOT/usr/bin/qemu-arm-static" "$ROOT/tmp/chroot-setup.sh"
rm -f "$ROOT/etc/resolv.conf"; mv "$ROOT/etc/resolv.conf.emupi-bak" "$ROOT/etc/resolv.conf"
[[ -f "$ROOT/etc/ld.so.preload.emupi-bak" ]] && mv "$ROOT/etc/ld.so.preload.emupi-bak" "$ROOT/etc/ld.so.preload"
rm -f "$ROOT/root/.bash_history"
sync
umount -R "$ROOT"
e2fsck -fy "${LOOP}p2" || [[ $? -le 1 ]]
losetup -d "$LOOP"; LOOP=""

FINAL="$OUT/EmuPi-OS-$EMUPI_VERSION.img"
mv "$IMG" "$FINAL"
if [[ "$COMPRESS" == "1" ]]; then
    log "Compressing (xz)"
    xz -T0 -6 -kf "$FINAL"
fi
chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$OUT" "$CACHE" 2>/dev/null || true
log "Done: $FINAL"
log "Flash with Raspberry Pi Imager (Use custom), balenaEtcher, or: sudo dd if=$FINAL of=/dev/sdX bs=4M status=progress conv=fsync"
