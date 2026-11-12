# aurora-secboot

`aurora-secboot` builds, signs and installs unified kernel images (UKIs) for the secure boot path, and reports how far the
boot chain is verified. It is shared by Fedora, ALARM and NixOS. Python 3 standard library only; it calls `ukify`
(systemd-ukify), `sbsign` and `sbverify` (sbsigntools) and `openssl`.

The default boot path does not change: U-Boot starts `EFI/BOOT/BOOTAA64.EFI`, which is shim (Fedora) or GRUB, and GRUB
starts the kernel. On the secure path `EFI/BOOT/BOOTAA64.EFI` is a signed UKI instead: kernel, initramfs and command line in
one PE file, signed with the db key that U-Boot's Secure Boot variables trust. GRUB's configuration, the initramfs and the
command line are then covered by the signature too. The command line inside the UKI always carries `lockdown=integrity` (or
`lockdown=confidentiality`).

Nothing refuses to boot because of this tool. It builds and signs UKIs and reports verdicts; enforcement comes from a U-Boot
built with Secure Boot variables (`aurora-secboot seed`) and from Stage 1.

## Configuration

`/etc/aurora/secboot.conf`, shell `KEY=value` lines, `#` comments:

| key | values | meaning |
|---|---|---|
| `KEY_OWNER` | `aurora` (default), `owner`, `both` | Who signs. Under `aurora` the machine holds no private key: Aurora signs what it ships, local UKIs stay unsigned and `enable` is refused. |
| `SECURE_PATH` | `no` (default), `yes` | `yes`: the newest verified UKI is `EFI/BOOT/BOOTAA64.EFI`. Set by `enable` and `disable`. |
| `LOCKDOWN` | `integrity` (default), `confidentiality` | The `lockdown=` value in every UKI. |
| `KEY_DIR` | `/var/lib/aurora/secboot/keys` | Owner keys. |
| `ESP` | empty | Empty: the ESP named by the device tree's `/chosen/asahi,efi-system-partition`, else `/boot/efi`. |
| `UKI_BUILD` | `auto` (default), `yes`, `no` | When the distribution hooks build a UKI: `auto` when `db.key` exists or the secure path is on. |

## Keys

`KEY_DIR` is root-only (0700). Private files are 0600, public ones 0644.

| file | what |
|---|---|
| `db.key`, `db.crt` | RSA-2048 UEFI db key and self-signed certificate (PEM). Signs UKIs. |
| `KEK.key`, `KEK.crt`, `PK.key`, `PK.crt` | RSA-2048 UEFI KEK and PK. Only used to build the U-Boot seed. |
| `owner.guid` | The signature owner GUID written into the seed's signature lists. |
| `stage2.key`, `stage2.pub` | Ed25519 Stage 2 key: 32-byte raw seed and 32-byte raw public key, the format of aura's `tools/secboot_sign.py keygen`. Stage 1's key block holds the public half. |

A private key on the machine means root can sign with it. `status` says so whenever one is present. On Fedora the
`aurora_secboot` SELinux module labels `/var/lib/aurora/secboot` as a security file type, so confined services cannot read the
keys.

When the owner chose their own keys at install, the installer writes `aurora/secboot/stage2.key`, `stage2.pub` and `owner`
(`KEY_OWNER=owner` or `both`) on the ESP. The ESP is not encrypted, so at first boot `aurora-secboot-adopt.service` runs
`aurora-secboot keys adopt`: it checks that the public key belongs to the seed, moves the key into `KEY_DIR` on the encrypted
root, records `KEY_OWNER`, and overwrites and unlinks the ESP copies. Until that first boot, anyone who can read the ESP can
read the Stage 2 key. Overwriting is best effort on flash storage.

## Commands

Exit status 0 on success, 1 when a request is refused (one sentence on stderr), 2 on usage errors. `--root DIR` puts every
path, including the ones given on the command line, under `DIR`.

- `status [--json]`: configuration, keys, the boot security record (`/sys/firmware/aurora/boot_security`), the EFI
  `SecureBoot`, `SetupMode`, `AuditMode` and `DeployedMode` variables, kernel lockdown, each UKI under `ESP/EFI/Linux/` and
  `EFI/BOOT/BOOTAA64.EFI` with its verdict against `db.crt` (`verified`, `unsigned` or `other-key`), and the first gap in the
  chain.
- `keys create [--force]`: make the UEFI PK, KEK and db keys, `owner.guid`, and the Stage 2 key if there is none. Refused under
  `KEY_OWNER=aurora`, and when UEFI keys exist unless `--force` is given. An existing Stage 2 key is never replaced: Stage 1
  trusts its public half.
