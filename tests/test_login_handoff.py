import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LoginHandoffTests(unittest.TestCase):
    def test_configured_graphical_login_wins_over_conflicting_text_login(self):
        self.check('graphical.target', True, 'display-manager.service')

    def test_minimal_image_keeps_text_login_even_with_an_installed_display_manager(self):
        self.check('multi-user.target', True, 'getty@tty1.service')

    def test_missing_display_manager_falls_back_to_text_login(self):
        self.check('graphical.target', False, 'getty@tty1.service')

    def check(self, target, display_manager, expected):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = root / 'systemctl'
            stub.write_text('''#!/bin/bash
case "$1" in
  get-default) printf '%s\\n' "$TEST_DEFAULT" ;;
  cat) [[ $TEST_DM == yes ]] ;;
  start) printf '%s\\n' "$@" > "$TEST_CALL" ;;
  *) exit 99 ;;
esac
''')
            stub.chmod(0o755)
            record = root / 'called'
            env = dict(os.environ, PATH=temporary + ':' + os.environ['PATH'],
                       TEST_DEFAULT=target, TEST_DM='yes' if display_manager else 'no',
                       TEST_CALL=str(record))
            subprocess.run(['bash', str(ROOT / 'libexec/aurora-firstboot-handoff')],
                           check=True, env=env)
            self.assertEqual(record.read_text().splitlines(), ['start', '--no-block', expected])
