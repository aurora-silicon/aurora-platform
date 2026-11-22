import errno
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('power_budget', str(ROOT / 'power/aurora-power-budget'))
spec = importlib.util.spec_from_loader('power_budget', loader)
pb = importlib.util.module_from_spec(spec)
loader.exec_module(pb)

# Two clusters shaped like a small/large pair; microwatts per CPU per operating point.
E_OPPS = {600000: 26000, 1284000: 88000, 2424000: 351000}
P_OPPS = {660000: 133000, 1704000: 441000, 2400000: 855000, 3204000: 2159000}


def be(value, width):
    return value.to_bytes(width, 'big')


class FakeSystem:
    def __init__(self, base, missing_microwatt=False):
        self.dt = base / 'dt'
        self.sys = base / 'sys'
        for phandle, (table, cpus) in enumerate(((E_OPPS, (0, 1)), (P_OPPS, (2, 3))), start=7):
            node = self.dt / ('opp-table-%d' % phandle)
            node.mkdir(parents=True)
            (node / 'phandle').write_bytes(be(phandle, 4))
            for i, (khz, uw) in enumerate(sorted(table.items())):
                opp = node / ('opp%02d' % i)
                opp.mkdir()
                (opp / 'opp-hz').write_bytes(be(khz * 1000, 8))
                if not missing_microwatt:
                    (opp / 'opp-microwatt').write_bytes(be(uw, 4))
            policy = self.sys / 'cpufreq' / ('policy%d' % cpus[0])
            policy.mkdir(parents=True)
            (policy / 'related_cpus').write_text(' '.join(map(str, cpus)) + '\n')
            (policy / 'cpuinfo_min_freq').write_text('%d\n' % min(table))
            (policy / 'cpuinfo_max_freq').write_text('%d\n' % max(table))
            (policy / 'scaling_max_freq').write_text('%d\n' % max(table))
            (policy / 'scaling_cur_freq').write_text('%d\n' % max(table))
            for cpu in cpus:
                (self.sys / ('cpu%d' % cpu)).mkdir(parents=True, exist_ok=True)
                (self.sys / ('cpu%d' % cpu) / 'cpu_capacity').write_text('%d\n' % (571 if phandle == 7 else 1024))
                of_node = self.sys / ('cpu%d' % cpu) / 'of_node'
                of_node.mkdir(parents=True)
                (of_node / 'operating-points-v2').write_bytes(be(phandle, 4))


