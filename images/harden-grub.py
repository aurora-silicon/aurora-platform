#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Keep GRUB's filesystem lookups on the disk it started from.

U-Boot on Apple Silicon exposes every NVMe namespace as a disk, and probing
the extra namespaces can wedge U-Boot's NVMe queue. GRUB's `search` falls back
to probing every filesystem on every disk when its hints miss, so a missing or
stale UUID turns into a whole-system scan. Fedora's `--root-dev-only` no longer
prevents that (since its CVE-2023-4001 rework it only checks USB duplicates).

The EFI stub therefore locates the boot filesystem itself, on the startup disk
only, before any UUID lookup:
  * Fedora's signed GRUB has no `probe`; a UUID-named marker file on the boot
    filesystem is found with `test -f`, which is silent and never leaves the
    named partition.
  * The Arch core image is built here with `probe`, which compares the UUID.
One exact-hint `search` then fills GRUB's search cache, so every identical
lookup in the main configuration is answered without probing. The main
configuration's EFI-partition lookup is replaced by the partition GRUB was
loaded from. When the boot filesystem is not on the startup disk, GRUB stops
with a message instead of scanning.
"""
import argparse
from pathlib import Path
import re
import subprocess

MARKER = "# Aurora: look for this installation only on the disk GRUB started from."
PREAMBLE = f"""\
{MARKER}
# $root starts on that disk; the EFI partition is where GRUB was loaded from
# ($cmdpath upstream, $fw_path in Fedora's GRUB).
regexp --set=1:aurora_esp '^\\(([^)]+)\\)' "$cmdpath"
if [ -z "$aurora_esp" ]; then regexp --set=1:aurora_esp '^\\(([^)]+)\\)' "$fw_path"; fi
regexp --set=1:aurora_disk '^([^,]+),' "$root"
if [ -z "$aurora_disk" ]; then regexp --set=1:aurora_disk '^([^,]+),' "$aurora_esp"; fi
"""
PARTITIONS = " ".join(str(number) for number in range(1, 33))
UUID = r"[0-9A-Fa-f][0-9A-Fa-f-]{3,}"
SEARCH = re.compile(rf"^(?P<indent>[ \t]*)search\b(?=[^\n]*--fs-uuid)(?P<args>[^\n]*?)\s(?P<uuid>{UUID})[ \t]*$",
                    re.MULTILINE)
VARIABLE = re.compile(r"--set(?:=|\s+)(\w+)")


def marker_name(boot_uuid):
    return f"aurora-boot-{boot_uuid.lower()}"


def stub(boot_uuid, grub_dir, locate):
    """EFI/<vendor>/grub.cfg: find /boot on the startup disk, then hand off."""
    return f"""{PREAMBLE}set aurora_boot=
if [ -n "$aurora_disk" ]; then
  for aurora_number in {PARTITIONS}; do
{locate}
  done
fi
if [ -n "$aurora_boot" ]; then
  # One exact-hint lookup fills GRUB's search cache for the main configuration.
  search --no-floppy --fs-uuid --set=root --hint="$aurora_boot" {boot_uuid}
  set prefix=($root){grub_dir}
  export prefix
  configfile ($root){grub_dir}/grub.cfg
else
  echo "Aurora cannot find its boot filesystem ({boot_uuid}) on the disk it started from."
  echo "Other disks are not searched. Reinstall Aurora or repair its boot partition."
