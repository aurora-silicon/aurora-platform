"""aurora-charge-full-now's busctl layer against a mock UPower on a private system bus.

Needs python-dbusmock and dbus-daemon; skipped without them.  The unit tests
in test_charge_full_now.py cover the policy with a fake bus layer.
"""

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

try:
    import dbus
    import dbusmock
except ImportError:
    dbusmock = None

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('charge_full_now', str(ROOT / 'power/aurora-charge-full-now'))
spec = importlib.util.spec_from_loader('charge_full_now', loader)
cf = importlib.util.module_from_spec(spec)
loader.exec_module(cf)

MAIN = 'org.freedesktop.UPower'
DEVICE = 'org.freedesktop.UPower.Device'

# EnableChargeThreshold as UPower 1.91 implements it: it records the state and
# changes ChargeThresholdEnabled, which emits PropertiesChanged.
ENABLE = ("self.UpdateProperties('%s', {'ChargeThresholdEnabled': dbus.Boolean(args[0])}); "
          "self.UpdateProperties('%s', {'State': dbus.UInt32(1 if not args[0] else 5)})" % (DEVICE, DEVICE))

Base = dbusmock.DBusTestCase if dbusmock else unittest.TestCase


@unittest.skipUnless(dbusmock and shutil.which('dbus-daemon') and shutil.which('busctl'),
                     'needs python-dbusmock, dbus-daemon and busctl')
class MockUPower(Base):
    @classmethod
    def setUpClass(cls):
        cls.start_system_bus()      # sets DBUS_SYSTEM_BUS_ADDRESS, which busctl --system follows
        cls.dbus_con = cls.get_dbus(system_bus=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = os.path.join(self.tmp.name, 'charge-full-now', 'active')
        self.p_mock, self.obj = self.spawn_server_template('upower', {'OnBattery': False},
                                                           stdout=subprocess.DEVNULL)
        self.addCleanup(self.p_mock.wait)
        self.addCleanup(self.p_mock.terminate)
        self.mock = dbus.Interface(self.obj, dbusmock.MOCK_IFACE)
        # A battery as UPower 1.91 exports macsmc-battery with the limit on.
        self.path = '/org/freedesktop/UPower/devices/battery_macsmc_battery'
        self.mock.AddObject(self.path, DEVICE, dbus.Dictionary({
            'NativePath': 'macsmc-battery', 'Type': dbus.UInt32(2), 'PowerSupply': True,
            'IsPresent': True, 'Percentage': 80.0, 'State': dbus.UInt32(5),
            'ChargeThresholdSupported': True, 'ChargeThresholdEnabled': True,
            'ChargeStartThreshold': dbus.UInt32(75), 'ChargeEndThreshold': dbus.UInt32(80)}, signature='sv'),
            [('EnableChargeThreshold', 'b', '', ENABLE)])
        self.dev = self.dbus_con.get_object(MAIN, self.path)
        self.bus = cf.UPowerBus()

    def tearDown(self):
        self.bus.close()
        self.tmp.cleanup()

    def set(self, **props):
        self.mock.SetDeviceProperties(self.path, dbus.Dictionary(props, signature='sv'))

    def prop(self, name):
        return self.dev.Get(DEVICE, name, dbus_interface=dbus.PROPERTIES_IFACE)

    def start(self):
        result = {}

        def run():
            result['rc'] = cf.charge_full(self.bus, self.marker, ceiling=120)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.monotonic() + 30
        while self.prop('ChargeThresholdEnabled') and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.prop('ChargeThresholdEnabled'), 'the limit was not lifted')
        self.assertTrue(os.path.exists(self.marker))
        return t, result

    def finish(self, t, result):
        t.join(30)
        self.assertFalse(t.is_alive(), 'the one-shot did not react to the signal')
        self.assertEqual(result['rc'], 0)
        self.assertTrue(self.prop('ChargeThresholdEnabled'))
        self.assertFalse(os.path.exists(self.marker))

    def test_find_battery(self):
        path, p = cf.find_battery(self.bus)
        self.assertEqual(path, self.path)
        self.assertEqual(p['ChargeEndThreshold'], 80)
        self.assertFalse(self.bus.on_battery())

    def test_full_wakes_the_loop(self):
        t, result = self.start()
        self.set(Percentage=100.0, State=dbus.UInt32(4))
        self.finish(t, result)

    def test_unplug_wakes_the_loop(self):
        t, result = self.start()
        self.obj.Set(MAIN, 'OnBattery', True, dbus_interface=dbus.PROPERTIES_IFACE)
        self.finish(t, result)

    def test_sigterm_cancels_through_main(self):
        # "systemctl stop" sends SIGTERM to "aurora-charge-full-now run".
        driver = ('import importlib.machinery, sys; '
                  'm = importlib.machinery.SourceFileLoader("cf", sys.argv[1]).load_module(); '
                  'm.MARKER = sys.argv[2]; sys.exit(m.main(["aurora-charge-full-now", "run"]))')
        child = subprocess.Popen([sys.executable, '-c', driver, str(ROOT / 'power/aurora-charge-full-now'),
                                  self.marker], stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 30
        while self.prop('ChargeThresholdEnabled') and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.prop('ChargeThresholdEnabled'))
        time.sleep(0.5)     # into the wait
        child.send_signal(signal.SIGTERM)
        _, err = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, err)
        self.assertIn('one-shot over: cancelled', err)
        self.assertTrue(self.prop('ChargeThresholdEnabled'))
        self.assertFalse(os.path.exists(self.marker))

    def test_restore_after_reboot(self):
        self.dev.EnableChargeThreshold(False, dbus_interface=DEVICE)
        cf.write_marker({'path': self.path, 'native_path': 'macsmc-battery'}, self.marker)
        self.assertEqual(cf.restore(self.bus, self.marker, settle=5), 0)
        self.assertTrue(self.prop('ChargeThresholdEnabled'))


if __name__ == '__main__':
    unittest.main()
