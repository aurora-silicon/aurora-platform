# Aurora first boot

Shared first-boot integration for Fedora Aurora Remix, Aurora ALARM and Aurora NixOS. This is distribution support code, separate from the native macOS installer. The native app records the encryption choice on the target EFI partition; Linux creates a separate owner account and password.

The image contract uses a Btrfs root subvolume named `root`, an ext4 boot filesystem, and the EFI partition handed off by Aura in the device tree. `/etc/aurora/image-layout` contains only version 1, the root filesystem UUID, the boot filesystem UUID and the root subvolume. Image production supplies it and copies it into every initrd; installing this package alone does not arm conversion. A fresh image also supplies `/usr/lib/aurora/image-format` (`aurora-btrfs-v1`) and `/var/lib/aurora/firstboot/pending`.

`aurora-initrd-root` adapts the captured conversion state machine to those identities and mounts. It verifies that all target filesystems belong to the handed-off EFI disk, records the root GPT identity before conversion, and retains the resumable phase journal. Every initrd includes the unlock service, so the normal root filesystem UUID remains valid after opening its LUKS container. The first-boot account wizard replaces the temporary key with the Linux password, verifies unlock, retires the temporary key copies and releases the login services. NixOS persists the account declaration and a private password-hash file outside the public Nix store.

The imported converter is byte-preserved under `vendor/`, with its exact content digest and source provenance. Aurora's wrapper and distro integrations are new work. Source checks and disposable native loop-device tests are separate from real initrd keyboard, reboot, interruption and hardware qualification. **No new distribution installation is qualified by this repository yet.**

The converter requires cryptsetup 2.8 or newer, matching the target Fedora 44 and current ALARM packages; the native conversion CI uses that target userspace. Older Ubuntu 24.04 cryptsetup rejects the bounded conversion arguments and is not a supported runtime.

Run unprivileged checks with `python3 -m unittest discover -s tests`. `tests/loop-conversion` requires root in a disposable native CI worker and is restricted to its own newly created loop-backed disk. It must not be run on a user's ordinary disk.


The privileged conversion test shares the worker's IPC namespace with its udev daemon. Device-mapper uses IPC semaphores for udev completion; a private container IPC namespace can leave `cryptsetup open` waiting after successful conversion. This affects the disposable container test environment; the installed initrd retains ordinary udev synchronization. See [Red Hat's documented container cryptsetup issue](https://docs.redhat.com/en/documentation/red_hat_openstack_platform/12/pdf/release_notes/Red_Hat_OpenStack_Platform-12-Release_Notes-en-US.pdf).
