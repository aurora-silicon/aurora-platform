import importlib.machinery
import importlib.util
from pathlib import Path
import socket
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('power_modes', str(ROOT / 'power/aurora-power-modes'))
spec = importlib.util.spec_from_loader('power_modes', loader)
pm = importlib.util.module_from_spec(spec)
loader.exec_module(pm)


class Legs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.rt = self.base / 'rt'
        self.rt.write_text('1024\n')
        self.aspm = self.base / 'aspm'
        self.aspm.write_text('[default] performance powersave powersupersave\n')
        self.legs = {'rt_floor': (str(self.rt), {'power-saver': '0'}),
                     'aspm': (str(self.aspm), {'power-saver': 'powersupersave'})}

    def tearDown(self):
        self.tmp.cleanup()

    def test_choice_list_baseline(self):
        self.assertEqual(pm.current_value(str(self.aspm)), 'default')

    def test_power_saver_then_back(self):
        legs = pm.legs_from_config({}, self.legs)
        for leg in legs:
            self.assertTrue(leg.start())
        for leg in legs:
            leg.apply('power-saver')
        self.assertEqual(self.rt.read_text(), '0')
        self.assertEqual(self.aspm.read_text(), 'powersupersave')
        for leg in legs:
            leg.apply('balanced')
        self.assertEqual(self.rt.read_text(), '1024')
        self.assertEqual(self.aspm.read_text(), 'default')

    def test_untouched_outside_power_saver(self):
        legs = pm.legs_from_config({}, self.legs)
        for leg in legs:
            leg.start()
            self.assertFalse(leg.apply('performance'))  # baseline already in place: no write needed
        self.assertEqual(self.rt.read_text(), '1024\n')

    def test_end_restores(self):
        legs = pm.legs_from_config({}, self.legs)
        for leg in legs:
            leg.start()
            leg.apply('power-saver')
            leg.end()
        self.assertEqual(self.rt.read_text(), '1024')

    def test_restart_after_crash_keeps_the_boot_baseline(self):
        saved = self.base / 'run' / 'baselines.json'
        legs = pm.legs_from_config({}, self.legs)
        first = pm.load_baselines(str(saved))
        self.assertEqual(first, {})
        for leg in legs:
            leg.start(first.get(leg.path))
        pm.save_baselines(legs, str(saved))
        for leg in legs:
            leg.apply('power-saver')
        # The daemon dies here without end(): power-saver values stay in sysfs.
        legs = pm.legs_from_config({}, self.legs)
        found = pm.load_baselines(str(saved))
        for leg in legs:
            self.assertTrue(leg.start(found.get(leg.path)))
        self.assertEqual({leg.name: leg.baseline for leg in legs},
                         {'rt_floor': '1024', 'aspm': 'default'})
        for leg in legs:
            leg.apply('balanced')
        self.assertEqual(self.rt.read_text(), '1024')
        self.assertEqual(self.aspm.read_text(), 'default')

    def test_without_saved_baselines_the_files_are_read(self):
        self.assertEqual(pm.load_baselines(str(self.base / 'missing.json')), {})
        bad = self.base / 'bad.json'
        bad.write_text('[1, 2]')
        self.assertEqual(pm.load_baselines(str(bad)), {})
        saved = self.base / 'baselines.json'
        legs = pm.legs_from_config({}, self.legs)
        for leg in legs:
            leg.start()
        pm.save_baselines(legs, str(saved))
        pm.drop_baselines(str(saved))
        self.assertFalse(saved.exists())

    def test_config_override_and_off(self):
        legs = pm.legs_from_config({'aspm': 'off', 'rt_floor.balanced': '0'}, self.legs)
        self.assertEqual([leg.name for leg in legs], ['rt_floor'])
        legs[0].start()
        legs[0].apply('balanced')
        self.assertEqual(self.rt.read_text(), '0')

    def test_missing_path_skipped(self):
        legs = pm.legs_from_config({}, {'x': (str(self.base / 'nope'), {'power-saver': '1'})})
        self.assertFalse(legs[0].start())


