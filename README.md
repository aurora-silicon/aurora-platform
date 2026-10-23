# Aurora first boot

Shared first-boot integration for Fedora Aurora Remix, Aurora ALARM and Aurora NixOS. This is distribution support code, separate from the native macOS installer. The native app records the encryption choice on the target EFI partition; Linux creates a separate owner account and password.

The image contract uses a Btrfs root subvolume named `root`, an ext4 boot filesystem, and the EFI partition handed off by Aura in the device tree. `/etc/aurora/image-layout` contains only version 1, the root filesystem UUID, the boot filesystem UUID and the root subvolume. Image production supplies it and copies it into every initrd; installing this package alone does not arm conversion. A fresh image also supplies `/usr/lib/aurora/image-format` (`aurora-btrfs-v1`) and `/var/lib/aurora/firstboot/pending`.

`aurora-initrd-root` adapts the captured conversion state machine to those identities and mounts. It verifies that all target filesystems belong to the handed-off EFI disk, records the root GPT identity before conversion, and retains the resumable phase journal. Every initrd includes the unlock service, so the normal root filesystem UUID remains valid after opening its LUKS container. The first-boot account wizard replaces the temporary key with the Linux password, verifies unlock, retires the temporary key copies and releases the login services. NixOS persists the account declaration and a private password-hash file outside the public Nix store.

## Console, splash and recovery

`aurora-firstboot` owns tty1 for the text wizard. It asks Plymouth to quit with a time bound and stops a splash that does not answer, so the wizard can never stay hidden behind it; the console is cleared, and systemd status lines and kernel messages are muted while the prompts are on screen. Input mistakes are asked again in place. Any other failure shows its cause and offers a retry in the same boot; the owner and encryption journals make every attempt resumable, including an account created without its password. The disk is rekeyed before the account password is set, so the two never diverge. While setup is pending the greeter and login prompts are skipped rather than failed.

The initrd wrapper keeps the converter byte-preserved and overrides only its presentation: the splash message carries the percentage (`Encrypting your drive... 37%`), because many Plymouth themes ignore system-update progress, and the message is replaced, not stacked, ending with `Your drive is encrypted. Starting Aurora...`.

## SELinux (Fedora)

`/usr/lib` is `lib_t` in Fedora's policy, and systemd runs a `lib_t` program in its own `init_t` domain, where tools such as `chpasswd` are denied. `selinux/aurora_firstboot.fc` labels `/usr/lib/aurora/aurora-*` as `bin_t`, so systemd starts these programs in `unconfined_service_t`. Fedora builds and loads this module with `make install-selinux`; the wizard restores the policy label of every file it creates. `tests/selinux-policy` loads the module into real Fedora targeted policy and checks the labels and domain rules.

## GRUB

U-Boot exposes every NVMe namespace of the internal disk, and probing the extra ones can wedge it. GRUB's `search` probes every filesystem on every disk once its hints miss. `images/harden-grub.py` (installed as `/usr/lib/aurora/aurora-harden-grub`) rewrites the EFI stub so it finds the boot filesystem only on the disk GRUB started from, with a UUID-named marker file on Fedora and `probe` in Aurora's Arch core image, before a single exact-hint lookup fills GRUB's cache. The main configuration's lookups name the same disk, and its EFI partition is the one GRUB was loaded from. A missing boot filesystem stops with a message instead of a scan. `finalize-image` applies it to images; Fedora's package reapplies it after GRUB updates.

The imported converter is byte-preserved under `vendor/`, with its exact content digest and source provenance. Aurora's wrapper and distro integrations are new work. Source checks and disposable native loop-device tests are separate from real initrd keyboard, reboot, interruption and hardware qualification. **No new distribution installation is qualified by this repository yet.**

The converter requires cryptsetup 2.8 or newer, matching the target Fedora 44 and current ALARM packages; the native conversion CI uses that target userspace. Older Ubuntu 24.04 cryptsetup rejects the bounded conversion arguments and is not a supported runtime.

Run unprivileged checks with `python3 -m unittest discover -s tests`. `tests/loop-conversion` requires root in a disposable native CI worker and is restricted to its own newly created loop-backed disk. It must not be run on a user's ordinary disk.


The privileged conversion test shares the worker's IPC namespace with its udev daemon. Device-mapper uses IPC semaphores for udev completion; a private container IPC namespace can leave `cryptsetup open` waiting after successful conversion. This affects the disposable container test environment; the installed initrd retains ordinary udev synchronization. See [Red Hat's documented container cryptsetup issue](https://docs.redhat.com/en/documentation/red_hat_openstack_platform/12/pdf/release_notes/Red_Hat_OpenStack_Platform-12-Release_Notes-en-US.pdf).
