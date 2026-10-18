PREFIX ?= /usr
DESTDIR ?=
LIBDIR = $(PREFIX)/lib

install:
	install -Dm755 libexec/aurora-initrd-root $(DESTDIR)$(LIBDIR)/aurora/aurora-initrd-root
	install -Dm755 libexec/aurora-firstboot $(DESTDIR)$(LIBDIR)/aurora/aurora-firstboot
	install -Dm644 vendor/omarchy-mac-encrypt $(DESTDIR)$(LIBDIR)/aurora/vendor/omarchy-mac-encrypt
	install -Dm644 vendor/provenance.json $(DESTDIR)$(PREFIX)/share/doc/aurora-firstboot/provenance.json
	install -Dm644 systemd/aurora-initrd-root.service $(DESTDIR)$(LIBDIR)/aurora/initrd/aurora-initrd-root.service
	install -Dm644 systemd/aurora-root-sysroot.conf $(DESTDIR)$(LIBDIR)/aurora/initrd/aurora-root-sysroot.conf
	install -Dm644 systemd/aurora-firstboot.service $(DESTDIR)$(LIBDIR)/systemd/system/aurora-firstboot.service
	install -Dm644 systemd/aurora-display-manager.conf $(DESTDIR)$(LIBDIR)/systemd/system/display-manager.service.d/aurora-firstboot.conf
	install -Dm644 systemd/aurora-getty.conf $(DESTDIR)$(LIBDIR)/systemd/system/getty@.service.d/aurora-firstboot.conf
	install -Dm755 dracut/97aurora-root/module-setup.sh $(DESTDIR)$(LIBDIR)/dracut/modules.d/97aurora-root/module-setup.sh
	install -Dm644 mkinitcpio/aurora-root $(DESTDIR)$(LIBDIR)/initcpio/install/aurora-root
