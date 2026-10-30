"""The disk password prompt's keymap, the temporary key and the PAM hooks."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'libexec/aurora-firstboot').read_text()


def between(start, end):
    return SCRIPT[SCRIPT.index(start):SCRIPT.index(end)]


KBD_MODEL_MAP = (
    '# Generated\n'
    'us\t\t\tus\tpc105+inet\t-\t\tterminate:ctrl_alt_bksp\n'
    'de\t\t\tde\tpc105\t\t-\t\tterminate:ctrl_alt_bksp\n'
    'de-latin1-nodeadkeys\tde\tpc105\t\tnodeadkeys\tterminate:ctrl_alt_bksp\n'
    'uk\t\t\tgb\tpc105\t\t-\t\tterminate:ctrl_alt_bksp\n'
    'dvorak\t\t\tus\tpc105\t\tdvorak\t\tterminate:ctrl_alt_bksp\n'
)


class KeyboardTests(unittest.TestCase):
    """The disk password is typed with the initrd's console keymap."""

    def run_keyboard(self, temporary, action, vconsole='', xorg=None, declared=None, extra=''):
        temporary = Path(temporary)
        (temporary / 'vconsole.conf').write_text(vconsole)
        (temporary / 'kbd-model-map').write_text(KBD_MODEL_MAP)
        if xorg is not None:
            (temporary / '00-keyboard.conf').write_text(xorg)
        if declared is not None:
            (temporary / 'keyboard').write_text(declared)
        return subprocess.run(['bash', '-c', f"""
            set -euo pipefail
            log() {{ echo "log: $*" >> {temporary}/log; }}
            warn() {{ echo "warn: $*" >> {temporary}/log; }}
            AURORA_VCONSOLE_CONF={temporary}/vconsole.conf
            AURORA_X11_KEYBOARD_CONF={temporary}/00-keyboard.conf
            AURORA_DECLARED_KEYBOARD={temporary}/keyboard
            AURORA_KBD_MODEL_MAP={temporary}/kbd-model-map
            AURORA_INITRD_DIR={temporary}/boot
            AURORA_RUN_DIR={temporary}
            journal={temporary}/encrypt.state
            {extra}
            {between('# BEGIN keyboard', '# END keyboard')}
            {action}"""], capture_output=True, text=True)

    def test_keymaps_are_compared_with_the_layout_they_type(self):
        with tempfile.TemporaryDirectory() as temporary:
            for layout, variant, keymap, expected in [
                    ('us', '', 'us', 0), ('de', '', 'de', 0), ('de', 'nodeadkeys', 'de-latin1-nodeadkeys', 0),
                    ('de', '', 'de-latin1-nodeadkeys', 0), ('gb', '', 'uk', 0), ('de', '', 'us', 1),
                    ('us', 'dvorak', 'us', 1), ('us', 'dvorak', 'dvorak', 0), ('fr', '', 'fr-latin9', 0),
                    ('fr', 'bepo', 'fr-bepo', 0), ('us', '', 'de', 1),
                    ('de', '', '/nix/store/0000-xkb-console-keymap', 0)]:
                with self.subTest(layout=layout, variant=variant, keymap=keymap):
                    result = self.run_keyboard(temporary, f"keymap_matches '{layout}' '{variant}' '{keymap}'")
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_the_desktop_layout_is_read_where_localed_keeps_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            show = 'desktop_keyboard; echo "$kb_layout/$kb_variant"'
            # systemd 255 and later: vconsole.conf.
            result = self.run_keyboard(temporary, show,
                                       vconsole='KEYMAP=us\nXKBLAYOUT="de,us"\nXKBVARIANT=nodeadkeys,\n')
            self.assertEqual(result.stdout, 'de/nodeadkeys\n', result.stderr)
            # Older systemd and Xorg: 00-keyboard.conf.
            result = self.run_keyboard(temporary, show, xorg='Section "InputClass"\n  Option "XkbLayout" "fr"\n'
                                       '  Option "XkbVariant" "bepo"\nEndSection\n')
            self.assertEqual(result.stdout, 'fr/bepo\n', result.stderr)
            # A distribution with a read-only localed declares it (NixOS).
            result = self.run_keyboard(temporary, show, declared='layout=gb\nvariant=\n')
            self.assertEqual(result.stdout, 'gb/\n', result.stderr)
            (Path(temporary) / '00-keyboard.conf').unlink()
            (Path(temporary) / 'keyboard').unlink()
            self.assertEqual(self.run_keyboard(temporary, 'desktop_keyboard').returncode, 1)

    def test_an_image_default_console_keymap_follows_the_desktop(self):
        for vconsole in ('KEYMAP=us\nXKBLAYOUT=de\n', 'XKBLAYOUT=de\n'):
            with self.subTest(vconsole=vconsole), tempfile.TemporaryDirectory() as temporary:
                stub = (f'localectl() {{ [[ $1 == --no-convert && $2 == set-keymap ]]; echo "localectl $3" >> {temporary}/log; '
                        f'printf "KEYMAP=%s\\nXKBLAYOUT=de\\n" "$3" > {temporary}/vconsole.conf; }}')
                result = self.run_keyboard(temporary, 'follow_desktop_keyboard', vconsole=vconsole, extra=stub)
                self.assertEqual(result.returncode, 0, result.stderr)
                log = (Path(temporary) / 'log').read_text()
                self.assertIn('localectl de\n', log)
                self.assertNotIn('warn:', log)
                self.assertIn('KEYMAP=de', (Path(temporary) / 'vconsole.conf').read_text())

    def test_a_chosen_console_keymap_is_kept_and_a_difference_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_keyboard(temporary, 'follow_desktop_keyboard', vconsole='KEYMAP=fr\nXKBLAYOUT=de\n',
                                       extra=f'localectl() {{ echo called >> {temporary}/log; }}')
            self.assertEqual(result.returncode, 0, result.stderr)
            log = (Path(temporary) / 'log').read_text()
            self.assertNotIn('called', log)
            self.assertIn('warn: the desktop keyboard layout (de) differs from the console keymap (fr)', log)
            self.assertEqual((Path(temporary) / 'vconsole.conf').read_text(), 'KEYMAP=fr\nXKBLAYOUT=de\n')

    def test_a_matching_keyboard_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_keyboard(temporary, 'follow_desktop_keyboard', vconsole='KEYMAP=us\nXKBLAYOUT=us\n',
                                       extra=f'localectl() {{ echo called >> {temporary}/log; }}')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(temporary) / 'log').exists())

    def test_initrds_are_rebuilt_only_when_their_keymap_is_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            boot = temporary / 'boot'
            boot.mkdir()
            for name in ('initramfs-7.3.0.img', 'initramfs-0-rescue-abc.img'):
                (boot / name).write_text(name)
            (temporary / 'encrypt.state').write_text('format=1\nphase=finished\n')
            stubs = f'''flock() {{ :; }}
                lsinitrd() {{ [[ $1 == *7.3.0* ]] && cat {temporary}/initrd-vconsole; }}
                dracut() {{ echo "dracut $*" >> {temporary}/log; }}'''
            (temporary / 'initrd-vconsole').write_text('KEYMAP="de"\n')
            result = self.run_keyboard(temporary, 'sync_initrd_keymap', vconsole='KEYMAP=de\n', extra=stubs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('dracut', (temporary / 'log').read_text() if (temporary / 'log').exists() else '')
            result = self.run_keyboard(temporary, 'sync_initrd_keymap', vconsole='KEYMAP=fr\n', extra=stubs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('dracut --regenerate-all --force', (temporary / 'log').read_text())
            # An unencrypted install has no disk password prompt.
            (temporary / 'log').unlink()
            (temporary / 'encrypt.state').write_text('format=1\nphase=declined\n')
            self.run_keyboard(temporary, 'sync_initrd_keymap', vconsole='KEYMAP=fr\n', extra=stubs)
            self.assertFalse((temporary / 'log').exists())

    def test_the_initrd_follows_vconsole_conf(self):
        path = (ROOT / 'systemd/aurora-initrd-keymap.path').read_text()
        self.assertIn('PathChanged=/etc/vconsole.conf', path)
        self.assertIn('WantedBy=paths.target', path)
        service = (ROOT / 'systemd/aurora-initrd-keymap.service').read_text()
        self.assertIn('ExecStart=/usr/lib/aurora/aurora-firstboot --sync-initrd-keymap', service)
        self.assertIn('After=systemd-vconsole-setup.service',
                      (ROOT / 'systemd/aurora-initrd-root.service').read_text())
        rekey = between('rekey_from_owner_password() {', '# Ask twice for the disk password')
        # The keyboard is checked before the password is enrolled.
        self.assertLess(rekey.index('follow_desktop_keyboard'), rekey.index('protect_disk "$password"'))
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(['make', '-s', 'install-common', 'DESTDIR=' + temporary], cwd=ROOT, check=True)
            units = Path(temporary) / 'usr/lib/systemd/system'
            self.assertTrue((units / 'aurora-initrd-keymap.path').is_file())
            self.assertTrue((units / 'aurora-initrd-keymap.service').is_file())


class TemporaryKeyTests(unittest.TestCase):
    def test_the_password_slot_is_proven_around_the_temporary_slot_removal(self):
        protect = between('protect_disk() {', "# Arch's sudoers")
        first_test = protect.index('--key-slot 1 --key-file=- "$device" || fail')
        kill = protect.index('luksKillSlot')
        proven = protect.index("fail 'Unlock failed after temporary-key removal'")
        self.assertLess(first_test, kill)
        self.assertLess(kill, proven)
        self.assertLess(proven, protect.index('retire_key_files'))
        self.assertIn('[[ $slot != 1 ]] || fail', protect)
        self.assertLess(protect.index('retire_key_files'), protect.index('> "$state/disk-rekeyed"'))

    def test_every_copy_of_the_temporary_key_is_retired(self):
        retire = between('retire_key_files() {', '# The key slot a key file opens')
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            state, omarchy = temporary / 'state', temporary / 'boot/omarchy'
            state.mkdir()
            omarchy.mkdir(parents=True)
            for path in (state / 'luks-key', omarchy / 'luks-key', omarchy / 'luks-header-before-recovery.bin'):
                path.write_bytes(b'k' * 64)
            (omarchy / 'encrypt.state').write_text('format=1\n')
            subprocess.run(['bash', '-c', f"""
                set -euo pipefail
                state={state}; boot_dir={temporary}/boot
                boot_key=$boot_dir/omarchy/luks-key
                header_backup=$boot_dir/omarchy/luks-header-before-recovery.bin
                {retire}
                retire_key_files"""], check=True)
            self.assertEqual(sorted(p.name for p in omarchy.iterdir()), ['encrypt.state'])
            self.assertEqual(list(state.iterdir()), [])


class DeclaredHookTests(unittest.TestCase):
    def retire(self, temporary, owner_text):
        temporary = Path(temporary)
        owner = temporary / 'owner.nix'
        owner.write_text(owner_text)
        (temporary / 'declared').write_text('sddm sddm-autologin gdm-password\n')
        subprocess.run(['bash', '-c', f"""
            set -euo pipefail
            log() {{ echo "$*" >> {temporary}/log; }}
            declared_hooks={temporary}/declared
            AURORA_NIXOS_OWNER={owner}
            {between('nixos_owner=', '# aurora-disk-rekey.service: enroll')}
            retire_declared_hooks; retire_declared_hooks"""], check=True)
        return owner.read_text()

    def test_the_factory_owner_configuration_switches_the_hooks_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            text = self.retire(temporary, '{ ... }: {}\n')
            self.assertEqual(text.count('aurora.firstboot.passwordHooks = false;'), 1)
            self.assertTrue(text.startswith('# Aurora image machine configuration\n{ ... }: {\n'))
            self.assertTrue(text.endswith('\n}\n'))
        with tempfile.TemporaryDirectory() as temporary:
            edited = '{ ... }: { services.openssh.enable = true; }\n'
            self.assertEqual(self.retire(temporary, edited), edited)
            self.assertIn('add aurora.firstboot.passwordHooks = false;', (Path(temporary) / 'log').read_text())

    def test_the_text_wizard_switches_the_hooks_off_once_the_disk_is_rekeyed(self):
        text = between('setup_text() {', '# Each attempt runs as its own process')
        self.assertLess(text.index('protect_disk "$password"'), text.index('! disk_rekeyed || hooks='))
        self.assertIn('networking.hostName = "$hostname";$hooks', text)

    def test_rekeyed_hooks_do_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            state = temporary / 'state'
            state.mkdir()
            (state / 'desktop').write_text('desktop\n')
            (state / 'luks-key').write_bytes(b'k' * 64)
            (state / 'disk-rekeyed').write_text('rekeyed\n')
            for arguments in ('--gate', ''):
                result = subprocess.run(['bash', '-c', f"""
                    set -euo pipefail
                    getent() {{ echo 'alice:x:1000:1000::/home/alice:/bin/bash'; }}
                    systemctl() {{ echo started >> {temporary}/started; }}
                    log() {{ :; }}
                    state={state}
                    AURORA_REKEY_DIR={temporary}/rekey
                    {between('pam_dir=', '# Ask twice for the disk password')}
                    PAM_USER=alice PAM_TYPE=auth password_hook {arguments}"""], input=b'right\0')
                self.assertEqual(result.returncode, 0)
            self.assertFalse((temporary / 'started').exists())
            self.assertFalse((temporary / 'rekey').exists())


if __name__ == '__main__':
    unittest.main()
