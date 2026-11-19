import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'cec/aurora-cec'

# A stand-in for cec-ctl: logs its arguments, answers -x/-l/-L from files.
FAKE_CTL = r'''#!/bin/bash
state=${FAKE_CEC_STATE:?}
echo "$*" >> "$state/calls"
for arg in "$@"; do
  case $arg in
    -x) cat "$state/pa" ;;
    -l) [[ -s $state/la ]] && cat "$state/la" ;;
    -L) [[ -s $state/la ]] && cat "$state/la" ;;
    --playback) echo 4 > "$state/la" ;;
    --request-active-source)
      [[ -f $state/hang ]] && sleep 5
      if [[ -s $state/active ]]; then
        printf '    Received from TV (0):\n    ACTIVE_SOURCE (0x82):\n\tphys-addr: %s\n' "$(cat "$state/active")"
      fi ;;
    --image-view-on|--standby) [[ -f $state/nack ]] && exit 1 ;;
  esac
done
exit 0
'''


class AuroraCec(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.state = base / 'state'
        self.state.mkdir()
        (self.state / 'pa').write_text('1.0.0.0\n')
        (self.state / 'la').write_text('')
        self.sysfs = base / 'sys'
        self.dev = base / 'dev'
        self.dev.mkdir()
        self.ctl = base / 'cec-ctl'
        self.ctl.write_text(FAKE_CTL)
        self.ctl.chmod(0o755)
        model = self.sysfs / 'firmware/devicetree/base'
        model.mkdir(parents=True)
        (model / 'model').write_bytes(b'Apple Mac mini (M2 Pro, 2023)\0')

    def tearDown(self):
        self.tmp.cleanup()

    def adapter(self, name, driver):
        parent = self.sysfs / 'devices/platform' / f'{name}.parent'
        (parent / name).mkdir(parents=True)
        drv = self.sysfs / 'bus/platform/drivers' / driver
        drv.mkdir(parents=True, exist_ok=True)
        (parent / 'driver').symlink_to(drv)
        bus = self.sysfs / 'bus/cec/devices'
        bus.mkdir(parents=True, exist_ok=True)
        (bus / name).symlink_to(parent / name)
        (self.dev / name).touch()

    def run_cec(self, *args):
        env = dict(os.environ, AURORA_CEC_SYSFS=str(self.sysfs), AURORA_CEC_DEV=str(self.dev),
                   AURORA_CEC_CTL=str(self.ctl), AURORA_CEC_WAKE_WAIT='1', FAKE_CEC_STATE=str(self.state))
        return subprocess.run(['bash', str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=20)

    def calls(self):
        path = self.state / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def test_wake_turns_tv_on_and_selects_mac(self):
        self.adapter('cec0', 'apple-dcp')
        r = self.run_cec('wake', 'cec0')
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertTrue(any('--playback --osd-name Mac mini' in c for c in calls), calls)
        self.assertTrue(any('--to 0 --image-view-on' in c for c in calls), calls)
        self.assertTrue(any('--to 15 --active-source phys-addr=1.0.0.0' in c for c in calls), calls)

    def test_wake_keeps_existing_configuration(self):
        self.adapter('cec0', 'apple-dcp')
        (self.state / 'la').write_text('8\n')
        self.assertEqual(self.run_cec('wake', 'cec0').returncode, 0)
        self.assertFalse(any('--playback' in c for c in self.calls()))

    def test_no_tv_is_quiet(self):
        self.adapter('cec0', 'apple-dcp')
        (self.state / 'pa').write_text('f.f.f.f\n')
        r = self.run_cec('wake', 'cec0')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        self.assertFalse(any('--image-view-on' in c for c in self.calls()))

    def test_tv_without_cec_is_quiet(self):
        self.adapter('cec0', 'apple-dcp')
        (self.state / 'nack').touch()
        r = self.run_cec('wake', 'cec0')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))

    def test_other_adapters_untouched(self):
        self.adapter('cec0', 'pulse8-cec')
        self.assertEqual(self.run_cec('wake', 'cec0').returncode, 0)
        self.assertEqual(self.run_cec('suspend').returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_no_adapter(self):
        r = self.run_cec('suspend')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        self.assertEqual(self.run_cec('wake', 'cec0').returncode, 0)

    def test_suspend_sends_standby(self):
        self.adapter('cec0', 'apple-dcp')
        (self.state / 'la').write_text('4\n')
        self.assertEqual(self.run_cec('suspend').returncode, 0)
        self.assertTrue(any('--to 0 --standby' in c for c in self.calls()))

    def test_suspend_leaves_tv_on_for_other_source(self):
        self.adapter('cec0', 'apple-dcp')
        (self.state / 'la').write_text('4\n')
        (self.state / 'active').write_text('2.0.0.0')
        self.assertEqual(self.run_cec('suspend').returncode, 0)
        self.assertFalse(any('--standby' in c for c in self.calls()))

    def test_suspend_is_bounded(self):
        self.adapter('cec0', 'apple-dcp')
        (self.state / 'la').write_text('4\n')
        (self.state / 'hang').touch()
        start = time.monotonic()
        self.assertEqual(self.run_cec('suspend').returncode, 0)
        self.assertLess(time.monotonic() - start, 1.5)


if __name__ == '__main__':
    unittest.main()
