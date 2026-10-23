from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UUID = '5b3ccad8-7145-4d61-a30a-14cd68e586ff'
UUID_UNIT = r'dev-disk-by\x2duuid-5b3ccad8\x2d7145\x2d4d61\x2da30a\x2d14cd68e586ff.device'
LABEL_UNIT = r'dev-disk-by\x2dlabel-AURORA_ROOT.device'


class InitrdHookTests(unittest.TestCase):
    """The root= device must wait as long as the unlock prompt (no 90 s device timeout)."""

    def run_hook(self, script):
        with tempfile.TemporaryDirectory() as temporary:
            layout = Path(temporary) / 'image-layout'
            layout.write_text(f'format=1\nroot_uuid={UUID}\nboot_uuid=2025cecd-807c-5835-9ad4-e547cfdeb279\nroot_subvolume=root\n')
            env = {**os.environ, 'AURORA_IMAGE_LAYOUT': str(layout),
                   'AURORA_INITRD_ROOT': str(ROOT / 'libexec/aurora-initrd-root')}
            result = subprocess.run(['bash', '-c', script], env=env, check=True, text=True,
                                    stdout=subprocess.PIPE)
            return result.stdout.splitlines()

    def test_mkinitcpio_hook_bounds_no_root_device_wait(self):
        calls = self.run_hook(f'''
            add_binary() {{ :; }}; add_module() {{ :; }}; add_symlink() {{ :; }}
            add_file() {{ printf '%s|%s\\n' "$1" "${{2:-$1}}"; }}
            source {ROOT / 'mkinitcpio/aurora-root'}; build''')
        mapper = '/usr/lib/aurora/initrd/aurora-root-mapper.conf'
        for unit in (UUID_UNIT, LABEL_UNIT, 'dev-mapper-root.device'):
            self.assertIn(f'{mapper}|/usr/lib/systemd/system/{unit}.d/aurora-root.conf', calls)
        self.assertTrue(any(call.endswith('|/etc/aurora/image-layout') for call in calls))

    def test_dracut_module_bounds_no_root_device_wait(self):
        calls = self.run_hook(f'''
            systemdsystemunitdir=/usr/lib/systemd/system; initdir=$(mktemp -d)
            inst_binary() {{ :; }}; inst_script() {{ :; }}; ln_r() {{ :; }}
            inst_simple() {{ printf '%s|%s\\n' "$1" "${{2:-$1}}"; }}
            source {ROOT / 'dracut/97aurora-root/module-setup.sh'}; install; rm -rf "$initdir"''')
        mapper = '/usr/lib/aurora/initrd/aurora-root-mapper.conf'
        for unit in (UUID_UNIT, LABEL_UNIT, 'dev-mapper-root.device'):
            self.assertIn(f'{mapper}|/usr/lib/systemd/system/{unit}.d/aurora-root.conf', calls)
        self.assertTrue(any(call.endswith('|/etc/aurora/image-layout') for call in calls))

    def test_layout_without_root_uuid_is_rejected(self):
        with tempfile.NamedTemporaryFile('w') as layout:
            layout.write('format=1\n')
            layout.flush()
            result = subprocess.run([str(ROOT / 'libexec/aurora-initrd-root'), '--root-device-units', layout.name],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
