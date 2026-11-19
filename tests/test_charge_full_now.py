import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('charge_full_now', str(ROOT / 'power/aurora-charge-full-now'))
spec = importlib.util.spec_from_loader('charge_full_now', loader)
cf = importlib.util.module_from_spec(spec)
loader.exec_module(cf)

BAT = '/org/freedesktop/UPower/devices/battery_macsmc_battery'


def battery(**kw):
    p = {'NativePath': 'macsmc-battery', 'Type': 2, 'PowerSupply': True, 'IsPresent': True,
         'Percentage': 80.0, 'State': 5, 'ChargeThresholdSupported': True,
         'ChargeThresholdEnabled': True, 'ChargeStartThreshold': 75, 'ChargeEndThreshold': 80}
    p.update(kw)
    return p


class FakeUPower:
    """UPower as the program sees it: devices, OnBattery, EnableChargeThreshold, signals.

    events is the script of what happens while the program waits: each entry
    runs on one wait() and changes the fake's state, as UPower would before it
    sends PropertiesChanged.
    """

    def __init__(self, devices=None, on_battery=False, events=(), clock=None, marker=None):
        self.devs = dict(devices or {})
        self.ac_gone = on_battery
        self.events = list(events)
        self.clock = clock
        self.marker = marker
        self.calls = []
        self.waits = 0
        self.refuse = set()
        self.watching = False
        self.missing_once = set()

    def devices(self):
        return list(self.devs)

    def props(self, path):
        if path in self.missing_once:
            self.missing_once.discard(path)
            return None
        p = self.devs.get(path)
        return dict(p) if p is not None else None

    def on_battery(self):
        return self.ac_gone

    def enable_threshold(self, path, enabled):
        # The marker must already be on disk when the limit is lifted.
        self.calls.append((path, enabled, os.path.exists(self.marker) if self.marker else None))
        if enabled in self.refuse:
            return False
        p = self.devs[path]
        p['ChargeThresholdEnabled'] = enabled
        if p.get('State') == 5 and not enabled:
            p['State'] = 1
        return True

    def watch(self):
        self.watching = True

    def wait(self, timeout):
        assert self.watching, 'wait() before watch(): a change could be lost'
        self.waits += 1
        if not self.events:
            raise AssertionError('the program waited with nothing left to happen')
        event = self.events.pop(0)
        event(self)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def set_bat(**kw):
    def ev(up):
        up.devs[BAT].update(kw)
    return ev


def unplug(up):
    up.ac_gone = True
    up.devs[BAT]['State'] = 2


def cancel(up):
    raise cf.Cancelled()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = os.path.join(self.tmp.name, 'charge-full-now', 'active')
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def up(self, events=(), **kw):
        devices = kw.pop('devices', {BAT: battery(**kw.pop('bat', {}))})
        return FakeUPower(devices, events=events, clock=self.clock, marker=self.marker, **kw)

    def run_once(self, up, ceiling=cf.CEILING):
        return cf.charge_full(up, self.marker, ceiling=ceiling, clock=self.clock)


class OneShot(Base):
    def test_charges_to_full_then_limit_back(self):
        seen = []

        def check_marker(up):
            info = json.loads(Path(self.marker).read_text())
            seen.append((info['path'], info['end_threshold'], up.devs[BAT]['ChargeThresholdEnabled']))
            set_bat(Percentage=93.0)(up)

        up = self.up([check_marker, set_bat(Percentage=100.0, State=4)])
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual(up.calls, [(BAT, False, True), (BAT, True, True)])
        self.assertEqual(seen, [(BAT, 80, False)])
        self.assertTrue(up.devs[BAT]['ChargeThresholdEnabled'])
        self.assertFalse(os.path.exists(self.marker))

    def test_fully_charged_state_below_100_ends(self):
        up = self.up([set_bat(Percentage=99.0, State=4)])
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual([c[1] for c in up.calls], [False, True])

    def test_unplug_ends_and_limit_back(self):
        up = self.up([set_bat(Percentage=85.0), unplug])
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual([c[1] for c in up.calls], [False, True])
        self.assertFalse(os.path.exists(self.marker))

    def test_cancel_puts_limit_back(self):
        up = self.up([set_bat(Percentage=82.0), cancel])
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual([c[1] for c in up.calls], [False, True])
        self.assertFalse(os.path.exists(self.marker))

    def test_user_turns_limit_back_on(self):
        up = self.up([set_bat(ChargeThresholdEnabled=True)])
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual([c[1] for c in up.calls], [False])     # UPower already has it on
        self.assertFalse(os.path.exists(self.marker))

    def test_ceiling(self):
        def hours(up):
            self.clock.t += 6 * 3600
        up = self.up([hours, hours])
        self.assertEqual(self.run_once(up, ceiling=12 * 3600), 0)
        self.assertEqual([c[1] for c in up.calls], [False, True])
        self.assertEqual(up.waits, 2)

    def test_s2idle_counts_toward_ceiling(self):
        # The clock is CLOCK_BOOTTIME: a long suspend moves it; resume wakes the wait (PrepareForSleep).
        def suspend_13h(up):
            self.clock.t += 13 * 3600
        up = self.up([suspend_13h])
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual([c[1] for c in up.calls], [False, True])

    def test_full_during_s2idle(self):
        def suspend_charged(up):
            self.clock.t += 2 * 3600
            up.devs[BAT].update(Percentage=100.0, State=4)
        up = self.up([suspend_charged])
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual([c[1] for c in up.calls], [False, True])

    def test_upower_restart_mid_run(self):
        def restart(up):
            up.missing_once.add(BAT)    # one read fails while UPower comes back
        up = self.up([restart, set_bat(Percentage=100.0, State=4)])
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual([c[1] for c in up.calls], [False, True])

    def test_battery_gone(self):
        def gone(up):
            del up.devs[BAT]
        up = self.up([gone])
        self.assertEqual(self.run_once(up), 1)      # nothing to restore on: keep the marker
        self.assertTrue(os.path.exists(self.marker))

    def test_upower_refuses(self):
        up = self.up()
        up.refuse.add(False)
        self.assertEqual(self.run_once(up), 1)
        self.assertFalse(os.path.exists(self.marker))
        self.assertTrue(up.devs[BAT]['ChargeThresholdEnabled'])


