import importlib.util
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_grub():
    spec = importlib.util.spec_from_file_location('harden_grub', ROOT / 'images/harden-grub.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BOOT = '0ba642d0-1111-4222-8333-444455556666'
EFI = '4866-D833'


class GrubTests(unittest.TestCase):
    def setUp(self):
        self.grub = load_grub()

    def assert_no_unhinted_search(self, text):
        for line in text.splitlines():
            if re.match(r'\s*search\b', line):
                self.assertIn('--hint=', line, line)
                self.assertIn('--no-floppy', line, line)

    def test_fedora_stub_locates_boot_before_any_search(self):
        stub = self.grub.fedora_stub(BOOT, '/grub2')
        self.assert_no_unhinted_search(stub)
        locate = stub.index('test -f "($aurora_disk,gpt$aurora_number)/grub2/aurora-boot-' + BOOT)
        search = stub.index('search --no-floppy --fs-uuid --set=root --hint="$aurora_boot" ' + BOOT)
        self.assertLess(locate, search)
        # A missing boot filesystem stops with a message; nothing else is probed.
        self.assertIn('Other disks are not searched', stub)
        self.assertNotIn('root-dev-only', stub)
        self.assertIn('"$fw_path"', stub)

    def test_arch_stub_compares_uuid_with_probe(self):
        stub = self.grub.arch_stub(BOOT.upper(), '/grub')
        self.assert_no_unhinted_search(stub)
        self.assertIn('probe --set=aurora_uuid --fs-uuid', stub)
        self.assertIn(f'= "{BOOT}" ]', stub)

    def test_main_configuration_lookups_stay_on_startup_disk(self):
        main = ('insmod ext2\nsearch --no-floppy --fs-uuid --set=root ' + BOOT + '\n'
                'insmod fat\nsearch --no-floppy --fs-uuid --set=boot ' + EFI + '\n'
                'if [ x$feature_platform_search_hint = xy ]; then\n'
                '  search --no-floppy --fs-uuid --set=root --hint-bios=hd0,gpt2 --hint-efi=hd0,gpt2 ' + BOOT + '\n'
                'fi\n')
        hardened = self.grub.harden_main(main, EFI)
        self.assert_no_unhinted_search(hardened)
        self.assertNotIn('hint-efi=hd0', hardened)
        # The EFI partition is the one GRUB was loaded from; no UUID needed.
        self.assertIn('set boot="$aurora_esp"\nif [ -z "$boot" ]; then', hardened)
        self.assertTrue(hardened.startswith(self.grub.MARKER))
        self.assertEqual(self.grub.harden_main(hardened, EFI), hardened)

    def test_installed_fedora_tree_is_hardened_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for vendor in ('BOOT', 'fedora'):
                path = root / 'boot/efi/EFI' / vendor / 'grub.cfg'
                path.parent.mkdir(parents=True)
                path.write_text(f'search --fs-uuid --set=root {BOOT}\nset prefix=($root)/grub2\n'
                                'configfile ($root)/grub2/grub.cfg\n')
            main = root / 'boot/grub2/grub.cfg'
            main.parent.mkdir(parents=True)
            main.write_text(f'search --no-floppy --fs-uuid --set=boot {EFI}\n')
            changed = self.grub.harden(root, 'fedora', BOOT, EFI)
            self.assertIn(root / f'boot/grub2/aurora-boot-{BOOT}', changed)
            for vendor in ('BOOT', 'fedora'):
                self.assertEqual((root / 'boot/efi/EFI' / vendor / 'grub.cfg').read_text(),
                                 self.grub.fedora_stub(BOOT, '/grub2'))
            self.assertEqual(self.grub.harden(root, 'fedora', BOOT, EFI), [])


class SplashTests(unittest.TestCase):
    def test_encryption_percentage_is_in_the_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            fake = temporary / 'bin/plymouth'
            fake.parent.mkdir()
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PLYMOUTH_LOG"\n')
            fake.chmod(0o755)
            wrapper = (ROOT / 'libexec/aurora-initrd-root').read_text()
            block = wrapper[wrapper.index('# BEGIN Aurora splash presentation'):
                            wrapper.index('# END Aurora splash presentation')]
            (temporary / 'splash.sh').write_text(block)
            script = f'''
set -euo pipefail
source "{ROOT}/vendor/omarchy-mac-encrypt"
PROGRESS_FILE={temporary}/progress
source {temporary}/splash.sh
omarchy_mac_encrypt_progress 0 "Encrypting your drive..."
printf '%s\\n' '{{"device_bytes":"370","device_size":"1000"}}' 'cryptsetup notice' \\
  '{{"device_bytes":"370","device_size":"1000"}}' '{{"device_bytes":"1000","device_size":"1000"}}' |
  omarchy_mac_encrypt_follow_reencrypt
'''
            log = temporary / 'plymouth.log'
            environment = dict(os.environ, PATH=f'{fake.parent}:{os.environ["PATH"]}',
                               PLYMOUTH_LOG=str(log), AURORA_SPLASH_MESSAGE=str(temporary / 'message'))
            result = subprocess.run(['bash', '-c', script], env=environment, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, 'cryptsetup notice\n')
            calls = [line for line in log.read_text().splitlines() if line != '--ping']
            self.assertEqual(calls, [
                'system-update --progress=0',
                'display-message --text=Encrypting your drive... 0%',
                'system-update --progress=37',
                'hide-message --text=Encrypting your drive... 0%',
                'display-message --text=Encrypting your drive... 37%',
                'system-update --progress=100',
                'hide-message --text=Encrypting your drive... 37%',
                'display-message --text=Encrypting your drive... 100%',
            ])
            self.assertEqual((temporary / 'progress').read_text(), '100\n')


class UnitTests(unittest.TestCase):
    def test_greeter_waits_without_failing(self):
        dropin = (ROOT / 'systemd/aurora-display-manager.conf').read_text()
        self.assertNotIn('Requires=', dropin)
        self.assertIn('ConditionPathExists=!/var/lib/aurora/firstboot/pending', dropin)
        self.assertIn('After=aurora-firstboot.service', dropin)

    def test_setup_releases_the_splash_with_a_bound(self):
        service = (ROOT / 'systemd/aurora-firstboot.service').read_text()
        self.assertIn('plymouth-quit.service', service)
        self.assertNotIn('plymouth-quit-wait.service', service)
        script = (ROOT / 'libexec/aurora-firstboot').read_text()
        self.assertIn('timeout --kill-after=2 10 plymouth quit', script)
        self.assertIsNone(re.search(r'(?m)^\s*plymouth quit', script))
        # set -e must stay in force, so each attempt runs as its own process.
        self.assertIn('until "$BASH" "${BASH_SOURCE[0]}" --attempt "$mode"; do', script)

    def test_unit_leaves_tty1_to_the_splash(self):
        # systemd's TTYReset/TTYVHangup on tty1 cut Plymouth off its keyboard,
        # so the splash's disk-password prompt never saw a keystroke.
        service = (ROOT / 'systemd/aurora-firstboot.service').read_text()
        self.assertIn('StandardInput=null', service)
        for setting in ('TTYPath', 'TTYReset', 'TTYVHangup', 'tty-force', 'StandardOutput=tty'):
            self.assertNotIn(setting, service)
        script = (ROOT / 'libexec/aurora-firstboot').read_text()
        self.assertIn('exec 0<>"$console" 1>&0 2>&0', script)

    def test_text_wizard_can_be_asked_for_recovery(self):
        service = (ROOT / 'systemd/aurora-firstboot.service').read_text()
        self.assertIn('ConditionPathExists=|/var/lib/aurora/firstboot/pending', service)
        self.assertIn('ConditionKernelCommandLine=|aurora.firstboot=text', service)

    def test_desktop_setup_protects_the_disk_before_the_greeter(self):
        script = (ROOT / 'libexec/aurora-firstboot').read_text()
        desktop = script[script.index('setup_desktop() {'):script.index('setup_text() {')]
        self.assertLess(desktop.index('protect_disk "$password"'), desktop.index('rm -- "$state/pending"'))
        self.assertIn('plymouth ask-for-password', script)
        self.assertLess(desktop.index('allow_wheel'), desktop.index('rm -- "$state/pending"'))

    def test_plasma_setup_runs_after_the_disk_step(self):
        dropin = (ROOT / 'systemd/aurora-plasma-setup.conf').read_text()
        self.assertIn('After=aurora-firstboot.service', dropin)
        self.assertIn('ConditionPathExists=!/var/lib/aurora/firstboot/pending', dropin)
        self.assertIn('ConditionPathExists=!/var/lib/aurora/firstboot/complete', dropin)
        self.assertNotIn('Requires=', dropin)
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(['make', '-s', 'install-common', 'DESTDIR=' + temporary], cwd=ROOT, check=True)
            installed = Path(temporary) / 'usr/lib/systemd/system/plasma-setup.service.d/aurora-firstboot.conf'
            self.assertEqual(installed.read_text(), dropin)

    def test_selinux_labels_every_installed_program(self):
        pattern = (ROOT / 'selinux/aurora_firstboot.fc').read_text().split()[0]
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(['make', '-s', 'install-common', 'DESTDIR=' + temporary], cwd=ROOT, check=True)
            programs = [path for path in (Path(temporary) / 'usr/lib/aurora').iterdir()
                        if path.is_file() and path.stat().st_mode & stat.S_IXUSR]
            self.assertGreaterEqual(len(programs), 4)
            for path in programs:
                self.assertRegex('/usr/lib/aurora/' + path.name, '^' + pattern + '$')


if __name__ == '__main__':
    unittest.main()
