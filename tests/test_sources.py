import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SourceTests(unittest.TestCase):
    def test_captured_converter_identity(self):
        provenance = json.loads((ROOT / 'vendor/provenance.json').read_text())
        self.assertEqual(hashlib.sha256((ROOT / 'vendor/omarchy-mac-encrypt').read_bytes()).hexdigest(), provenance['file_sha256'])

    def test_the_encrypted_root_passes_discards(self):
        converter = (ROOT / 'vendor/omarchy-mac-encrypt').read_text()
        self.assertIn("printf 'root UUID=%s none luks,discard\\n'", converter)
        self.assertIn('cryptsetup open --batch-mode --allow-discards "${persistent[@]}"', converter)
        self.assertIn('[[ ${STATE_PHASE:-} != encrypted ]] || persistent=(--persistent)', converter)
        unlock = (ROOT / 'libexec/aurora-initrd-root').read_text()
        self.assertIn('cryptsetup open --allow-discards --key-file=- "$ROOT_DEV" root', unlock)

    def test_installation_is_staged_without_machine_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(['make', '-s', 'install', 'DESTDIR=' + temporary], cwd=ROOT, check=True)
            staged = Path(temporary)
            self.assertTrue((staged / 'usr/lib/aurora/aurora-initrd-root').is_file())
            self.assertTrue((staged / 'usr/lib/dracut/modules.d/97aurora-root/module-setup.sh').is_file())
            budget = staged / 'usr/libexec/aurora/aurora-power-budget'
            self.assertTrue(budget.is_file() and os.access(budget, os.X_OK))
            unit = (staged / 'usr/lib/systemd/system/aurora-power-budget.service').read_text()
            self.assertIn('ExecStart=/usr/libexec/aurora/aurora-power-budget', unit)
            self.assertNotIn('ProtectKernelTunables', unit)  # it writes cpufreq limits in /sys
            modes = staged / 'usr/libexec/aurora/aurora-power-modes'
            self.assertTrue(modes.is_file() and os.access(modes, os.X_OK))
            unit = (staged / 'usr/lib/systemd/system/aurora-power-modes.service').read_text()
            self.assertIn('ExecStart=/usr/libexec/aurora/aurora-power-modes', unit)
            self.assertNotIn('ProtectKernelTunables', unit)  # it writes /proc/sys/kernel and /sys
            mapper = staged / 'usr/lib/aurora/initrd/aurora-root-mapper.conf'
            self.assertIn('JobRunningTimeoutSec=infinity', mapper.read_text())
            service = staged / 'usr/lib/aurora/initrd/aurora-initrd-root.service'
            self.assertIn('ExecStartPre=-/usr/bin/modprobe apple_wdt', service.read_text())
            self.assertFalse((staged / 'etc/aurora/image-layout').exists())
            self.assertFalse((staged / 'var/lib/aurora/firstboot/pending').exists())

if __name__ == '__main__':
    unittest.main()
