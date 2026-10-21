#!/bin/bash
# SPDX-License-Identifier: MIT
check() { [[ -f /etc/aurora/image-layout ]] || return 1; return 0; }
depends() { echo 'systemd crypt btrfs'; }
install() {
  local binary
  for binary in bash cryptsetup btrfs blkid lsblk awk findmnt mount umount readlink stat modprobe \
    mkdir chmod dd date cat mv sync install grep sed kill sleep tr; do
    inst_binary "$(command -v "$binary")" || return 1
  done
  inst_binary /usr/bin/systemd-ask-password
  inst_simple /etc/aurora/image-layout
  inst_script /usr/lib/aurora/aurora-initrd-root
  inst_simple /usr/lib/aurora/vendor/omarchy-mac-encrypt
  inst_simple /usr/lib/aurora/initrd/aurora-initrd-root.service "$systemdsystemunitdir/aurora-initrd-root.service"
  mkdir -p "$initdir$systemdsystemunitdir/initrd.target.wants" "$initdir$systemdsystemunitdir/sysroot.mount.d" "$initdir$systemdsystemunitdir/dev-mapper-root.device.d"
  ln_r "$systemdsystemunitdir/aurora-initrd-root.service" "$systemdsystemunitdir/initrd.target.wants/aurora-initrd-root.service"
  inst_simple /usr/lib/aurora/initrd/aurora-root-sysroot.conf "$systemdsystemunitdir/sysroot.mount.d/aurora-root.conf"
  inst_simple /usr/lib/aurora/initrd/aurora-root-mapper.conf "$systemdsystemunitdir/dev-mapper-root.device.d/aurora-root.conf"
}
installkernel() { instmods apple_wdt dm-crypt btrfs vfat ext4; }
