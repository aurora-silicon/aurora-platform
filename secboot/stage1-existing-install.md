# Owner-key Stage 1 on an existing install

This page is for an Aurora install that already boots. It replaces the install's Stage 1 (aura, the part
macOS's LocalPolicy pins) with one that checks Stage 2 against your own key. You do it twice: first in
audit mode, which checks, records and boots anyway, then in enforce mode, which refuses a Stage 2 that
isn't signed with your key.

New installs can get the same key block from aurora-installer when you pick owner keys there. This page
covers installs made before that.

## Status: read this first

- No Mac has run this procedure yet. The pieces are tested on their own: Stage 2 signature checks
  (audit, enforce, and a bad signature refused) on an M2 MacBook Air, the Stage 1 object format on our
  lab Macs, the padding byte for byte against aurora-installer's.
- An enforcing Stage 1 has never booted on any Mac.
- **Nothing has been tried on M4 Macs (T8132, such as the MacBook Air J713) or later.** Those Macs need
  a padded Stage 1 that loads `m1n1/boot-ctxr.bin`. The tool builds that, but no M4 has booted one
  with key checks.
- Each Linux install and macOS has its own LocalPolicy, so this changes only the install you enrol it
  for. If it goes wrong, macOS and your other installs still boot.

## What you need

- macOS still bootable, with an admin user. You enrol from that Mac's One True recoveryOS (1TR), which
  asks for the owner's password.
- The install's name as macOS shows it (`diskutil list` in macOS or 1TR), for example `Fedora`.
- `aurora-secboot` and an aura package that ships `/usr/share/aura/tools/` and
  `/usr/share/aura/stage1/aura-secure-raw.bin`. Until your distribution ships that, use an aura checkout
  (below).
- Room for the objects. On an M4 or later Mac each padded object is about 150 MB, and the ESP's FAT
  can't store the padding sparsely. The tool checks the space; `--output-dir` puts them on a FAT USB
  stick instead.
- A backup of the ESP (`/boot/efi`).

## 1. Make your keys

In `/etc/aurora/secboot.conf` set `KEY_OWNER=owner`, then:

```sh
sudo aurora-secboot keys create
```

This writes `/var/lib/aurora/secboot/keys/stage2.key` and `stage2.pub`. The private key sits on this
machine, inside your encrypted root. Root can sign with it, so a root compromise of this install still
wins. What this stops is changes made from outside it: another OS on the same disk, or someone with the
disk.

## 2. Get a secure-flavour Stage 1 image

With the packaged image, skip to step 3. Otherwise build one from aura (see aura's README for the
toolchain):

```sh
git clone https://github.com/aurora-silicon/aura && cd aura
make RELEASE=1 CHAINLOADING=1 SECBOOT=audit SECBOOT_FLAVOUR=secure \
     SECBOOT_PUBKEY=/var/lib/aurora/secboot/keys/stage2.pub
```

Use the same aura release your distribution's Stage 2 comes from (`rpm -q aura`, `pacman -Q aura`). Then
pass `--aura-dir $PWD --stage1-image $PWD/build/aura.bin` to the commands below, and
`--stock-image` with a stock Stage 1 image (or `--rollback` with the object enrolled now).

The `secure` flavour never gives a USB host aura's proxy. A `dev` image is refused unless you pass
`--allow-dev-flavour`.

## 3. Audit

Sign the current Stage 2, then build the audit Stage 1:

```sh
sudo aurora-secboot stage2 sign
sudo aurora-secboot stage1 build --audit --volume Fedora
```

The tool reads the ESP this boot came from and which Stage 2 file Stage 1 must load, puts your
`stage2.pub` in the Stage 1 key block (rollback floor 1), wraps and, on M4 or later, pads it. It writes,
in `/boot/efi/aurora/stage1/`:

- `stage1-audit.macho`, the object to enrol;
- `stage1-stock.macho`, a Stage 1 without key checks for this same install, to go back to;
- `enroll-stage1.sh` and `ENROLL-audit.txt` with the exact 1TR commands and the MD5s.

