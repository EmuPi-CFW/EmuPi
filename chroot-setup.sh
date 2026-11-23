#!/bin/bash
# Runs INSIDE the image (via qemu-user chroot). Installs packages and wires up services.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive LC_ALL=C

echo "[chroot] apt update"
apt-get update

echo "[chroot] core packages"
apt-get install -y --no-install-recommends \
    retroarch libretro-core-info libretro-nestopia libretro-mgba \
    python3-pygame libgl1-mesa-dri libgles2 libegl1 libgbm1 \
    exfatprogs alsa-utils sudo fonts-roboto fdisk psmisc

# Optional extras: lighter NES core, preferred if available.
for p in libretro-fceumm; do
    apt-get install -y --no-install-recommends "$p" || echo "[chroot] optional $p not available, skipping"
done

echo "[chroot] user"
if ! getent passwd 1000 >/dev/null; then
    useradd -M -u 1000 -s /bin/bash "$EMUPI_USER"
fi
USER_NAME=$(getent passwd 1000 | cut -d: -f1)
echo "$USER_NAME:$EMUPI_PASS" | chpasswd
for g in video audio input render plugdev netdev dialout sudo; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$USER_NAME" || true
done
mkdir -p /home/emupi
cp -rn /etc/skel/. /home/emupi/ 2>/dev/null || true
chown -R 1000:1000 /home/emupi
chmod 0440 /etc/sudoers.d/emupi
visudo -cf /etc/sudoers.d/emupi

echo "[chroot] hostname"
echo "$EMUPI_HOSTNAME" > /etc/hostname
sed -i "s/127\.0\.1\.1.*/127.0.1.1\t$EMUPI_HOSTNAME/" /etc/hosts

echo "[chroot] services"
systemctl enable emupi.service emupi-firstboot.service emupi-shutdown-log.service emupi-diag.service emupi-heartbeat.service
systemctl set-default multi-user.target
# Raspberry Pi OS first-run helpers that can take over tty1, rename users, or reboot/halt
# the machine. EmuPi does its own first-boot work, so keep these out of the way.
for s in userconfig.service regenerate_ssh_host_keys.service resize2fs_once.service \
         rpi-resize-rootfs.service sshswitch.service piwiz.service; do
    systemctl disable "$s" 2>/dev/null || true
    systemctl mask "$s" 2>/dev/null || true
done
rm -f /etc/xdg/autostart/piwiz.desktop

# --- No "create a new user" wizard -------------------------------------------
# Raspberry Pi OS (Bookworm+) asks for a new username/password on first boot when it
# finds no user. EmuPi already ships the user (emupi, UID 1000, password from EMUPI_PASS),
# so remove every piece of that wizard instead of only masking it.
apt-get purge -y userconf-pi rpi-first-boot-wizard cloud-init 2>/dev/null || true
for s in userconfig.service rpi-first-boot-wizard.service cloud-init.service cloud-init-local.service \
         cloud-config.service cloud-final.service cloud-init.target; do
    systemctl disable "$s" 2>/dev/null || true
    rm -f "/etc/systemd/system/$s"
    ln -sf /dev/null "/etc/systemd/system/$s"      # manual mask, works even if systemctl can't
done
rm -f /etc/systemd/system/multi-user.target.wants/userconfig.service \
      /etc/systemd/system/multi-user.target.wants/rpi-first-boot-wizard.service
rm -rf /usr/lib/userconf-pi /usr/share/userconf-pi
mkdir -p /etc/cloud && touch /etc/cloud/cloud-init.disabled
# The stock image boots through init=.../firstboot (rename/user wizard). build.sh strips it
# from cmdline.txt; make sure the script itself can never prompt either.
if [ -e /usr/lib/raspberrypi-sys-mods/firstboot ]; then
    printf '#!/bin/sh\nexit 0\n' > /usr/lib/raspberrypi-sys-mods/firstboot
    chmod +x /usr/lib/raspberrypi-sys-mods/firstboot
fi
# Make sure the account is complete: no password-expiry / "must change password" prompt.
chage -I -1 -m 0 -M 99999 -E -1 -d "$(date +%F)" "$USER_NAME" 2>/dev/null || true

# Fallback auto-login on tty1 (used only if emupi.service isn't holding the console).
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<AUTOLOGIN
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $USER_NAME --noclear %I \$TERM
AUTOLOGIN
# Optional overrides for the session (e.g. SDL_VIDEODRIVER=...)
cat > /etc/default/emupi <<'DEF'
# EmuPi OS session overrides. Example: SDL_VIDEODRIVER=kmsdrm
DEF
if [ "${EMUPI_SSH:-1}" = "1" ]; then systemctl enable ssh; fi
for s in bluetooth hciuart triggerhappy ModemManager apt-daily.timer apt-daily-upgrade.timer \
         man-db.timer e2scrub_all.timer NetworkManager-wait-online systemd-timesyncd-wait; do
    systemctl disable "$s" 2>/dev/null || true
done
rm -f /etc/ssh/ssh_host_*
mkdir -p /etc/ssh/sshd_config.d
printf 'HostKey /etc/ssh/ssh_host_ed25519_key\n' > /etc/ssh/sshd_config.d/emupi.conf
# Services that only slow a Pi Zero's boot (swapfile creation, NFS, cron, fs scrub...).
for s in dphys-swapfile.service rpcbind.service rpcbind.socket rpc-statd-notify.service \
         nfs-client.target cron.service e2scrub_reap.service apt-daily.service \
         apt-daily-upgrade.service man-db.service; do
    systemctl disable "$s" 2>/dev/null || true
    systemctl mask "$s" 2>/dev/null || true
done

# Debug build: keep the journal on disk so a crash/poweroff can be inspected after the fact.
# (Set EMUPI_DEBUG_LOGS=0 at build time to keep logs in RAM and spare the SD card.)
mkdir -p /etc/systemd/journald.conf.d /var/log/journal
if [ "${EMUPI_DEBUG_LOGS:-1}" = "1" ]; then
    printf '[Journal]\nStorage=persistent\nSystemMaxUse=32M\nRuntimeMaxUse=16M\nSyncIntervalSec=5s\n' > /etc/systemd/journald.conf.d/emupi.conf
else
    printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n' > /etc/systemd/journald.conf.d/emupi.conf
fi

# Let the emupi user write to the boot partition's emupi-logs/ folder (FAT has no chmod).
if grep -q '/boot/firmware' /etc/fstab && ! grep '/boot/firmware' /etc/fstab | grep -q 'uid=1000'; then
    sed -i '\#/boot/firmware#s/defaults/defaults,uid=1000,gid=1000,umask=0022/' /etc/fstab
fi
echo "[chroot] fstab:"; cat /etc/fstab

mkdir -p /mnt/roms
grep -q 'LABEL=EMUROMS' /etc/fstab || \
  echo 'LABEL=EMUROMS /mnt/roms exfat noatime,nofail,uid=1000,gid=1000,umask=0002,x-systemd.device-timeout=10s 0 0' >> /etc/fstab

echo "EmuPi OS $EMUPI_VERSION" > /etc/emupi-release

echo "[chroot] cores found:"
find /usr/lib -name '*_libretro.so' | sed 's/^/  /'

apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache
