PREFIX ?= /usr
DESTDIR ?=
LIBDIR = $(PREFIX)/lib
SYSCONFDIR ?= /etc
LOCALSTATEDIR ?= /var
SELINUX_DEVEL ?= /usr/share/selinux/devel/Makefile

install: install-common install-dracut install-mkinitcpio install-power install-charge-full-now install-gamemode install-secboot install-secboot-mkinitcpio

install-common:
	install -Dm755 libexec/aurora-initrd-root $(DESTDIR)$(LIBDIR)/aurora/aurora-initrd-root
	install -Dm755 libexec/aurora-firstboot $(DESTDIR)$(LIBDIR)/aurora/aurora-firstboot
	install -Dm755 libexec/aurora-firstboot-handoff $(DESTDIR)$(LIBDIR)/aurora/aurora-firstboot-handoff
	install -Dm755 libexec/aurora-password-hook $(DESTDIR)$(LIBDIR)/aurora/aurora-password-hook
	install -Dm755 libexec/aurora-growfs-needed $(DESTDIR)$(LIBDIR)/aurora/aurora-growfs-needed
	install -Dm755 images/harden-grub.py $(DESTDIR)$(LIBDIR)/aurora/aurora-harden-grub
	install -Dm644 vendor/omarchy-mac-encrypt $(DESTDIR)$(LIBDIR)/aurora/vendor/omarchy-mac-encrypt
	install -Dm644 vendor/provenance.json $(DESTDIR)$(PREFIX)/share/doc/aurora-firstboot/provenance.json
	install -Dm644 systemd/aurora-initrd-root.service $(DESTDIR)$(LIBDIR)/aurora/initrd/aurora-initrd-root.service
	install -Dm644 systemd/aurora-root-sysroot.conf $(DESTDIR)$(LIBDIR)/aurora/initrd/aurora-root-sysroot.conf
	install -Dm644 systemd/aurora-root-mapper.conf $(DESTDIR)$(LIBDIR)/aurora/initrd/aurora-root-mapper.conf
	install -Dm644 systemd/aurora-firstboot.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-firstboot.service
	install -Dm644 systemd/aurora-disk-rekey.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-disk-rekey.service
	install -Dm644 systemd/aurora-initrd-keymap.path $(DESTDIR)$(LIBDIR)/systemd/system/aurora-initrd-keymap.path
	install -Dm644 systemd/aurora-initrd-keymap.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-initrd-keymap.service
	install -Dm644 systemd/aurora-display-manager.conf $(DESTDIR)$(LIBDIR)/systemd/system/display-manager.service.d/aurora-firstboot.conf
	install -Dm644 systemd/aurora-getty.conf $(DESTDIR)$(LIBDIR)/systemd/system/getty@.service.d/aurora-firstboot.conf
	install -Dm644 systemd/aurora-plasma-setup.conf $(DESTDIR)$(LIBDIR)/systemd/system/plasma-setup.service.d/aurora-firstboot.conf
	install -Dm644 systemd/aurora-plymouth-quit.conf $(DESTDIR)$(LIBDIR)/systemd/system/plymouth-quit.service.d/aurora-firstboot.conf
	install -Dm644 systemd/aurora-getty.conf $(DESTDIR)$(LIBDIR)/systemd/system/serial-getty@.service.d/aurora-firstboot.conf
	install -Dm644 systemd/aurora-growfs-root.conf $(DESTDIR)$(LIBDIR)/systemd/system/systemd-growfs-root.service.d/aurora-growfs.conf
	install -Dm644 systemd/aurora-growfs.conf $(DESTDIR)$(LIBDIR)/systemd/system/systemd-growfs@.service.d/aurora-growfs.conf

install-power:
	install -Dm755 power/aurora-power-budget $(DESTDIR)$(PREFIX)/libexec/aurora/aurora-power-budget
	install -Dm644 power/aurora-power-budget.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-power-budget.service
	install -Dm755 power/aurora-power-modes $(DESTDIR)$(PREFIX)/libexec/aurora/aurora-power-modes
	install -Dm644 power/aurora-power-modes.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-power-modes.service
	install -Dm644 power/README.md $(DESTDIR)$(PREFIX)/share/doc/aurora-power-budget/README.md
	install -Dm644 power/DESIGN.md $(DESTDIR)$(PREFIX)/share/doc/aurora-power-budget/DESIGN.md