class Platform(unittest.TestCase):
    def test_follows_profile(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            d = root / 'platform-profile-0'
            d.mkdir()
            (d / 'name').write_text('apple-smc\n')
            (d / 'choices').write_text('low-power balanced performance\n')
            (d / 'profile').write_text('balanced\n')
            legs = pm.legs_from_config({'rt_floor': 'off', 'aspm': 'off', '_platform_root': str(root)})
            self.assertEqual([leg.name for leg in legs], ['platform:apple-smc'])
            leg = legs[0]
            leg.start()
            self.assertTrue(leg.apply('performance'))
            self.assertEqual((d / 'profile').read_text(), 'performance')
            leg.apply('power-saver')
            self.assertEqual((d / 'profile').read_text(), 'low-power')
            leg.end()
            self.assertEqual((d / 'profile').read_text(), 'balanced')

    def test_missing_choice_keeps_baseline(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / 'p0'
            d.mkdir()
            (d / 'name').write_text('x\n')
            (d / 'choices').write_text('balanced performance\n')
            (d / 'profile').write_text('performance\n')
            leg = pm.platform_legs(t)[0]
            leg.start()
            self.assertFalse(leg.apply('power-saver'))
            self.assertTrue(leg.apply('balanced'))

    def test_off(self):
        self.assertEqual(pm.legs_from_config({'rt_floor': 'off', 'aspm': 'off', 'platform': 'off'}), [])

    def test_budget_handlers_are_left_to_aurora_power_budget(self):
        with tempfile.TemporaryDirectory() as t:
            for dev, name in (('platform-profile-0', 'apple-clpc'), ('platform-profile-1', 'apple-smc')):
                d = Path(t) / dev
                d.mkdir()
                (d / 'name').write_text(name + '\n')
                (d / 'choices').write_text('low-power balanced performance\n')
                (d / 'profile').write_text('low-power\n')
            legs = pm.legs_from_config({'rt_floor': 'off', 'aspm': 'off', '_platform_root': t})
            self.assertEqual([leg.name for leg in legs], ['platform:apple-smc'])


class Source(unittest.TestCase):
    def supply(self, root, name, kind, **attrs):
        d = root / name
        d.mkdir()
        (d / 'type').write_text(kind + '\n')
        for k, v in attrs.items():
            (d / k).write_text(v + '\n')

    def test_sources(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.assertIsNone(pm.power_source(str(root)))
            self.supply(root, 'hid-mouse', 'Battery', scope='Device')
            self.assertIsNone(pm.power_source(str(root)))
            self.supply(root, 'macsmc-battery', 'Battery')
            self.supply(root, 'macsmc-ac', 'Mains', online='0')
            self.assertEqual(pm.power_source(str(root)), 'battery')
            (root / 'macsmc-ac' / 'online').write_text('1\n')
            self.assertEqual(pm.power_source(str(root)), 'adapter')


class SourceEvents(unittest.TestCase):
    AC = (b'change@/devices/platform/soc/23e400000.smc/macsmc-power/power_supply/macsmc-ac\0'
          b'ACTION=change\0DEVPATH=/devices/platform/soc/23e400000.smc/macsmc-power/power_supply/macsmc-ac\0'
          b'SUBSYSTEM=power_supply\0POWER_SUPPLY_NAME=macsmc-ac\0POWER_SUPPLY_TYPE=Mains\0'
          b'POWER_SUPPLY_ONLINE=0\0SEQNUM=4242\0')
    USB = (b'add@/devices/platform/usb-drd1/xhci-hcd.1.auto/usb3/3-1\0ACTION=add\0'
           b'DEVPATH=/devices/platform/usb-drd1/xhci-hcd.1.auto/usb3/3-1\0SUBSYSTEM=usb\0SEQNUM=4243\0')

    def test_only_power_supply_events_count(self):
        self.assertTrue(pm.is_power_supply_event(self.AC))
        self.assertFalse(pm.is_power_supply_event(self.USB))
        self.assertFalse(pm.is_power_supply_event(b'change@/x\0SUBSYSTEM=power_supply_x\0'))

    def test_drain_reads_every_queued_event(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        with a, b:
            b.setblocking(False)
            self.assertFalse(pm.drain_power_supply_events(b))
            a.send(self.USB)
            self.assertFalse(pm.drain_power_supply_events(b))
            a.send(self.USB)
            a.send(self.AC)
            a.send(self.USB)
            self.assertTrue(pm.drain_power_supply_events(b))
            self.assertFalse(pm.drain_power_supply_events(b))

    def test_the_kernel_uevent_socket_opens_unprivileged(self):
        sock = pm.uevents()
        self.assertIsNotNone(sock)
        sock.close()


class Schedule(unittest.TestCase):
    def test_never(self):
        s = pm.Scheduler('never')
        self.assertIsNone(s.update('battery', 'balanced'))

    def test_battery_round_trip(self):
        s = pm.Scheduler('battery')
        self.assertIsNone(s.update('adapter', 'performance'))
        self.assertEqual(s.update('battery', 'performance'), 'power-saver')
        self.assertIsNone(s.update('battery', 'power-saver'))
        self.assertEqual(s.update('adapter', 'power-saver'), 'performance')

    def test_user_choice_wins(self):
        s = pm.Scheduler('battery')
        s.update('adapter', 'balanced')
        self.assertEqual(s.update('battery', 'balanced'), 'power-saver')
        self.assertIsNone(s.update('battery', 'balanced'))      # the user went back to balanced on battery
        self.assertIsNone(s.update('adapter', 'balanced'))      # nothing to restore

    def test_user_already_in_power_saver(self):
        s = pm.Scheduler('battery')
        s.update('adapter', 'power-saver')
        self.assertIsNone(s.update('battery', 'power-saver'))
        self.assertIsNone(s.update('adapter', 'power-saver'))   # we did not switch, so we do not switch back

    def test_waits_for_profile_daemon(self):
        s = pm.Scheduler('battery')
        self.assertIsNone(s.update('battery', None))
        self.assertEqual(s.update('battery', 'balanced'), 'power-saver')

    def test_always_and_adapter(self):
        self.assertEqual(pm.Scheduler('always').update(None, 'balanced'), 'power-saver')
        self.assertEqual(pm.Scheduler('adapter').update('adapter', 'balanced'), 'power-saver')
        self.assertIsNone(pm.Scheduler('adapter').update('battery', 'balanced'))


if __name__ == '__main__':
    unittest.main()
