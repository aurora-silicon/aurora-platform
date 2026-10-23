PREFIX ?= /usr
DESTDIR ?=
LIBDIR = $(PREFIX)/lib
SELINUX_DEVEL ?= /usr/share/selinux/devel/Makefile

install: install-common install-dracut install-mkinitcpio

install-common:
	install -Dm755 libexec/aurora-initrd-root $(DESTDIR)$(LIBDIR)/aurora/aurora-initrd-root
	install -Dm755 libexec/aurora-firstboot $(DESTDIR)$(LIBDIR)/aurora/aurora-firstboot
	install -Dm755 libexec/aurora-firstboot-handoff $(DESTDIR)$(LIBDIR)/aurora/aurora-firstboot-handoff
	install -Dm755 images/harden-grub.py $(DESTDIR)$(LIBDIR)/aurora/aurora-harden-grub
	install -Dm644 vendor/omarchy-mac-encrypt $(DESTDIR)$(LIBDIR)/aurora/vendor/omarchy-mac-encrypt
	install -Dm644 vendor/provenance.json $(DESTDIR)$(PREFIX)/share/doc/aurora-firstboot/provenance.json
	install -Dm644 systemd/aurora-initrd-root.service $(DESTDIR)$(LIBDIR)/aurora/initrd/aurora-initrd-root.service
	install -Dm644 systemd/aurora-root-sysroot.conf $(DESTDIR)$(LIBDIR)/aurora/initrd/aurora-root-sysroot.conf
	install -Dm644 systemd/aurora-root-mapper.conf $(DESTDIR)$(LIBDIR)/aurora/initrd/aurora-root-mapper.conf
	install -Dm644 systemd/aurora-firstboot.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-firstboot.service
	install -Dm644 systemd/aurora-display-manager.conf $(DESTDIR)$(LIBDIR)/systemd/system/display-manager.service.d/aurora-firstboot.conf
	install -Dm644 systemd/aurora-getty.conf $(DESTDIR)$(LIBDIR)/systemd/system/getty@.service.d/aurora-firstboot.conf
	install -Dm644 systemd/aurora-plasma-setup.conf $(DESTDIR)$(LIBDIR)/systemd/system/plasma-setup.service.d/aurora-firstboot.conf
	install -Dm644 systemd/aurora-getty.conf $(DESTDIR)$(LIBDIR)/systemd/system/serial-getty@.service.d/aurora-firstboot.conf

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