It prints the commands. **Write the MD5s down from this terminal.** Anything on the ESP can be changed
by another OS on the disk; the MD5 you copy here is how `enroll-stage1.sh` knows the files are yours. A
copy of the record also goes in `/var/lib/aurora/secboot/stage1/`, on the encrypted root.

Shut down, hold the power button until "Loading startup options", pick Options, then Utilities >
Terminal, and run the printed lines:

```sh
diskutil list
diskutil mount diskXsY          # the install's EFI partition
cd /Volumes/EFI/aurora/stage1    # the name diskutil mounted it as
bash enroll-stage1.sh --check ...   # the printed --check line: checks only
bash enroll-stage1.sh ...           # enrols, asks for "yes" and the owner's password
```

On M4 and later the lines carry `--compress`. Boot the install and check:

```sh
cat /sys/firmware/aurora/boot_security/stage1_flavour     # secure
sudo aurora-secboot status
```

`status` should show `Stage 1: policy audit, secure flavour, Stage 2 verified (key id ...)`. If it shows
`unsigned`, the Stage 2 changed after you signed it: run `sudo aurora-secboot stage2 sign` and reboot.
Keep it in audit through a few kernel and aura updates and check that each Stage 2 stays signed.

## Stage 2 updates

Package updates rebuild Stage 2 (`m1n1/boot.bin`, or `m1n1/boot-ctxr.bin` on M4 and later). With
`KEY_OWNER=owner` and `STAGE2_SIGN=auto`, an updater that supports it hands each new Stage 2 to
`aurora-secboot stage2 hook`, which signs it before it reaches the ESP. If signing fails the update stops
and the old Stage 2 stays. Updaters that do this install `/usr/share/aurora/stage2-sign-hook`. With an
older updater, run `sudo aurora-secboot stage2 sign` after every update; `sudo aurora-secboot stage2 verify`
checks.

## 4. Enforce

```sh
sudo aurora-secboot stage1 build --enforce --volume Fedora
```

It refuses unless all of these hold:

- this boot came from your audit Stage 1, and it recorded this boot's Stage 2 as verified with your key;
- the Stage 2 on the ESP (and an `aura-try` trial, if one is staged) is signed with your key, at or above
  the floor; no trial is armed;
- Stage 2 signing is on and the updater signs (or you pass `--sign-stage2-by-hand` and promise to sign
  after every update).

It writes `stage1-enforce.macho` with the audit object as its rollback. Enrol it from 1TR the same way.
From then on this install's Stage 1 starts only a Stage 2 signed with your key, and the secure flavour
reboots rather than open a proxy. Keep `stage2.key` safe: losing it means no more Stage 2 updates until
you enrol another Stage 1 from 1TR.

## Recovery

- **The install doesn't boot** (a reboot loop, or a panel message and a reboot): hold the power button,
  pick macOS or another install in the startup picker. Nothing else changed.
- **Go back to the previous Stage 1:** in 1TR, run `enroll-stage1.sh` again with the rollback object and
  its MD5 in place of the new one (for enforce: `stage1-audit.macho`; for audit: `stage1-stock.macho`).
  The `--keep` you gave at enrolment was there for this.
- **Unsigned Stage 2 under enforce** (an update wrote one without signing it): in 1TR enrol
  `stage1-audit.macho` again, boot the install, run `sudo aurora-secboot stage2 sign`, check `status`,
  then enrol `stage1-enforce.macho` again.
- aurora-installer's Recovery app can update Stage 1 too. It fills the keys recorded at install, so on an
  install made before owner keys existed it puts back a Stage 1 without your key. Use `enroll-stage1.sh`
  for this install from now on.

## What this does not cover

Root on this install can still sign with the key. Another OS on the disk can't make this install boot
its Stage 2 any more, but it can still wipe the install or make itself the default startup disk and show
a lookalike unlock prompt, so check which volume you start. U-Boot Secure Boot with your own keys and
signed UKIs (`aurora-secboot seed`, `uki build`, `enable`) are the next steps after enforce; see
[README.md](README.md).