# Charge to Full Now (power/README.md): the command, the one-shot service, the
# boot-time restore and the polkit rule for the active local user.  Enable the
# restore unit (systemctl enable aurora-charge-full-now-restore.service).
install-charge-full-now:
	install -Dm755 power/aurora-charge-full-now $(DESTDIR)$(PREFIX)/bin/aurora-charge-full-now
	install -Dm644 power/aurora-charge-full-now.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-charge-full-now.service
	install -Dm644 power/aurora-charge-full-now-restore.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-charge-full-now-restore.service
	install -Dm644 'power/aurora-charge-full-now-schedule@.service' '$(DESTDIR)$(LIBDIR)/systemd/system/aurora-charge-full-now-schedule@.service'
	install -Dm644 power/50-aurora-charge-full-now.rules $(DESTDIR)$(PREFIX)/share/polkit-1/rules.d/50-aurora-charge-full-now.rules
	install -dm755 $(DESTDIR)$(LOCALSTATEDIR)/lib/aurora/charge-full-now

# Optional, not part of install: the GNOME Shell 50/51 quick settings toggle for
# aurora-charge-full-now (gnome/README.md).  Installed system wide it stays off
# until a user runs gnome-extensions enable charge-full-now@aurorasilicon.org.
GNOME_EXT = charge-full-now@aurorasilicon.org
install-gnome-charge-full-now:
	install -Dm644 gnome/$(GNOME_EXT)/metadata.json $(DESTDIR)$(PREFIX)/share/gnome-shell/extensions/$(GNOME_EXT)/metadata.json
	install -Dm644 gnome/$(GNOME_EXT)/extension.js $(DESTDIR)$(PREFIX)/share/gnome-shell/extensions/$(GNOME_EXT)/extension.js

# GameMode defaults (power/GAMEMODE.md): an administrator-level config that
# GameMode merges over its shipped one. Packages ship it as a config file.
install-gamemode:
	install -Dm644 power/gamemode.ini $(DESTDIR)$(SYSCONFDIR)/gamemode.ini
	install -Dm644 power/GAMEMODE.md $(DESTDIR)$(PREFIX)/share/doc/aurora-gamemode/GAMEMODE.md

# The secure boot path: aurora-secboot, its kernel-install plugin, the
# first-boot key adoption service and the default configuration (secboot/).
install-secboot:
	install -Dm755 secboot/aurora-secboot $(DESTDIR)$(PREFIX)/bin/aurora-secboot
	install -Dm755 secboot/92-aurora-secboot.install $(DESTDIR)$(LIBDIR)/kernel/install.d/92-aurora-secboot.install
	install -Dm644 secboot/aurora-secboot-adopt.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-secboot-adopt.service
	install -Dm644 secboot/secboot.conf $(DESTDIR)$(SYSCONFDIR)/aurora/secboot.conf
	install -Dm644 secboot/README.md $(DESTDIR)$(PREFIX)/share/doc/aurora-secboot/README.md
	install -Dm644 secboot/stage1-existing-install.md $(DESTDIR)$(PREFIX)/share/doc/aurora-secboot/stage1-existing-install.md
	install -dm700 $(DESTDIR)$(LOCALSTATEDIR)/lib/aurora/secboot

# Arch Linux ARM: build the UKI after mkinitcpio.
install-secboot-mkinitcpio:
	install -Dm755 secboot/aurora-secboot.initcpio-post $(DESTDIR)$(LIBDIR)/initcpio/post/aurora-secboot

install-dracut:
	install -Dm755 dracut/97aurora-root/module-setup.sh $(DESTDIR)$(LIBDIR)/dracut/modules.d/97aurora-root/module-setup.sh

install-mkinitcpio:
	install -Dm644 mkinitcpio/aurora-root $(DESTDIR)$(LIBDIR)/initcpio/install/aurora-root

# Fedora only: label the first-boot programs as ordinary executables so that
# systemd starts them in an unconfined service domain (see selinux/).
selinux/aurora_firstboot.pp: selinux/aurora_firstboot.te selinux/aurora_firstboot.fc
	$(MAKE) -C selinux -f $(SELINUX_DEVEL) aurora_firstboot.pp

install-selinux: selinux/aurora_firstboot.pp
	install -Dm644 selinux/aurora_firstboot.pp $(DESTDIR)$(PREFIX)/share/selinux/packages/targeted/aurora_firstboot.pp

# Fedora only: keep the secure boot keys away from confined services.
selinux/aurora_secboot.pp: selinux/aurora_secboot.te selinux/aurora_secboot.fc
	$(MAKE) -C selinux -f $(SELINUX_DEVEL) aurora_secboot.pp

install-selinux-secboot: selinux/aurora_secboot.pp
	install -Dm644 selinux/aurora_secboot.pp $(DESTDIR)$(PREFIX)/share/selinux/packages/targeted/aurora_secboot.pp
