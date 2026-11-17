import configparser
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
INI = ROOT / 'power/gamemode.ini'

# cpuinfo_max_freq (kHz) per CPU, as the kernel reports it on our Macs.
MACS = {
    'M1 (T8103)': [2064000] * 4 + [3204000] * 4,
    'M2 (T8112)': [2424000] * 4 + [3504000] * 4,
    'M1 Pro (T6000)': [2064000] * 2 + [3228000] * 8,
    'M4 Pro (T6040)': [4512000] + [2592000] * 4 + [4512000] * 9,
    'A18 Pro (T8140)': [4044000] + [2424000] * 4 + [4044000],
}


def gamemode_pin_set(freqs):
    """GameMode 1.8's automatic pin set (daemon/gamemode-cpu.c walk_sysfs):
    the CPUs within 10 % of the highest cpuinfo_max_freq, or nothing when
    that would leave fewer than four CPUs or every CPU."""
    keep, top = set(), 0
    for cpu, freq in enumerate(freqs):
        cutoff = freq * 10 // 100
        if freq > top:
            if top < freq - cutoff:
                keep.clear()
            top = freq
        if freq + cutoff >= top:
            keep.add(cpu)
    if len(keep) < 4 or len(keep) == len(freqs):
        return set()
    return keep


def load():
    text = INI.read_text()
    parser = configparser.ConfigParser(comment_prefixes=(';',), inline_comment_prefixes=None)
    parser.read_string(text)
    return text, parser


class GameModeConfig(unittest.TestCase):
    def test_keeps_the_energy_aware_governor(self):
        _, cfg = load()
        self.assertEqual(cfg['general']['desiredgov'], 'schedutil')
        self.assertEqual(cfg['general']['defaultgov'], 'schedutil')

    def test_pins_to_the_big_cores_without_parking(self):
        _, cfg = load()
        self.assertEqual(cfg['cpu']['pin_cores'], 'yes')
        self.assertEqual(cfg['cpu']['park_cores'], 'no')

    def test_sets_nothing_else(self):
        # Clock, GPU and scheduler-class knobs stay GameMode's (and the user's).
        _, cfg = load()
        self.assertEqual(set(cfg.sections()), {'general', 'cpu'})
        self.assertEqual(set(cfg['general']), {'desiredgov', 'defaultgov'})
        self.assertEqual(set(cfg['cpu']), {'pin_cores', 'park_cores'})

    def test_comments_use_gamemode_syntax(self):
        text, _ = load()
        for line in text.splitlines():
            self.assertFalse(line.lstrip().startswith('#'), line)

    def test_pin_sets_on_our_macs(self):
        self.assertEqual(gamemode_pin_set(MACS['M4 Pro (T6040)']), {0, *range(5, 14)})
        self.assertEqual(gamemode_pin_set(MACS['M1 Pro (T6000)']), set(range(2, 10)))
        self.assertEqual(gamemode_pin_set(MACS['M1 (T8103)']), {4, 5, 6, 7})
        self.assertEqual(gamemode_pin_set(MACS['M2 (T8112)']), {4, 5, 6, 7})
        # Two P cores: GameMode leaves placement alone.
        self.assertEqual(gamemode_pin_set(MACS['A18 Pro (T8140)']), set())

    def test_installed_as_admin_config(self):
        make = (ROOT / 'Makefile').read_text()
        self.assertRegex(make, r'install-gamemode:\n\tinstall -Dm644 power/gamemode.ini \$\(DESTDIR\)\$\(SYSCONFDIR\)/gamemode.ini')
        self.assertNotIn('gamemode', re.search(r'install-power:\n((?:\t.*\n)+)', make).group(1))


if __name__ == '__main__':
    unittest.main()
