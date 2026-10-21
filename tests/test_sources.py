import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SourceTests(unittest.TestCase):
    def test_captured_converter_identity(self):
        provenance = json.loads((ROOT / 'vendor/provenance.json').read_text())
        self.assertEqual(hashlib.sha256((ROOT / 'vendor/omarchy-mac-encrypt').read_bytes()).hexdigest(), provenance['file_sha256'])

    def test_installation_is_staged_without_machine_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(['make', '-s', 'install', 'DESTDIR=' + temporary], cwd=ROOT, check=True)
            staged = Path(temporary)
            self.assertTrue((staged / 'usr/lib/aurora/aurora-initrd-root').is_file())
            self.assertTrue((staged / 'usr/lib/dracut/modules.d/97aurora-root/module-setup.sh').is_file())
            mapper = staged / 'usr/lib/aurora/initrd/aurora-root-mapper.conf'
            self.assertIn('JobRunningTimeoutSec=infinity', mapper.read_text())
            service = staged / 'usr/lib/aurora/initrd/aurora-initrd-root.service'
            self.assertIn('ExecStartPre=-/usr/bin/modprobe apple_wdt', service.read_text())
            self.assertFalse((staged / 'etc/aurora/image-layout').exists())
            self.assertFalse((staged / 'var/lib/aurora/firstboot/pending').exists())

if __name__ == '__main__':
    unittest.main()
