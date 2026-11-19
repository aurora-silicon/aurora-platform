"""The Secure Enclave disk password at first boot: when it is used, and the
recovery key's form. The enclave itself is qualified on hardware; the LUKS2
side runs in tests/loop-rekey."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIRSTBOOT = (ROOT / 'libexec/aurora-firstboot').read_text()
INITRD = (ROOT / 'libexec/aurora-initrd-root').read_text()
POLICY = 'xart_writes={w} provision_keybag=1 xart_guard={g} trusted_keys={t} filevault_api=1 xarm_trace=0 refkey_v2=1 identity_override=1\n'
PARM = 'keystore_only:Key-store-only attach (1); also per-user key bags (2) (int)'


def section(text, start, end):
    return text[text.index(start):text.index(end)]


class CapabilityTests(unittest.TestCase):
    def capable(self, keystore='open', w=1, g=0, t=1, parm=PARM, tool=True):
        functions = section(FIRSTBOOT, 'sep_fde=${AURORA_SEP_FDE', '# Count the disk')
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            diag = temporary / 'sys/1234.sep/diag'
            diag.mkdir(parents=True)
            (diag / 'keystore').write_text(keystore + '\n')
            (diag / 'policy').write_text(POLICY.format(w=w, g=g, t=t))
            fde = temporary / 'aurora-sep-fde'
            if tool:
                fde.write_text('#!/bin/sh\n')
                fde.chmod(0o755)
            return subprocess.run(['bash', '-c', f"""
                set -euo pipefail
                log() {{ :; }}
                state={temporary}
                AURORA_SEP_FDE={fde} AURORA_SEP_DEVICES={temporary / 'sys'} AURORA_SEP_KEYSTORE_WAIT=1
                {functions}
                sep_modinfo() {{ printf '%s\\n' '{parm}'; }}
                sep_root_capable"""]).returncode == 0

    def test_an_enclave_that_can_open_a_user_bag_in_the_initrd(self):
        self.assertTrue(self.capable())

    def test_every_missing_piece_keeps_the_passphrase(self):
        self.assertFalse(self.capable(g=1))           # xART guard (J616s, M2 today)
        self.assertFalse(self.capable(g=2))
        self.assertFalse(self.capable(w=0))           # enclave writes off
        self.assertFalse(self.capable(t=0))           # no trusted keys
        self.assertFalse(self.capable(keystore='closed'))
        self.assertFalse(self.capable(keystore='unknown'))
        self.assertFalse(self.capable(parm='keystore_only:Key-store-only attach (int)'))
        self.assertFalse(self.capable(tool=False))    # aurora-sep-luks not installed

    def test_no_mac_model_decides_it(self):
        functions = section(FIRSTBOOT, '# ---- Secure Enclave disk password', '# Arch\'s sudoers')
        for model in ('j700', 'j616', 'j413', 't8112', 't6000', 'compatible'):
            self.assertNotIn(model, functions.lower())


class RecoveryKeyTests(unittest.TestCase):
    def test_the_key_has_aurora_sep_fdes_form(self):
        functions = section(FIRSTBOOT, 'recovery_alphabet=', 'add_recovery_key() {')
        keys = {subprocess.run(['bash', '-c', functions + 'new_recovery_key'], capture_output=True,
                               text=True, check=True).stdout for _ in range(5)}
        self.assertEqual(len(keys), 5)
        for key in keys:
            self.assertRegex(key, r'^([A-HJ-NP-Z2-9]{4}-){5}[A-HJ-NP-Z2-9]{4}$')

    def test_the_unlock_prompt_accepts_it_typed_loosely(self):
        function = section(INITRD, 'aurora_recovery_form() {', '# A root sealed by the Secure Enclave')

        def form(text):
            result = subprocess.run(['bash', '-c', function + 'aurora_recovery_form "$1" && printf %s "$AURORA_RECOVERY_FORM"',
                                     '-', text], capture_output=True, text=True)
            return result.stdout if result.returncode == 0 else None

        self.assertEqual(form('abcd efgh jkmn pqrs tuvw xyz2'), 'ABCD-EFGH-JKMN-PQRS-TUVW-XYZ2')
        self.assertEqual(form('ABCDEFGHJKMNPQRSTUVWXYZ2'), 'ABCD-EFGH-JKMN-PQRS-TUVW-XYZ2')
        self.assertIsNone(form('ABCD-EFGH-JKMN-PQRS-TUVW-XYZ2'))   # already exact: tried as typed
        self.assertIsNone(form('a disk password'))
        self.assertIsNone(form('ABCDEFGHIJKMNPQRSTUVWXYZ'))      # I is not in the alphabet

    def test_it_is_shown_once_and_every_copy_goes(self):
        helper = (ROOT / 'libexec/aurora-recovery-key').read_text()
        self.assertIn('It is shown only once.', helper)
        retire = section(helper, 'retire() {', 'case ${1:-} in')
        for step in ('shred', 'rm -rf -- "$handoff"', '> "$shown"'):
            self.assertIn(step, retire)
        units = {name: (ROOT / 'systemd' / name).read_text() for name in (
            'aurora-recovery-key.service', 'aurora-recovery-key-done.path')}
        self.assertIn('ConditionPathExists=!/var/lib/aurora/firstboot/recovery-key-shown',
                      units['aurora-recovery-key.service'])
        self.assertIn('PathExists=/run/aurora-recovery-key/acknowledged', units['aurora-recovery-key-done.path'])


class HeaderAndUnlockTests(unittest.TestCase):
    def test_conversion_formats_256k_metadata(self):
        self.assertIn('AURORA_LUKS2_METADATA_SIZE=${AURORA_LUKS2_METADATA_SIZE:-256k}', INITRD)
        self.assertIn('reencrypt --encrypt --luks2-metadata-size "$AURORA_LUKS2_METADATA_SIZE"', INITRD)

    def test_a_sealed_root_opens_through_systemd_cryptsetup_after_the_enclave_driver(self):
        self.assertLess(INITRD.index('if aurora_sep_sealed; then'), INITRD.index("'Unlock Aurora root:'"))
        self.assertIn('attach root "$ROOT_DEV" - luks,tries=0', INITRD)
        self.assertIn('After=aurora-sep-initrd.service', (ROOT / 'systemd/aurora-initrd-root.service').read_text())

    def test_the_seal_removes_the_temporary_key_last(self):
        seal = section(FIRSTBOOT, 'seal_disk() {', '# The owner\'s password protects the disk')
        self.assertLess(seal.index('enroll --user'), seal.index('recovery "${replace[@]}"'))
        self.assertLess(seal.index('recovery "${replace[@]}"'), seal.index('remove-passphrase'))
        self.assertLess(seal.index('remove-passphrase'), seal.index('retire_key_files'))


if __name__ == '__main__':
    unittest.main()