class NothingToDo(Base):
    def assertNothing(self, up):
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual(up.calls, [])
        self.assertEqual(up.waits, 0)
        self.assertFalse(os.path.exists(self.marker))

    def test_limit_off(self):
        self.assertNothing(self.up(bat={'ChargeThresholdEnabled': False, 'State': 1}))

    def test_no_battery(self):
        self.assertNothing(self.up(devices={}))

    def test_desktop_with_a_wireless_mouse(self):
        mouse = battery(NativePath='hid-mouse', Type=5, PowerSupply=False)
        self.assertNothing(self.up(devices={'/org/freedesktop/UPower/devices/mouse_0': mouse}))

    def test_not_supported(self):
        self.assertNothing(self.up(bat={'ChargeThresholdSupported': False, 'ChargeThresholdEnabled': False}))

    def test_already_full(self):
        self.assertNothing(self.up(bat={'Percentage': 100.0, 'State': 4}))

    def test_on_battery(self):
        self.assertNothing(self.up(on_battery=True, bat={'State': 2}))


class Recovery(Base):
    def interrupted(self, **bat):
        """UPower at boot after a power loss mid one-shot: its state file says off."""
        cf.write_marker({'path': BAT, 'native_path': 'macsmc-battery', 'end_threshold': 80}, self.marker)
        return self.up(bat=dict({'ChargeThresholdEnabled': False, 'State': 1}, **bat))

    def test_reboot_puts_limit_back(self):
        up = self.interrupted()
        self.assertEqual(cf.restore(up, self.marker, settle=0, clock=self.clock), 0)
        self.assertEqual([c[1] for c in up.calls], [True])
        self.assertFalse(os.path.exists(self.marker))

    def test_waits_for_upower_to_list_the_battery(self):
        up = self.interrupted()
        bat = up.devs.pop(BAT)

        def added(u):
            u.devs[BAT] = bat
        up.events = [added]
        self.assertEqual(cf.restore(up, self.marker, settle=60, clock=self.clock), 0)
        self.assertEqual([c[1] for c in up.calls], [True])

    def test_upower_never_lists_it(self):
        up = self.interrupted()
        up.devs.clear()

        def late(u):
            self.clock.t += 61
        up.events = [late]
        self.assertEqual(cf.restore(up, self.marker, settle=60, clock=self.clock), 1)
        self.assertTrue(os.path.exists(self.marker))

    def test_service_died_mid_run(self):
        # The process dies in the wait loop; ExecStopPost runs restore.
        def crash(up):
            raise RuntimeError('killed')
        up = self.up([crash])
        with self.assertRaises(RuntimeError):
            self.run_once(up)
        self.assertFalse(up.devs[BAT]['ChargeThresholdEnabled'])
        self.assertTrue(os.path.exists(self.marker))
        self.assertEqual(cf.restore(up, self.marker, settle=0, clock=self.clock), 0)
        self.assertTrue(up.devs[BAT]['ChargeThresholdEnabled'])
        self.assertFalse(os.path.exists(self.marker))

    def test_no_marker_nothing_touched(self):
        up = self.up(bat={'ChargeThresholdEnabled': False})
        self.assertEqual(cf.restore(up, self.marker, settle=0, clock=self.clock), 0)
        self.assertEqual(up.calls, [])

    def test_user_already_turned_it_on(self):
        up = self.interrupted(ChargeThresholdEnabled=True)
        self.assertEqual(cf.restore(up, self.marker, settle=0, clock=self.clock), 0)
        self.assertEqual(up.calls, [])
        self.assertFalse(os.path.exists(self.marker))

    def test_refused_keeps_marker(self):
        up = self.interrupted()
        up.refuse.add(True)
        self.assertEqual(cf.restore(up, self.marker, settle=0, clock=self.clock), 1)
        self.assertTrue(os.path.exists(self.marker))

    def test_damaged_marker_still_restores(self):
        up = self.interrupted()
        Path(self.marker).write_text('{')
        self.assertEqual(cf.restore(up, self.marker, settle=0, clock=self.clock), 0)
        self.assertEqual([c[1] for c in up.calls], [True])

    def test_start_after_interruption_continues(self):
        # Limit off only because of the earlier one-shot: starting again is allowed, then restores.
        up = self.interrupted()
        up.events = [set_bat(Percentage=100.0, State=4)]
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual([c[1] for c in up.calls], [True])
        self.assertFalse(os.path.exists(self.marker))

    def test_interrupted_and_now_full(self):
        up = self.interrupted(Percentage=100.0, State=4)
        self.assertEqual(self.run_once(up), 0)
        self.assertEqual([c[1] for c in up.calls], [True])
        self.assertFalse(os.path.exists(self.marker))


