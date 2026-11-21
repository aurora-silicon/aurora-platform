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

    def test_debian_profiles_use_dracut_and_their_initrd_name(self):
        text = FINALIZE.read_text()
        self.assertIn('debian-kde|debian-gnome|debian-minimal) distribution=debian backend=dracut;;', text)
        self.assertIn('initrd=initrd.img-KERNEL', text)
        self.assertIn("dpkg-query -W -f '${Package} ${Version} ${Architecture}\\n'", text)
        # SELinux labelling and Anaconda's initial setup are Fedora's alone.
        fedora_only = text[text.index('if [[ $distribution == fedora ]]; then\n    chroot "$scratch/root" /bin/bash'):]
        self.assertIn('label-fedora-rootfs', fedora_only.split('fi', 1)[0])


if __name__ == '__main__':
    unittest.main()
