import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