- `keys adopt [--from PATH]`: see above. `PATH` is the directory or the `stage2.key` file; the default is
  `ESP/aurora/secboot/stage2.key`. Running it again does nothing. A key that does not match its public half, or that differs
  from the one already in `KEY_DIR`, is refused and left where it is.
- `cmdline [--lab]`: the secure-path command line. It starts from `/etc/kernel/cmdline` (else `/proc/cmdline` without
  `BOOT_IMAGE=` and `initrd=`), removes the tokens below with one warning each, and adds `lockdown=<LOCKDOWN>`.
- `uki build KVER [--image P] [--initrd P]... [--cmdline-file P] [--lab] [--output P] [--no-sign]`: `ukify build` with
  `/usr/lib/systemd/boot/efi/linuxaa64.efi.stub`, `--uname KVER` and `/etc/os-release`. No devicetree goes in the UKI: U-Boot
  passes the one it booted with. It signs with `db.key` when it exists, else leaves the UKI unsigned with a warning. The
  default output is `ESP/EFI/Linux/aurora-<machine-id>-<KVER>.efi`; it is written to a temporary file in the same directory,
  synced and renamed. The defaults for the image are `/usr/lib/modules/KVER/vmlinuz` and `/boot/vmlinuz-KVER`, and for the
  initramfs `/boot/initramfs-KVER.img`.
- `uki remove KVER`: remove that kernel's UKI.
- `uki verify [PATH...]`: `sbverify --cert db.crt` on each UKI (default: all of this machine's). Exit 1 if any fails.
- `enable`: refused under `KEY_OWNER=aurora` and when neither the running nor the newest kernel has a verified UKI. It keeps the
  current loader as `EFI/BOOT/BOOTAA64.EFI.aurora-shim` (once), copies the newest verified UKI to `EFI/BOOT/BOOTAA64.EFI` and
  sets `SECURE_PATH=yes`. Running it again refreshes `BOOTAA64.EFI` to the newest verified UKI.
- `disable`: puts the saved loader back and sets `SECURE_PATH=no`.
- `seed -o FILE`: the U-Boot variable seed (`CONFIG_EFI_VAR_SEED_FILE` with `CONFIG_EFI_VARIABLES_PRESEED`) holding PK, KEK
  and db from `KEY_DIR` and an empty dbx, for an owner-built U-Boot. Certificates from Microsoft's UEFI and Windows CAs are
  refused: they would admit every boot loader Microsoft has signed.

Command line tokens the secure path removes (names compare with `-` and `_` alike):
`systemd.debug_shell*`, `rd.systemd.debug_shell*`, `init=`, `rdinit=`, `rd.break*`, `rd.shell*`, `systemd.unit=`,
`rd.systemd.unit=`, `lockdown=`, `module.sig_enforce=`, `efi=`, `panic=`, `systemd.watchdog_sec=`, and the bare words
`single`, `emergency`, `rescue`, `s`, `S`, `1`, `-s` and `-b`. `--lab` keeps `panic=` and `systemd.watchdog_sec=` for test
machines; a UKI built with it must not ship.

## Distribution hooks

- `/usr/lib/kernel/install.d/92-aurora-secboot.install` (kernel-install, Fedora): on `add` it builds the kernel's UKI after the
  initramfs exists (from kernel-install's arguments, its staging area or `/boot/initramfs-KVER.img`) and, with
  `SECURE_PATH=yes`, refreshes `BOOTAA64.EFI`. On `remove` it removes the UKI.
- `/usr/lib/initcpio/post/aurora-secboot` (mkinitcpio, ALARM): the same as `add` for the image mkinitcpio made. The fallback
  image and images written outside `/boot` are skipped.
- NixOS writes the configuration from its module and runs `uki build` for each generation.

Both hooks do nothing until `/etc/aurora/secboot.conf` exists. With `UKI_BUILD=auto` and no owner key they build nothing. A
failed build only fails the hook while the secure path is on, so the default path never blocks a kernel update. Both call
`aurora-secboot hook add|remove`, which is meant for these hooks only.

## Tests

`python3 -m unittest discover -s tests` runs `tests/test_secboot.py` with fake `ukify`, `sbsign`, `sbverify` and `openssl` on
`PATH`. Where the real tools exist (a Fedora container or VM), `tests/secboot-roundtrip` makes keys, builds and signs a real
UKI from the installed kernel, verifies it with `sbverify` and checks that a UKI signed with another key fails.
