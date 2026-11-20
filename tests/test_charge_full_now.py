import calendar
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
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


class FakeSystemd:
    """systemctl and systemd-run as the schedule code calls them."""

    def __init__(self, active=(), run_rc=0):
        self.active = set(active)
        self.calls = []
        self.run_rc = run_rc

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if args[0] == 'systemd-run':
            if self.run_rc == 0:
                self.active.add(cf.SCHEDULE + '.timer')
            return subprocess.CompletedProcess(args, self.run_rc, '', 'Failed to start transient timer unit')
        verb, units = args[1], [a for a in args[2:] if not a.startswith('-')]
        if verb == 'is-active':
            on = units[0] in self.active
            return subprocess.CompletedProcess(args, 0 if on else 3, 'active\n' if on else 'inactive\n', '')
        if verb == 'stop':
            self.active.difference_update(units)
        if verb == 'start':
            self.active.update(u for u in units if '@' not in u)
        return subprocess.CompletedProcess(args, 0, '', '')

    def verbs(self):
        return [c[1] if c[0] == 'systemctl' else c[0] for c in self.calls if c[1:2] != ['is-active']]


class Schedule(Base):
    """start --by HH:MM: the plan, the timer, cancel."""

    def setUp(self):
        super().setUp()
        self.tz = os.environ.get('TZ')
        os.environ['TZ'] = 'UTC'
        time.tzset()
        self.now = calendar.timegm((2026, 11, 20, 22, 0, 0))      # Friday 22:00
        self.sys = Path(self.tmp.name) / 'power_supply'
        self.sys.mkdir()
        self.sched = os.path.join(self.tmp.name, 'run', 'schedule.json')
        self.saved = cf.RUN, cf.MARKER, cf.SCHEDULE_FILE
        cf.MARKER, cf.SCHEDULE_FILE = self.marker, self.sched
        self.systemd = cf.RUN = FakeSystemd()

    def tearDown(self):
        cf.RUN, cf.MARKER, cf.SCHEDULE_FILE = self.saved
        if self.tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self.tz
        time.tzset()
        super().tearDown()

    def supply(self, name, kind, **attrs):
        d = self.sys / name
        d.mkdir()
        (d / 'type').write_text(kind + '\n')
        for k, v in attrs.items():
            (d / k).write_text('%s\n' % v)

    def adapter(self, watts=96, online=1):
        self.supply('macsmc-ac', 'Mains', online=online, input_power_limit=int(watts * 1e6))

    def bat(self, **kw):
        return dict({'EnergyFull': 70.0, 'Energy': 56.0, 'EnergyRate': 0.0}, **kw)

    def arm(self, by='07:30', **bat):
        up = self.up(bat=self.bat(**bat))
        rc = cf.arm(up, by, now=self.now, root=str(self.sys), schedule_file=self.sched)
        self.assertEqual(up.calls, [], 'arming must never touch the limit')
        return rc

    def test_parse_by(self):
        self.assertEqual(cf.parse_by('07:30'), (7, 30))
        self.assertEqual(cf.parse_by('0730'), (7, 30))
        self.assertEqual(cf.parse_by('23:59'), (23, 59))
        for bad in ('24:00', '7:30', '07:60', 'ab:cd', '', None, '07:30:00', '073'):
            self.assertIsNone(cf.parse_by(bad), bad)

    def test_next_occurrence(self):
        self.assertEqual(cf.next_deadline(7, 30, self.now), calendar.timegm((2026, 11, 21, 7, 30, 0)))
        self.assertEqual(cf.next_deadline(23, 0, self.now), calendar.timegm((2026, 11, 20, 23, 0, 0)))
        # The time already passed today, or is now: tomorrow.
        self.assertEqual(cf.next_deadline(22, 0, self.now), calendar.timegm((2026, 11, 21, 22, 0, 0)))
        self.assertEqual(cf.next_deadline(6, 0, self.now), calendar.timegm((2026, 11, 21, 6, 0, 0)))

    def test_adapter_watts(self):
        self.assertIsNone(cf.adapter_watts(str(self.sys)))
        self.supply('macsmc-battery', 'Battery', online=1, input_power_limit=5000000)
        self.adapter(96, online=0)
        self.assertIsNone(cf.adapter_watts(str(self.sys)))      # unplugged
        (self.sys / 'macsmc-ac' / 'online').write_text('1\n')
        self.assertEqual(cf.adapter_watts(str(self.sys)), 96.0)
        self.supply('usb-c', 'USB', online=1)                   # no input_power_limit: ignored
        self.assertEqual(cf.adapter_watts(str(self.sys)), 96.0)

    def test_plan_far(self):
        deadline = cf.next_deadline(7, 30, self.now)
        pl = cf.plan(self.bat(), deadline, self.now, 96.0)
        self.assertEqual(pl['rate_w'], 57.6)                    # 0.6 x 96 W
        self.assertEqual(pl['deficit_wh'], 14.0)
        self.assertEqual(pl['start'], int(deadline - 14.0 / 57.6 * 3600 - 1800))
        self.assertFalse(pl['now'])

    def test_plan_near(self):
        deadline = self.now + 20 * 60
        pl = cf.plan(self.bat(), deadline, self.now, 96.0)
        self.assertTrue(pl['now'])
        self.assertEqual(pl['start'], self.now)

    def test_plan_no_adapter_assumes_the_floor(self):
        deadline = cf.next_deadline(7, 30, self.now)
        pl = cf.plan(self.bat(), deadline, self.now, None)
        self.assertEqual(pl['rate_w'], 10.0)
        self.assertEqual(pl['start'], int(deadline - 1.4 * 3600 - 1800))

    def test_plan_rates(self):
        d = self.now + 86400
        self.assertEqual(cf.plan(self.bat(State=1, EnergyRate=20.0), d, self.now, 96.0)['rate_w'], 20.0)
        self.assertEqual(cf.plan(self.bat(State=1, EnergyRate=3.0), d, self.now, 96.0)['rate_w'], 10.0)
        self.assertEqual(cf.plan(self.bat(), d, self.now, 12.0)['rate_w'], 10.0)    # 7.2 W derated: floor
        self.assertEqual(cf.plan(self.bat(EnergyFull=0, Percentage=80.0), d, self.now, None)['deficit_wh'], 20.0)

    def test_arm_far_sets_a_timer(self):
        self.adapter(96)
        self.assertEqual(self.arm('07:30'), 0)
        run = [c for c in self.systemd.calls if c[0] == 'systemd-run']
        self.assertEqual(len(run), 1)
        start = int(calendar.timegm((2026, 11, 21, 7, 30, 0)) - 14.0 / 57.6 * 3600 - 1800)
        self.assertIn('--unit=aurora-charge-full-now-schedule', run[0])
        self.assertIn('--on-calendar=' + time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(start)), run[0])
        self.assertIn('--timer-property=AccuracySec=1min', run[0])
        self.assertEqual(run[0][-2:], ['/usr/bin/aurora-charge-full-now', 'fire'])
        self.assertNotIn('start', self.systemd.verbs())          # the one-shot itself waits for the timer
        sch = json.loads(Path(self.sched).read_text())
        self.assertEqual((sch['start'], sch['deadline']), (start, calendar.timegm((2026, 11, 21, 7, 30, 0))))

    def test_arm_near_starts_now(self):
        self.adapter(96)
        self.assertEqual(self.arm('22:20'), 0)
        self.assertIn(['systemctl', 'start', '--no-block', cf.UNIT], self.systemd.calls)
        self.assertNotIn('systemd-run', self.systemd.verbs())
        self.assertFalse(os.path.exists(self.sched))

    def test_arm_replaces_a_pending_timer(self):
        self.systemd.active.add(cf.SCHEDULE + '.timer')
        self.adapter(96)
        self.assertEqual(self.arm('07:30'), 0)
        self.assertEqual(self.systemd.verbs()[:3], ['stop', 'reset-failed', 'systemd-run'])

    def test_arm_full_battery(self):
        self.systemd.active.add(cf.SCHEDULE + '.timer')
        self.assertEqual(self.arm('07:30', Percentage=100.0, State=4, Energy=70.0), 0)
        self.assertNotIn('systemd-run', self.systemd.verbs())
        self.assertNotIn('start', self.systemd.verbs())
        self.assertNotIn(cf.SCHEDULE + '.timer', self.systemd.active)     # an old schedule is cleared

    def test_arm_limit_off(self):
        self.assertEqual(self.arm('07:30', ChargeThresholdEnabled=False), 0)
        self.assertNotIn('systemd-run', self.systemd.verbs())
        self.assertNotIn('start', self.systemd.verbs())

    def test_arm_on_battery_still_plans(self):
        # Plugged in later at night: the timer is armed; the one-shot checks the adapter when it runs.
        up = self.up(bat=self.bat(State=2), on_battery=True)
        self.assertEqual(cf.arm(up, '07:30', now=self.now, root=str(self.sys), schedule_file=self.sched), 0)
        self.assertIn('systemd-run', self.systemd.verbs())

    def test_arm_bad_time(self):
        self.assertEqual(self.arm('25:00'), 2)
        self.assertEqual(self.systemd.calls, [])

    def test_arm_failure_leaves_nothing(self):
        self.systemd.run_rc = 1
        self.assertEqual(self.arm('07:30'), 1)
        self.assertFalse(os.path.exists(self.sched))
        self.assertNotIn('start', self.systemd.verbs())

    def test_fire_starts_the_one_shot(self):
        Path(self.sched).parent.mkdir(parents=True)
        Path(self.sched).write_text('{}')
        self.assertEqual(cf.fire(self.sched), 0)
        self.assertEqual(self.systemd.calls, [['systemctl', 'start', '--no-block', cf.UNIT]])
        self.assertFalse(os.path.exists(self.sched))

    def test_cli_start_by_starts_the_planner(self):
        def planner(args, **kw):
            if args[:2] == ['systemctl', 'start'] and '@' in args[2]:
                self.systemd.active.add(cf.SCHEDULE + '.timer')
                Path(self.sched).parent.mkdir(parents=True, exist_ok=True)
                Path(self.sched).write_text(json.dumps({'start': self.now + 3600, 'deadline': self.now + 9000,
                                                        'deficit_wh': 14.0, 'rate_w': 57.6}))
            return self.systemd(args, **kw)
        cf.RUN = planner
        self.assertEqual(cf.cmd_start(self.up(bat=self.bat()), '07:30'), 0)
        self.assertIn(['systemctl', 'start', 'aurora-charge-full-now-schedule@0730.service'], self.systemd.calls)
        st = cf.status(self.up(), 'inactive', None, cf.read_schedule(self.sched))
        self.assertEqual(st['schedule']['start_at'], self.now + 3600)
        self.assertEqual(st['schedule']['by'], self.now + 9000)
        self.assertEqual(cf.cmd_start(self.up(), '7h'), 2)

    def test_cli_start_by_limit_off(self):
        self.assertEqual(cf.cmd_start(self.up(bat={'ChargeThresholdEnabled': False}), '07:30'), 0)
        self.assertEqual(self.systemd.calls, [])

    def test_start_now_clears_a_schedule(self):
        self.systemd.active.add(cf.SCHEDULE + '.timer')
        self.assertEqual(cf.cmd_start(self.up()), 0)
        self.assertEqual(self.systemd.verbs(), ['stop', 'start'])

    def test_cancel_clears_schedule_and_one_shot(self):
        self.systemd.active.update({cf.SCHEDULE + '.timer', cf.UNIT})
        self.assertEqual(cf.cmd_cancel(), 0)
        self.assertEqual(self.systemd.active, set())
        self.systemd.calls.clear()
        self.assertEqual(cf.cmd_cancel(), 0)                    # nothing left: no stop
        self.assertNotIn('stop', self.systemd.verbs())

    def test_no_schedule_without_timer(self):
        Path(self.sched).parent.mkdir(parents=True)
        Path(self.sched).write_text('{"start": 1}')             # stale file, timer gone (fired or cancelled)
        self.assertIsNone(cf.read_schedule(self.sched))
        self.assertIsNone(cf.status(self.up(), 'inactive', None, None)['schedule'])

    def test_usage(self):
        self.assertEqual(cf.main(['aurora-charge-full-now', 'start', '--by']), 2)
        self.assertEqual(cf.main(['aurora-charge-full-now', 'start', '07:30']), 2)
        self.assertEqual(cf.main(['aurora-charge-full-now', 'arm']), 2)


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
            planner = (staged / 'usr/lib/systemd/system/aurora-charge-full-now-schedule@.service').read_text()
            self.assertIn('ExecStart=/usr/bin/aurora-charge-full-now arm %i', planner)
            self.assertIn('WantedBy=multi-user.target', boot)
            rules = (staged / 'usr/share/polkit-1/rules.d/50-aurora-charge-full-now.rules').read_text()
            self.assertIn('"%s"' % cf.UNIT, rules)
            self.assertIn('subject.active', rules)
            self.assertNotIn('ConditionPathExists=/sys/firmware/devicetree', unit + boot)   # any laptop
            self.assertFalse((staged / 'usr/share/gnome-shell').exists())     # the extension is opt-in

    def test_gnome_extension_staged(self):
        uuid = 'charge-full-now@aurorasilicon.org'
        with tempfile.TemporaryDirectory() as t:
            subprocess.run(['make', '-s', 'install-gnome-charge-full-now', 'DESTDIR=' + t], cwd=ROOT, check=True)
            ext = Path(t) / 'usr/share/gnome-shell/extensions' / uuid
            meta = json.loads((ext / 'metadata.json').read_text())
            self.assertEqual(meta['uuid'], uuid)
            self.assertIn('51', meta['shell-version'])
            js = (ext / 'extension.js').read_text()
            self.assertIn("'%s'" % cf.MARKER, js)
            self.assertIn('ChargeThresholdEnabled', js)
            self.assertNotIn('_sync(Gio', js)
            self.assertNotIn('new_for_bus_sync', js)   # no blocking D-Bus calls in the compositor


if __name__ == '__main__':
    unittest.main()