class PowerBudgetTests(unittest.TestCase):
    def test_energy_model_comes_from_the_device_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeSystem(Path(tmp))
            cl = pb.clusters(str(fake.sys), str(fake.dt))
            self.assertEqual([c.cpus for c in cl], [[0, 1], [2, 3]])
            self.assertEqual(cl[1].opps[-1], (3204000, 2159.0))
            self.assertEqual(cl[0].power(1000000), 88.0)

    def test_no_energy_model_means_no_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeSystem(Path(tmp), missing_microwatt=True)
            self.assertIsNone(pb.clusters(str(fake.sys), str(fake.dt)))

    def test_budget_policy_fraction_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeSystem(Path(tmp))
            cl = pb.clusters(str(fake.sys), str(fake.dt))
            # 0.45 x fmax, interpolated: E 1090.8 MHz between 600 and 1284, P 1441.8 MHz between 660 and 1704.
            e = 26 + (88 - 26) * (1090800 - 600000) / (1284000 - 600000)
            p = 133 + (441 - 133) * (1441800 - 660000) / (1704000 - 660000)
            self.assertAlmostEqual(pb.budget_mw(cl, {}), 2 * e + 2 * p)
            self.assertEqual(pb.budget_mw(cl, {'budget_mw': '1500'}), 1500)
            # Below the table the lowest point's power is used.
            self.assertEqual(pb.budget_mw(cl, {'fraction': '0.2'}), 2 * 26 + 2 * 133)

    def test_ceiling_holds_only_the_biggest_cores(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeSystem(Path(tmp))
            cl = pb.clusters(str(fake.sys), str(fake.dt))
            # 0.64 x 3204 MHz = 2050 MHz: the highest P point at or below it is 1704 MHz.
            self.assertEqual(pb.ceilings(cl, {}), [2424000, 1704000])
            self.assertEqual(pb.ceilings(cl, {'ceiling': '1'}), [2424000, 3204000])

    def test_budget_goes_to_the_cheapest_capacity_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeSystem(Path(tmp))
            e, p = pb.clusters(str(fake.sys), str(fake.dt))
            self.assertEqual((e.capacity, p.capacity), (571, 1024))
            # Both clusters fully busy: the E cluster keeps its top point, P gives way.
            self.assertEqual(pb.allocate(2 * 351 + 2 * 441, [(e, 2, e.fmax), (p, 2, p.fmax)]), [2424000, 1704000])
            # Nothing affordable above the floor: both stay at their lowest point.
            self.assertEqual(pb.allocate(10, [(e, 2, e.fmax), (p, 2, p.fmax)]), [600000, 660000])
            # A cluster is never raised past what it asks for.
            self.assertEqual(pb.allocate(10000, [(e, 2, 1284000), (p, 2, p.fmax)]), [1284000, 3204000])

    def test_an_idle_cluster_keeps_headroom_for_a_burst(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeSystem(Path(tmp))
            e, p = pb.clusters(str(fake.sys), str(fake.dt))
            # P busy and over budget; idle E gets what one CPU can afford from the rest.
            self.assertEqual(pb.allocate(2 * 441 + 351, [(e, 0.0, e.fmin), (p, 2, p.fmax)]), [2424000, 1704000])

    def test_config_parser_ignores_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / 'power-budget.conf'
            conf.write_text('# comment\nbudget_mw = 2500  # watts x1000\nnoise\n')
            self.assertEqual(pb.load_config(str(conf)), {'budget_mw': '2500'})


def handler(root, dev='platform-profile-0', name='apple-clpc',
            choices='low-power balanced performance', profile='balanced'):
    d = Path(root) / dev
    d.mkdir(parents=True)
    (d / 'name').write_text(name + '\n')
    (d / 'choices').write_text(choices + '\n')
    (d / 'profile').write_text(profile + '\n')
    return d / 'profile'


def failing_writes(err):
    """open() for the module under test that refuses every write, as a kernel handler can."""
    real = open

    def fake(path, mode='r', *args, **kwargs):
        if 'w' in mode:
            raise OSError(err, os.strerror(err), path)
        return real(path, mode, *args, **kwargs)
    return fake


def counting_writes(writes):
    real = open

    def fake(path, mode='r', *args, **kwargs):
        if 'w' in mode:
            writes.append(path)
        return real(path, mode, *args, **kwargs)
    return fake


class KernelHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logged = []
        patcher = mock.patch.object(pb, 'log', self.logged.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_named_handlers_count(self):
        self.assertEqual(pb.KernelHandlers(str(self.root / 'missing')).devices(), [])
        self.assertEqual(pb.KernelHandlers(str(self.root)).devices(), [])
        handler(self.root, 'platform-profile-0', name='thinkpad-acpi')
        self.assertEqual(pb.KernelHandlers(str(self.root)).devices(), [])
        handler(self.root, 'platform-profile-1')
        self.assertEqual(pb.KernelHandlers(str(self.root)).devices(),
                         [('platform-profile-1', 'apple-clpc', ['low-power', 'balanced', 'performance'])])

    def test_names_come_from_the_choices_file(self):
        apple = 'low-power balanced performance'.split()
        self.assertEqual([pb.platform_value(p, apple) for p in ('power-saver', 'balanced', 'performance')],
                         ['low-power', 'balanced', 'performance'])
        # power-profiles-daemon 0.30's fallback for power-saver.
        self.assertEqual(pb.platform_value('power-saver', ['cool', 'quiet', 'balanced']), 'quiet')
        self.assertIsNone(pb.platform_value('power-saver', ['cool', 'balanced']))
        self.assertIsNone(pb.platform_value('power-saver', ['balanced', 'performance']))
        self.assertIsNone(pb.platform_value('performance', ['low-power', 'balanced']))
        self.assertIsNone(pb.platform_value(None, apple))

    def test_each_profile_reaches_the_handler(self):
        profile = handler(self.root)
        kernel = pb.KernelHandlers(str(self.root))
        self.assertEqual(kernel.hand_over('power-saver'), ({'platform-profile-0': 'low-power'}, 'apple-clpc'))
        self.assertEqual(profile.read_text(), 'low-power')
        self.assertEqual(kernel.hand_over('performance'), ({'platform-profile-0': 'performance'}, None))
        self.assertEqual(profile.read_text(), 'performance')
        self.assertEqual(kernel.hand_over('balanced'), ({'platform-profile-0': 'balanced'}, None))
        self.assertEqual(profile.read_text(), 'balanced')

    def test_a_handler_that_holds_the_value_is_not_written(self):
        # power-profiles-daemon writes the class before it announces the profile.
        handler(self.root, profile='low-power')
        writes = []
        with mock.patch.object(pb, 'open', counting_writes(writes), create=True):
            state, held = pb.KernelHandlers(str(self.root)).hand_over('power-saver')
        self.assertEqual((state, held, writes), ({'platform-profile-0': 'low-power'}, 'apple-clpc', []))

    def test_unknown_profile_leaves_the_handler_alone(self):
        profile = handler(self.root, profile='performance')
        kernel = pb.KernelHandlers(str(self.root))
        self.assertEqual(kernel.hand_over(None), ({}, None))
        self.assertEqual(kernel.hand_over('custom'), ({}, None))
        self.assertEqual(profile.read_text(), 'performance\n')

    def test_no_class_device_is_a_no_op(self):
        for root in (self.root / 'missing', self.root):
            self.assertEqual(pb.KernelHandlers(str(root)).hand_over('power-saver'), ({}, None))
        # So the CPU budget applies, as without a handler before.
        self.assertTrue(pb.cap_wanted('power-saver', None, True))

    def test_a_refused_write_leaves_power_saver_to_the_budget(self):
        handler(self.root)
        kernel = pb.KernelHandlers(str(self.root))
        with mock.patch.object(pb, 'open', failing_writes(errno.EOPNOTSUPP), create=True):
            self.assertEqual(kernel.hand_over('power-saver'), ({}, None))
            kernel.hand_over('power-saver')
        self.assertEqual(len([m for m in self.logged if 'cannot set low-power' in m]), 1)
        self.assertTrue(pb.cap_wanted('power-saver', None, True))

    def test_a_handler_without_a_power_saver_choice_does_not_hold_it(self):
        profile = handler(self.root, choices='balanced performance')
        state, held = pb.KernelHandlers(str(self.root)).hand_over('power-saver')
        self.assertEqual((state, held), ({'platform-profile-0': 'balanced'}, None))
        self.assertEqual(profile.read_text(), 'balanced\n')
        self.assertTrue(any('offers nothing for power-saver' in m for m in self.logged))

    def test_each_change_is_logged_once(self):
        handler(self.root)
        kernel = pb.KernelHandlers(str(self.root))
        for p in ('power-saver', 'power-saver', 'balanced', 'balanced', 'power-saver'):
            kernel.hand_over(p)
        self.assertEqual(self.logged, ['apple-clpc (platform-profile-0): low-power for power-saver',
                                       'apple-clpc (platform-profile-0): balanced for balanced',
                                       'apple-clpc (platform-profile-0): low-power for power-saver'])

    def test_stop_puts_balanced_back_where_this_daemon_set_the_handler(self):
        # tuned-ppd: this daemon is the class's only writer.
        profile = handler(self.root)
        kernel = pb.KernelHandlers(str(self.root))
        kernel.hand_over('power-saver')
        kernel.hand_over('power-saver')     # still ours on a repeat
        kernel.release()
        self.assertEqual(profile.read_text(), 'balanced')
        self.assertIn('apple-clpc (platform-profile-0): back to balanced at stop', self.logged)

    def test_stop_leaves_a_value_this_daemon_did_not_write(self):
        # power-profiles-daemon set it before announcing the profile.
        profile = handler(self.root, profile='low-power')
        kernel = pb.KernelHandlers(str(self.root))
        kernel.hand_over('power-saver')
        writes = []
        with mock.patch.object(pb, 'open', counting_writes(writes), create=True):
            kernel.release()
        self.assertEqual((profile.read_text(), writes), ('low-power\n', []))

    def test_stop_leaves_a_handler_someone_changed_since(self):
        profile = handler(self.root)
        kernel = pb.KernelHandlers(str(self.root))
        kernel.hand_over('power-saver')
        profile.write_text('performance\n')
        kernel.release()
        self.assertEqual(profile.read_text(), 'performance\n')

    def test_stop_after_balanced_writes_nothing(self):
        profile = handler(self.root, profile='performance')
        kernel = pb.KernelHandlers(str(self.root))
        kernel.hand_over('balanced')
        writes = []
        with mock.patch.object(pb, 'open', counting_writes(writes), create=True):
            kernel.release()
        self.assertEqual((profile.read_text(), writes), ('balanced', []))

    def test_who_caps(self):
        self.assertTrue(pb.cap_wanted('power-saver', None, True))
        self.assertFalse(pb.cap_wanted('power-saver', 'apple-clpc', True))
        self.assertFalse(pb.cap_wanted('power-saver', None, False))
        self.assertFalse(pb.cap_wanted('balanced', None, True))
        self.assertFalse(pb.cap_wanted(None, None, True))

    def test_idle_status(self):
        self.assertEqual(pb.idle_status('power-saver', 1234.4, {'platform-profile-0': 'low-power'}, 'apple-clpc'),
                         {'profile': 'power-saver', 'capping': False, 'budget_mw': 1234,
                          'kernel_profiles': {'platform-profile-0': 'low-power'},
                          'reason': 'kernel apple-clpc holds power-saver'})
        self.assertEqual(pb.idle_status('balanced', None, {}, None),
                         {'profile': 'balanced', 'capping': False, 'reason': 'no energy model'})


class Done(Exception):
    pass


class MainLoopTests(unittest.TestCase):
    """main() against a fake class device, fake clusters and a fake busctl monitor."""

    def run_main(self, profiles, with_handler=True, with_model=True, refuse=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        fake = FakeSystem(base, missing_microwatt=not with_model)
        classes = base / 'class'
        classes.mkdir()
        profile = handler(classes) if with_handler else None
        seen, logged, status = [], [], []
        self.signals = {}
        queue = list(profiles)
        r, w = os.pipe()
        # One busctl line per profile read after the first, and one more that ends the run.
        os.write(w, b'{}\n' * len(queue))
        os.close(w)
        mon = types.SimpleNamespace(stdout=os.fdopen(r, 'rb', buffering=0), wait=lambda: None)
        self.addCleanup(mon.stdout.close)

        def active_profile():
            if not queue:
                raise Done()
            p = queue.pop(0)
            if profile is not None:
                seen.append(profile.read_text().strip())
            return p

        real_clusters, real_handlers = pb.clusters, pb.KernelHandlers
        self.budget = pb.budget_mw(real_clusters(str(fake.sys), str(fake.dt)), {}) if with_model else None
        patches = [
            mock.patch.object(pb, 'clusters', lambda: real_clusters(str(fake.sys), str(fake.dt))),
            mock.patch.object(pb, 'KernelHandlers', lambda: real_handlers(str(classes))),
            mock.patch.object(pb, 'active_profile', active_profile),
            mock.patch.object(pb, 'monitor', lambda: mon),
            mock.patch.object(pb, 'log', logged.append),
            mock.patch.object(pb, 'write_status', status.append),
            mock.patch.object(pb.signal, 'signal', self.signals.__setitem__),
            mock.patch.object(pb.signal, 'pause', mock.Mock(side_effect=Done)),
        ]
        if refuse:
            patches.append(mock.patch.object(pb, 'open', failing_writes(refuse), create=True))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with self.assertRaises(Done):
            pb.main()
        if profile is not None:
            seen.append(profile.read_text().strip())
        self.profile = profile
        return seen, logged, status

    def test_tuned_ppd_profiles_reach_the_handler_and_the_kernel_owns_power_saver(self):
        # seen: the handler's profile before each profile read, and at the end.
        seen, logged, status = self.run_main(['balanced', 'power-saver', 'performance', 'balanced'])
        self.assertEqual(seen, ['balanced', 'balanced', 'low-power', 'performance', 'balanced'])
        self.assertFalse(any('CPU budget' in m and 'active' in m for m in logged))
        self.assertIn({'profile': 'power-saver', 'capping': False, 'budget_mw': mock.ANY,
                       'kernel_profiles': {'platform-profile-0': 'low-power'},
                       'reason': 'kernel apple-clpc holds power-saver'}, status)

    def test_sigterm_in_power_saver_puts_the_handler_back_to_balanced(self):
        seen, _, status = self.run_main(['balanced', 'power-saver'])
        self.assertEqual(seen[-1], 'low-power')
        with self.assertRaises(SystemExit):
            self.signals[pb.signal.SIGTERM](pb.signal.SIGTERM, None)
        self.assertEqual(self.profile.read_text().strip(), 'balanced')
        self.assertEqual(status[-1], {'profile': None, 'capping': False})

    def test_a_handler_that_refuses_leaves_the_budget_capping(self):
        seen, logged, _ = self.run_main(['balanced', 'power-saver', 'balanced'], refuse=errno.EOPNOTSUPP)
        self.assertEqual(seen, ['balanced'] * 4)
        self.assertEqual([m for m in logged if m.startswith(('power-saver:', 'profile '))],
                         ['power-saver: CPU budget %.0f mW active' % self.budget, 'profile balanced: CPU caps released'])

    def test_without_a_handler_the_budget_is_unchanged(self):
        _, logged, _ = self.run_main(['power-saver', 'balanced'], with_handler=False)
        self.assertEqual([m for m in logged if m.startswith(('power-saver:', 'profile ', 'kernel '))],
                         ['power-saver: CPU budget %.0f mW active' % self.budget, 'profile balanced: CPU caps released'])

    def test_a_handler_without_an_energy_model_still_gets_the_profile(self):
        seen, logged, _ = self.run_main(['power-saver', 'balanced'], with_model=False)
        self.assertEqual(seen, ['balanced', 'low-power', 'balanced'])
        self.assertIn("no device tree energy model for every CPU cluster: power-saver has only the kernel's budget",
                      logged)

    def test_no_handler_and_no_energy_model_idles_as_before(self):
        _, logged, status = self.run_main(['power-saver'], with_handler=False, with_model=False)
        self.assertEqual(status, [{'profile': None, 'capping': False, 'reason': 'no energy model'}])


if __name__ == '__main__':
    unittest.main()