class Status(Base):
    def test_status(self):
        up = self.up()
        st = cf.status(up, 'inactive', None)
        self.assertFalse(st['active'])
        self.assertEqual(st['battery']['end_threshold'], 80)
        self.assertTrue(st['battery']['limit_enabled'])
        self.assertIsNone(cf.status(self.up(devices={}), 'inactive', None)['battery'])

    def test_preflight_messages(self):
        self.assertIn('limit is off', cf.preflight(self.up(bat={'ChargeThresholdEnabled': False}), None)[2])
        self.assertIn('no battery', cf.preflight(self.up(devices={}), None)[2])
        self.assertIn('adapter', cf.preflight(self.up(on_battery=True), None)[2])
        self.assertIsNone(cf.preflight(self.up(), None)[2])

    def test_usage(self):
        self.assertEqual(cf.main(['aurora-charge-full-now']), 2)
        self.assertEqual(cf.main(['aurora-charge-full-now', 'bogus']), 2)


class Busctl(unittest.TestCase):
    """The busctl layer against busctl's --json=short output."""

    def setUp(self):
        self.calls = []
        self.replies = {}

    def runner(self, args, **kw):
        self.calls.append(args)
        for member, out in self.replies.items():
            if member in args:
                return subprocess.CompletedProcess(args, 0, out, '')
        return subprocess.CompletedProcess(args, 1, '', 'Unit org.freedesktop.UPower not found')

    def test_parse(self):
        self.replies['EnumerateDevices'] = '{"type":"ao","data":[["%s"]]}' % BAT
        self.replies['GetAll'] = json.dumps({'type': 'a{sv}', 'data': [{
            'Type': {'type': 'u', 'data': 2}, 'PowerSupply': {'type': 'b', 'data': True},
            'Percentage': {'type': 'd', 'data': 80.0},
            'ChargeThresholdEnabled': {'type': 'b', 'data': True},
            'ChargeThresholdSupported': {'type': 'b', 'data': True}}]})
        self.replies['OnBattery'] = '{"type":"b","data":false}'
        self.replies['EnableChargeThreshold'] = ''      # busctl prints nothing for a void reply
        bus = cf.UPowerBus(runner=self.runner)
        path, p = cf.find_battery(bus)
        self.assertEqual(path, BAT)
        self.assertEqual(p['Percentage'], 80.0)
        self.assertFalse(bus.on_battery())
        self.assertTrue(bus.enable_threshold(BAT, False))
        self.assertEqual(self.calls[-1], ['busctl', '--system', '--json=short', 'call', 'org.freedesktop.UPower',
                                          BAT, 'org.freedesktop.UPower.Device', 'EnableChargeThreshold',
                                          'b', 'false'])

    def test_upower_absent(self):
        bus = cf.UPowerBus(runner=self.runner)
        self.assertEqual(bus.devices(), [])
        self.assertEqual(cf.find_battery(bus), (None, None))
        self.assertFalse(bus.enable_threshold(BAT, True))


class Install(unittest.TestCase):
    def test_staged(self):
        with tempfile.TemporaryDirectory() as t:
            subprocess.run(['make', '-s', 'install-charge-full-now', 'DESTDIR=' + t], cwd=ROOT, check=True)
            staged = Path(t)
            cli = staged / 'usr/bin/aurora-charge-full-now'
            self.assertTrue(cli.is_file() and os.access(cli, os.X_OK))
            unit = (staged / 'usr/lib/systemd/system/aurora-charge-full-now.service').read_text()
            self.assertIn('ExecStart=/usr/bin/aurora-charge-full-now run', unit)
            self.assertIn('ExecStopPost=/usr/bin/aurora-charge-full-now restore', unit)
            self.assertIn('StateDirectory=aurora/charge-full-now', unit)
            boot = (staged / 'usr/lib/systemd/system/aurora-charge-full-now-restore.service').read_text()
            self.assertIn('ConditionPathExists=%s' % cf.MARKER, boot)
            self.assertIn('WantedBy=multi-user.target', boot)
            rules = (staged / 'usr/share/polkit-1/rules.d/50-aurora-charge-full-now.rules').read_text()
            self.assertIn('"%s"' % cf.UNIT, rules)
            self.assertIn('subject.active', rules)
            self.assertNotIn('ConditionPathExists=/sys/firmware/devicetree', unit + boot)   # any laptop


if __name__ == '__main__':
    unittest.main()
