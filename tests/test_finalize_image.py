from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FINALIZE = ROOT / 'images/finalize-image'


def gnu_install():
    try:
        result = subprocess.run(['install', '--version'], capture_output=True, text=True)
    except OSError:
        return False
    return 'GNU' in result.stdout


def stage1_block():
    """The finalizer's Stage 1 step: from stage1.bin up to the Stage 2 preparation."""
    text = FINALIZE.read_text()
    start = text.index('install -Dm644 "$scratch/root/usr/share/aura/stage1/aura-raw.bin"')
    return text[start:text.index('python3 "$(dirname -- "${BASH_SOURCE[0]}")/prepare-stage2.py"', start)]


@unittest.skipUnless(gnu_install(), 'GNU install is required')
class Stage1Tests(unittest.TestCase):
    def run_block(self, files):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage1 = root / 'scratch/root/usr/share/aura/stage1'
            stage1.mkdir(parents=True)
            for name, data in files.items():
                (stage1 / name).write_bytes(data)
            output = root / 'output'
            (output / 'esp').mkdir(parents=True)
            script = 'set -euo pipefail\nscratch=$1\noutput=$2\n' + stage1_block()
            result = subprocess.run(['bash', '-c', script, 'finalize', str(root / 'scratch'), str(output)],
                                    capture_output=True, text=True)
            esp = {path.name: path.read_bytes() for path in (output / 'esp/aurora').iterdir()} \
                if (output / 'esp/aurora').is_dir() else {}
            return result, esp

    def test_both_stage1_forms_reach_the_esp(self):
        result, esp = self.run_block({'aura-raw.bin': b'raw', 'aura-boot.macho': b'macho'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(esp, {'stage1.bin': b'raw', 'stage1.macho': b'macho'})
        self.assertEqual(result.stderr, '')

    def test_an_aura_without_the_macho_form_warns_and_keeps_stage1_bin(self):
        result, esp = self.run_block({'aura-raw.bin': b'raw'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(esp, {'stage1.bin': b'raw'})
        self.assertIn('Warning: no /usr/share/aura/stage1/aura-boot.macho', result.stderr)

    def test_stage1_bin_is_still_required(self):
        result, _ = self.run_block({'aura-boot.macho': b'macho'})
        self.assertNotEqual(result.returncode, 0)


def image_record_block():
    """The finalizer's image.json writer."""
    text = FINALIZE.read_text()
    start = text.index("<<'PY'\n") + len("<<'PY'\n")
    return text[start:text.index('\nPY\n', start)]


class ImageRecordTests(unittest.TestCase):
    def write_record(self, profile, distribution):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for name in ('boot.img', 'root.img'):
                (output / name).write_bytes(name.encode())
            subprocess.run(['python3', '-c', image_record_block(), str(output), profile, 'r-uuid', 'b-uuid',
                            'ABCD-1234', '536870912', distribution], check=True)
            import json
            return json.loads((output / 'image.json').read_text())

    def test_each_distribution_names_its_own_layout(self):
        for profile, distribution, layout in (('fedora-kde', 'fedora', 'fedora-btrfs-v1'),
                                              ('arch-gnome', 'arch', 'alarm-btrfs-v1'),
                                              ('debian-minimal', 'debian', 'debian-btrfs-v1')):
            record = self.write_record(profile, distribution)
            self.assertEqual(record['layout_kind'], layout)
            self.assertEqual(record['installation_profile'], profile)
            self.assertEqual(record['root_subvolume'], 'root')
            self.assertEqual(record['efi_volume_id'], 'ABCD1234')


def block(name):
    """A marked step of the finalizer: from '# BEGIN name' to '# END name'."""
    text = FINALIZE.read_text()
    start = text.index(f'# BEGIN {name}\n')
    return text[start:text.index(f'# END {name}\n', start)]


# The commands the steps run inside the image, recorded and answered from files.
STUBS = r'''
record() { printf '%s\n' "$*" >> "$fixtures/log"; }
chroot() {
  shift
  record "$*"
  case $1 in
    systemctl)
      case $2 in
        preset-all) cat "$fixtures/preset-${3#--preset-mode=}" 2>/dev/null || true;;
        list-unit-files) cat "$fixtures/unit-files";;
      esac;;
    getent) [[ -e $fixtures/wheel ]];;
    lsinitrd) printf 'usr/lib/systemd/system/aurora-initrd-root.service\netc/aurora/image-layout\n';;
    dpkg-query|rpm|pacman) cat "$fixtures/$1";;
  esac
}
python3() { record "python3 $*"; }
'''


class StepTests(unittest.TestCase):
    """The profile, GRUB, initrd and package-list steps, run with stubbed image commands."""

    def run_step(self, name, profile, files=None):
        with tempfile.TemporaryDirectory() as temporary:
            fixtures = Path(temporary)
            (fixtures / 'scratch/root/usr/lib/modules/7.3.0-1-aurora').mkdir(parents=True)
            (fixtures / 'output').mkdir()
            for path, text in (files or {}).items():
                (fixtures / path).write_text(text)
            script = (f'set -euo pipefail\nfixtures={fixtures}\nscratch=$fixtures/scratch\n'
                      f'output=$fixtures/output\nprofile={profile}\nboot_uuid=b-uuid\nefi_uuid=e-uuid\n'
                      + STUBS + block('profile') + (block(name) if name != 'profile' else ''))
            # Run from a file: the steps find their helpers next to the script.
            (fixtures / 'step').write_text(script)
            (fixtures / 'label-fedora-rootfs').write_text('')
            result = subprocess.run(['bash', str(fixtures / 'step')], capture_output=True, text=True)
            log = (fixtures / 'log').read_text().splitlines() if (fixtures / 'log').exists() else []
            packages = fixtures / 'output/packages.txt'
            return result, log, packages.read_text() if packages.exists() else None

    def test_profiles_select_their_distribution_and_initrd_tool(self):
        for profile, expected in (('fedora-kde', 'fedora dracut'), ('arch-minimal', 'arch mkinitcpio'),
                                  ('debian-gnome', 'debian dracut'), ('nixos-kde', None)):
            with self.subTest(profile=profile):
                script = f'profile={profile}\n' + block('profile') + 'echo "$distribution $backend"\n'
                result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
                if expected is None:
                    self.assertEqual(result.returncode, 2)
                else:
                    self.assertEqual(result.stdout.strip(), expected, result.stderr)

    def test_only_fedora_uses_fedora_grub(self):
        for profile, flavor in (('fedora-gnome', 'fedora'), ('arch-kde', 'arch'), ('debian-kde', 'arch')):
            with self.subTest(profile=profile):
                result, log, _ = self.run_step('grub', profile)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'--flavor {flavor} --boot-uuid b-uuid --efi-uuid e-uuid', log[0])

    def test_debian_checks_presets_as_its_first_boot_and_writes_its_initrd_name(self):
        result, log, _ = self.run_step('dracut', 'debian-minimal', {'wheel': '', 'unit-files': ''})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('systemctl preset-all --preset-mode=enable-only', log)
        self.assertIn('dracut --force --no-hostonly --add aurora-root systemd-ask-password '
                      '/boot/initrd.img-7.3.0-1-aurora 7.3.0-1-aurora', log)
        # Anaconda's initial setup and SELinux labelling are Fedora's alone.
        self.assertFalse([line for line in log if 'initial-setup' in line or line.startswith('/bin/bash')])

    def test_fedora_checks_presets_in_full_and_labels_its_root(self):
        result, log, _ = self.run_step('dracut', 'fedora-kde')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('systemctl preset-all --preset-mode=full', log)
        self.assertIn('dracut --force --no-hostonly --add aurora-root systemd-ask-password '
                      '/boot/initramfs-7.3.0-1-aurora.img 7.3.0-1-aurora', log)
        self.assertIn('systemctl disable initial-setup.service initial-setup-graphical.service', log)
        self.assertIn('/bin/bash -s -- /', log)

    def test_a_unit_the_first_boot_would_change_fails_the_image(self):
        for profile, mode in (('debian-kde', 'enable-only'), ('fedora-minimal', 'full')):
            with self.subTest(profile=profile):
                change = "Created symlink '/etc/systemd/system/timers.target.wants/fstrim.timer'.\n"
                result, log, _ = self.run_step('dracut', profile, {'wheel': '', 'unit-files': '',
                                                                   f'preset-{mode}': change})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('first boot cannot apply them', result.stderr)
                self.assertFalse([line for line in log if line.startswith('dracut')])

    def test_debian_units_must_match_their_presets_in_both_directions(self):
        listing = ('NetworkManager.service enabled enabled\n'
                   'default.target alias -\n'
                   'multi-user.target static -\n'
                   'ssh.service disabled disabled\n')
        result, log, _ = self.run_step('dracut', 'debian-kde', {'wheel': '', 'unit-files': listing})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('systemctl list-unit-files --no-legend --no-pager', log)
        for line in ('fstrim.timer enabled disabled', 'bluetooth.service disabled enabled'):
            with self.subTest(unit=line):
                result, log, _ = self.run_step('dracut', 'debian-kde',
                                               {'wheel': '', 'unit-files': listing + line + '\n'})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(line, result.stderr)
                self.assertIn('against their presets', result.stderr)
                self.assertFalse([entry for entry in log if entry.startswith('dracut')])

    def test_fedora_relies_on_its_full_preset_pass(self):
        result, log, _ = self.run_step('dracut', 'fedora-kde')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([line for line in log if 'list-unit-files' in line])

    def test_debian_lists_its_packages_with_dpkg(self):
        result, _, packages = self.run_step('package list', 'debian-kde',
                                            {'dpkg-query': 'zstd 1.5 arm64\nbash 5.3 arm64\n'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(packages, 'bash 5.3 arm64\nzstd 1.5 arm64\n')


if __name__ == '__main__':
    unittest.main()
