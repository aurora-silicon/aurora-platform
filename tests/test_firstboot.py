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
        # The stock quit waits for setup, never the other way round, so the
        # splash survives until the disk-password prompt is done.
        after = next(line for line in service.splitlines() if line.startswith('After='))
        self.assertNotIn('plymouth-quit', after)
        self.assertIn('After=aurora-firstboot.service',
                      (ROOT / 'systemd/aurora-plymouth-quit.conf').read_text())
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

    def test_status_output_is_off_while_setup_owns_the_console(self):
        # Images boot with systemd.show_status=auto; once setup had waited a
        # few seconds, status lines and "A start job is running" covered the
        # text wizard's prompts. Status is turned off whatever it reads when
        # setup starts, then reset to the configured mode.
        script = (ROOT / 'libexec/aurora-firstboot').read_text()
        functions = re.search(r'(?ms)^show_status_call\(\) \{.*?^restore_console\(\) \{.*?^\}$', script).group(0)
        for shown in ('yes', 'no'):
            with self.subTest(shown=shown), tempfile.TemporaryDirectory() as temporary:
                log = Path(temporary) / 'modes'
                subprocess.run(['bash', '-c', f"""
                    systemctl() {{ echo {shown}; }}
                    busctl() {{ [[ $5 == SetShowStatus && $6 == s ]] && echo "mode=$7" >> {log}; }}
                    {functions}
                    quiet_console; restore_console"""], check=True)
                self.assertEqual(log.read_text().split(), ['mode=no', 'mode='])

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

    def test_desktop_setup_lets_the_account_password_unlock_the_disk(self):
        script = (ROOT / 'libexec/aurora-firstboot').read_text()
        desktop = script[script.index('setup_desktop() {'):script.index('setup_text() {')]
        # Account mode asks nothing on the splash: the hooks are armed before
        # the greeter or Plasma Setup can start.
        self.assertLess(desktop.index('arm_password_hooks'), desktop.index('rm -- "$state/pending"'))
        self.assertIn('[[ $mode != desktop ]] || ! password_hooks_available || mode=account', script)
        self.assertNotIn('chpasswd password', script)
        self.assertIn('account) setup_desktop account ;;', script)

    @staticmethod
    def hook_functions():
        script = (ROOT / 'libexec/aurora-firstboot').read_text()
        return script[script.index('pam_dir='):script.index('# Ask twice for the disk password')]

    PLASMALOGIN = '#%PAM-1.0\nauth     substack      password-auth\nsession  include       postlogin\n'
    AUTOLOGIN = '#%PAM-1.0\nauth     required      pam_permit.so\n'

    def run_hooks(self, pam, vendor, action, extra=''):
        return subprocess.run(['bash', '-c', f"""
            set -euo pipefail
            label() {{ :; }}
            AURORA_PAM_DIR={pam}
            AURORA_PAM_VENDOR_DIR={vendor}
            AURORA_DECLARED_HOOKS={pam}/../declared
            {extra}
            {self.hook_functions()}
            {action}"""])

    def test_greeter_hooks_use_vendor_files_through_a_temporary_override(self):
        hook = '/usr/lib/aurora/aurora-password-hook'
        with tempfile.TemporaryDirectory() as temporary:
            pam, vendor = Path(temporary) / 'pam.d', Path(temporary) / 'vendor'
            pam.mkdir()
            vendor.mkdir()
            # Fedora 44: the greeter's files are vendor files; sddm is edited in place.
            (vendor / 'plasmalogin').write_text(self.PLASMALOGIN)
            (vendor / 'plasmalogin-autologin').write_text(self.AUTOLOGIN)
            (pam / 'sddm').write_text(self.PLASMALOGIN)
            (pam / 'sddm').chmod(0o640)
            # A generated, read-only configuration is never edited.
            (pam / 'store').write_text('auth required pam_unix.so\n')
            (pam / 'gdm-password').symlink_to(pam / 'store')
            self.assertEqual(self.run_hooks(pam, vendor,
                'password_hooks_available; arm_password_hooks; arm_password_hooks').returncode, 0)
            login = (pam / 'plasmalogin').read_text().splitlines()
            self.assertTrue(login[0].startswith('# aurora-firstboot: a temporary copy'))
            self.assertEqual(login[1], '#%PAM-1.0')
            self.assertTrue(login[2].startswith('# aurora-firstboot: the owner password'))
            self.assertEqual(login[3], f'auth optional pam_exec.so quiet expose_authtok {hook}')
            self.assertEqual(login[4:], self.PLASMALOGIN.splitlines()[1:])
            self.assertIn(f'auth requisite pam_exec.so quiet {hook} --gate',
                          (pam / 'plasmalogin-autologin').read_text())
            self.assertEqual((pam / 'sddm').read_text().count(hook), 1)
            self.assertEqual((pam / 'sddm').stat().st_mode & 0o777, 0o640)
            self.assertEqual((pam / 'store').read_text(), 'auth required pam_unix.so\n')
            self.assertEqual((vendor / 'plasmalogin').read_text(), self.PLASMALOGIN)
            self.assertEqual(self.run_hooks(pam, vendor, 'disarm_password_hooks').returncode, 0)
            # The vendor files apply again; the edited file is as it was.
            self.assertFalse((pam / 'plasmalogin').exists())
            self.assertFalse((pam / 'plasmalogin-autologin').exists())
            self.assertEqual((pam / 'sddm').read_text(), self.PLASMALOGIN)

    def test_without_a_greeter_file_the_disk_password_is_asked_on_the_console(self):
        with tempfile.TemporaryDirectory() as temporary:
            pam, vendor = Path(temporary) / 'pam.d', Path(temporary) / 'vendor'
            pam.mkdir()
            vendor.mkdir()
            (pam / 'store').write_text('auth required pam_unix.so\n')
            for service in ('sddm', 'sddm-autologin', 'gdm-password'):
                (pam / service).symlink_to(pam / 'store')
            self.assertEqual(self.run_hooks(pam, vendor, 'password_hooks_available').returncode, 1)
            # A distribution can declare its own permanent hooks (NixOS).
            (Path(temporary) / 'declared').write_text('sddm\n')
            self.assertEqual(self.run_hooks(pam, vendor,
                'password_hooks_available && arm_password_hooks').returncode, 0)
            self.assertFalse(any(p.name.endswith('aurora-new') for p in pam.iterdir()))
        script = (ROOT / 'libexec/aurora-firstboot').read_text()
        self.assertIn('if [[ $mode == text || $mode == desktop ]]; then\n  release_splash', script)

    def test_owner_sign_in_is_handed_to_the_rekey_and_the_gate_holds_autologin(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            state = temporary / 'state'
            state.mkdir()
            (state / 'desktop').write_text('desktop\n')
            (state / 'luks-key').write_bytes(b'k' * 64)
            checker = temporary / 'unix_chkpwd'
            # The fake checker accepts only "right" for alice.
            checker.write_text('#!/bin/bash\nIFS= read -r -d "" p; [[ $1 == alice && $p == right ]]\n')
            checker.chmod(0o755)
            started = temporary / 'started'

            def hook(user, kind, password, *arguments):
                rekey = temporary / 'rekey'
                if rekey.exists():
                    for path in rekey.iterdir():
                        path.unlink()
                result = subprocess.run(['bash', '-c', f"""
                    set -euo pipefail
                    label() {{ :; }}
                    log() {{ :; }}
                    getent() {{ [[ $2 == alice ]] && echo 'alice:x:1000:1000::/home/alice:/bin/bash'; [[ $2 == plasma-setup ]] && echo 'plasma-setup:x:990:990::/run/plasma-setup:/bin/sh'; true; }}
                    systemctl() {{ echo "$*" >> {started}; }}
                    state={state}
                    AURORA_REKEY_DIR={rekey}
                    AURORA_UNIX_CHKPWD={checker}
                    {self.hook_functions()}
                    PAM_USER={user} PAM_TYPE={kind} password_hook {' '.join(arguments)}"""],
                    input=password.encode() + b'\0')
                file = rekey / 'password'
                return result.returncode, file.read_text() if file.exists() else None

            self.assertEqual(hook('alice', 'auth', 'right'), (0, 'right'))
            self.assertEqual(hook('alice', 'auth', 'wrong'), (0, None))
            self.assertEqual(hook('plasma-setup', 'auth', 'right'), (0, None))
            self.assertEqual(hook('alice', 'session', 'right'), (0, None))
            self.assertEqual(started.read_text().splitlines(), ['start --no-block aurora-disk-rekey.service'])
            # The owner's automatic login waits for a password sign-in; the
            # setup user's does not.
            self.assertEqual(hook('alice', 'auth', '', '--gate')[0], 1)
            self.assertEqual(hook('plasma-setup', 'auth', '', '--gate')[0], 0)
            # Once the temporary key is gone, nothing is held or handed over.
            (state / 'luks-key').unlink()
            self.assertEqual(hook('alice', 'auth', '', '--gate')[0], 0)
            self.assertEqual(hook('alice', 'auth', 'right'), (0, None))

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
