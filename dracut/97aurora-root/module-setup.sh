#!/bin/bash
# SPDX-License-Identifier: MIT
check() { [[ -f /etc/aurora/image-layout ]] || return 1; return 0; }
depends() { echo 'systemd crypt btrfs'; }
install() {
  local binary unit layout=${AURORA_IMAGE_LAYOUT:-/etc/aurora/image-layout}
  for binary in bash cryptsetup btrfs blkid lsblk awk findmnt mount umount readlink stat modprobe \
    mkdir chmod dd date cat mv sync install grep sed kill sleep tr; do
    inst_binary "$(command -v "$binary")" || return 1
  done
  inst_binary /usr/bin/systemd-ask-password
  # The unlock prompt uses the keymap the i18n module copies from vconsole.conf.
  dracut_module_included i18n || dwarn 'aurora-root: without the i18n module the disk password prompt uses the US keymap'
  inst_simple "$layout" /etc/aurora/image-layout
  inst_script /usr/lib/aurora/aurora-initrd-root
  inst_simple /usr/lib/aurora/vendor/omarchy-mac-encrypt
  inst_simple /usr/lib/aurora/initrd/aurora-initrd-root.service "$systemdsystemunitdir/aurora-initrd-root.service"
  mkdir -p "$initdir$systemdsystemunitdir/initrd.target.wants" "$initdir$systemdsystemunitdir/sysroot.mount.d" "$initdir$systemdsystemunitdir/dev-mapper-root.device.d"
  ln_r "$systemdsystemunitdir/aurora-initrd-root.service" "$systemdsystemunitdir/initrd.target.wants/aurora-initrd-root.service"
  inst_simple /usr/lib/aurora/initrd/aurora-root-sysroot.conf "$systemdsystemunitdir/sysroot.mount.d/aurora-root.conf"
  inst_simple /usr/lib/aurora/initrd/aurora-root-mapper.conf "$systemdsystemunitdir/dev-mapper-root.device.d/aurora-root.conf"
  # root= names the Btrfs filesystem. Once the root is encrypted that device
  # appears only after aurora-initrd-root has unlocked it, so waiting for it
  # must outlast the passphrase prompt instead of failing after 90 seconds.
  while IFS= read -r unit; do
    mkdir -p "$initdir$systemdsystemunitdir/$unit.d"
    inst_simple /usr/lib/aurora/initrd/aurora-root-mapper.conf "$systemdsystemunitdir/$unit.d/aurora-root.conf"
  done < <("${AURORA_INITRD_ROOT:-/usr/lib/aurora/aurora-initrd-root}" --root-device-units "$layout")
}
installkernel() { instmods apple_wdt dm-crypt btrfs vfat ext4; }
