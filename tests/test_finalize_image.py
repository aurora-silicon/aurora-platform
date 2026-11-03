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


if __name__ == '__main__':
    unittest.main()