fi
"""


def fedora_stub(boot_uuid, grub_dir):
    # test -f is silent on empty slots and foreign filesystems.
    locate = f"""    if test -f "($aurora_disk,gpt$aurora_number){grub_dir}/{marker_name(boot_uuid)}"; then
      set aurora_boot="$aurora_disk,gpt$aurora_number"
      break
    fi"""
    return stub(boot_uuid, grub_dir, locate)


def arch_stub(boot_uuid, grub_dir):
    locate = f"""    if test -d "($aurora_disk,gpt$aurora_number){grub_dir}"; then
      probe --set=aurora_uuid --fs-uuid "($aurora_disk,gpt$aurora_number)"
      if [ "$aurora_uuid" = "{boot_uuid.lower()}" ]; then
        set aurora_boot="$aurora_disk,gpt$aurora_number"
        break
      fi
    fi"""
    return stub(boot_uuid, grub_dir, locate)


def lookup(indent, variable, uuid):
    return f'{indent}search --no-floppy --fs-uuid --set={variable} --hint="${{aurora_disk}}," {uuid}'


def harden_main(text, efi_uuid):
    """Hint every UUID lookup; the EFI partition comes from where GRUB started."""
    if MARKER in text:
        return text
    count = 0

    def replace(match):
        nonlocal count
        found = VARIABLE.search(match["args"])
        if not found:
            return match[0]
        variable, uuid, indent = found[1], match["uuid"], match["indent"]
        count += 1
        if efi_uuid and uuid.lower() == efi_uuid.lower():
            return (f'{indent}set {variable}="$aurora_esp"\n'
                    f'{indent}if [ -z "${variable}" ]; then\n'
                    f'{lookup(indent + "  ", variable, uuid)}\n'
                    f'{indent}fi')
        return lookup(indent, variable, uuid)

    text = SEARCH.sub(replace, text)
    return PREAMBLE + text if count else text


def write_if_changed(path, text, changed):
    if path.is_symlink():
        raise SystemExit(f"Refusing to replace a symbolic link: {path}")
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        changed.append(path)


def harden(root, flavor, boot_uuid, efi_uuid, boot_is_root=False, mkimage=None, modules_dir=None):
    changed = []
    boot = root / "boot"
    grub_name = "grub2" if flavor == "fedora" else "grub"
    # GRUB paths are relative to the filesystem that holds /boot.
    grub_dir = f"/boot/{grub_name}" if boot_is_root else f"/{grub_name}"
    main = boot / grub_name / "grub.cfg"
    if flavor == "fedora":
        write_if_changed(boot / grub_name / marker_name(boot_uuid), "", changed)
        stubs = [path for path in sorted((boot / "efi/EFI").glob("*/grub.cfg"))
                 if "configfile" in path.read_text()]
        if not stubs:
            raise SystemExit("No EFI GRUB stub configuration was found")
        for path in stubs:
            write_if_changed(path, fedora_stub(boot_uuid, grub_dir), changed)
    else:
        core = boot / "efi/EFI/BOOT/BOOTAA64.EFI"
        if not core.is_file():
            raise SystemExit("The removable GRUB core image is missing")
        write_if_changed(core.with_name("grub.cfg"), arch_stub(boot_uuid, grub_dir), changed)
        # grub-install embeds a lookup that falls back to every disk. This core
        # runs the stub next to it instead; the main configuration's modules
        # still load from the boot filesystem once the stub hands off.
        modules = ["normal", "configfile", "part_gpt", "part_msdos", "fat", "ext2", "probe",
                   "regexp", "search", "search_fs_uuid", "test", "echo", "linux"]
        if mkimage:
            # A GRUB outside the target (NixOS keeps it in the store).
            command = [mkimage, f"--directory={modules_dir}", f"--output={core}"]
        else:
            command = ["chroot", str(root), "grub-mkimage", "--directory=/usr/lib/grub/arm64-efi",
                       "--output=/boot/efi/EFI/BOOT/BOOTAA64.EFI"]
        subprocess.run([*command, "--format=arm64-efi", "--prefix=/EFI/BOOT", *modules], check=True)
        changed.append(core)
    if main.is_file():
        write_if_changed(main, harden_main(main.read_text(), efi_uuid), changed)
    return changed


def mounted_uuid(path):
    return subprocess.check_output(["findmnt", "-nro", "UUID", "--target", str(path)], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, nargs="?", default=Path("/"),
                        help="image or system root holding /boot and /boot/efi")
    parser.add_argument("--flavor", choices=("fedora", "arch"), required=True)
    parser.add_argument("--boot-uuid", help="filesystem UUID holding /boot (default: detect)")
    parser.add_argument("--efi-uuid", help="EFI partition UUID (default: detect)")
    parser.add_argument("--grub-mkimage", help="grub-mkimage to run directly instead of in a chroot")
    parser.add_argument("--grub-modules", help="arm64-efi module directory for --grub-mkimage")
    parser.add_argument("--boot-on-root", action="store_true",
                        help="/boot is a directory of the root filesystem, not its own partition")
    args = parser.parse_args()
    root = args.root.resolve()
    boot_uuid = args.boot_uuid or mounted_uuid(root / "boot")
    efi_uuid = args.efi_uuid or mounted_uuid(root / "boot/efi")
    boot_on_root = args.boot_on_root or (args.boot_uuid is None and
                                         mounted_uuid(root / "boot") == mounted_uuid(root))
    if bool(args.grub_mkimage) != bool(args.grub_modules):
        parser.error("--grub-mkimage and --grub-modules go together")
    for path in harden(root, args.flavor, boot_uuid, efi_uuid, boot_on_root,
                       args.grub_mkimage, args.grub_modules):
        print(f"GRUB lookups confined to the startup disk: {path}")


if __name__ == "__main__":
    main()
