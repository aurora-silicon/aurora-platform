import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / 'libexec/aurora-growfs-needed'


def usage(*slack):
    """btrfs device usage -b output with one device per slack value."""
    return ''.join(f'/dev/mapper/root, ID: {n}\n   Device size: 38835060736\n'
                   f'   Device slack: {s}\n   Unallocated: 30000000000\n\n'
                   for n, s in enumerate(slack, 1))


class GrowfsGateTests(unittest.TestCase):
    def run_gate(self, fstype, output, btrfs_status=0, mountpoint='/'):
        with tempfile.TemporaryDirectory() as temporary:
            bindir = Path(temporary)
            (bindir / 'findmnt').write_text(f'#!/bin/sh\necho {fstype}\n')
            (bindir / 'btrfs').write_text(
                f'#!/bin/sh\n[ "$1 $2 $3 $4" = "device usage -b {mountpoint}" ] || exit 9\n'
                f'cat <<\'EOF\'\n{output}EOF\nexit {btrfs_status}\n')
            for tool in ('findmnt', 'btrfs'):
                (bindir / tool).chmod(0o755)
            env = dict(os.environ, PATH=f'{bindir}:{os.environ["PATH"]}')
            return subprocess.run(['bash', str(GATE), mountpoint], env=env,
                                  capture_output=True, text=True).returncode

    def test_grows_a_filesystem_smaller_than_its_device(self):
        # A plaintext install: the image filesystem in a larger partition.
        self.assertEqual(self.run_gate('btrfs', usage(25131220992)), 0)
        self.assertEqual(self.run_gate('btrfs', usage(0, 1 << 30), mountpoint='/home'), 0)

    def test_skips_a_filesystem_that_fills_its_device(self):
        # ExecCondition: 1-254 skips the unit instead of failing it.
        self.assertEqual(self.run_gate('btrfs', usage(0)), 1)
        self.assertEqual(self.run_gate('btrfs', usage(4096)), 1)

    def test_leaves_what_it_cannot_measure_to_systemd_growfs(self):
        self.assertEqual(self.run_gate('ext4', ''), 0)
        self.assertEqual(self.run_gate('btrfs', 'ERROR: not a btrfs filesystem\n', btrfs_status=1), 0)

    def test_drop_ins_gate_both_growfs_units(self):
        root = (ROOT / 'systemd/aurora-growfs-root.conf').read_text()
        template = (ROOT / 'systemd/aurora-growfs.conf').read_text()
        self.assertIn('ExecCondition=/usr/lib/aurora/aurora-growfs-needed /\n', root)
        self.assertIn('ExecCondition=/usr/lib/aurora/aurora-growfs-needed %f\n', template)
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(['make', '-s', 'install-common', 'DESTDIR=' + temporary], cwd=ROOT, check=True)
            units = Path(temporary) / 'usr/lib/systemd/system'
            self.assertEqual((units / 'systemd-growfs-root.service.d/aurora-growfs.conf').read_text(), root)
            self.assertEqual((units / 'systemd-growfs@.service.d/aurora-growfs.conf').read_text(), template)
            gate = Path(temporary) / 'usr/lib/aurora/aurora-growfs-needed'
            self.assertTrue(os.access(gate, os.X_OK))


if __name__ == '__main__':
    unittest.main()
