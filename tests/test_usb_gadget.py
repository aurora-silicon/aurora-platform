import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'usb-gadget/aurora-usb-gadget'
RULES = ROOT / 'usb-gadget/86-aurora-usb-gadget.rules'
UNIT = ROOT / 'usb-gadget/aurora-usb-gadget@.service'

# Stand-ins for the tools the script calls: each logs its arguments.
STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 0\n'
MACHINE_ID = '0123456789abcdef0123456789abcdef\n'


class UsbGadget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.configfs = base / 'config'
        (self.configfs / 'usb_gadget').mkdir(parents=True)
        self.sysfs = base / 'sys'
        (self.sysfs / 'class/udc').mkdir(parents=True)
        self.run_dir = base / 'run'
        self.run_dir.mkdir()
        self.conf = base / 'etc/aurora/usb-gadget.conf'
        self.machine_id = base / 'machine-id'
        self.machine_id.write_text(MACHINE_ID)
        self.bin = base / 'bin'
        self.bin.mkdir()
        for tool in ('modprobe', 'nmcli', 'networkctl', 'systemctl', 'ip'):
            path = self.bin / tool
            path.write_text(STUB)
            path.chmod(0o755)
        self.log = base / 'calls'
        self.g = self.configfs / 'usb_gadget/aurora'

    def tearDown(self):
        self.tmp.cleanup()

    def udc(self, name, state='configured', speed='high-speed'):
        d = self.sysfs / 'class/udc' / name
        d.mkdir()
        (d / 'state').write_text(state + '\n')
        (d / 'current_speed').write_text(speed + '\n')

    def config(self, text):
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        self.conf.write_text(text)

    def run_gadget(self, *args):
        env = dict(os.environ,
                   PATH=f'{self.bin}:/usr/bin:/bin',
                   AURORA_GADGET_CONFIGFS=str(self.configfs),
                   AURORA_GADGET_SYSFS=str(self.sysfs),
                   AURORA_GADGET_RUN=str(self.run_dir),
                   AURORA_GADGET_CONF=str(self.conf),
                   AURORA_GADGET_MACHINE_ID=str(self.machine_id),
                   STUB_LOG=str(self.log))
        return subprocess.run(['bash', str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=20)

    def read(self, rel):
        return (self.g / rel).read_text().strip()

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    # --- nothing to do ---

    def test_no_controller_is_a_no_op(self):
        r = self.run_gadget('up')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('no USB device controller', r.stdout)
        self.assertFalse(self.g.exists())

    def test_named_controller_missing_is_a_no_op(self):
        self.udc('a.usb')
        r = self.run_gadget('up', 'b.usb')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.g.exists())

    def test_no_configfs_is_a_no_op(self):
        self.udc('a.usb')
        shutil.rmtree(self.configfs / 'usb_gadget')
        r = self.run_gadget('up')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('no USB gadget configfs', r.stdout)
        self.assertTrue(any(c.startswith('modprobe -q -a libcomposite') for c in self.calls()), self.calls())

    def test_disabled(self):
        self.udc('a.usb')
        self.config('ENABLED=no\n')
        r = self.run_gadget('up')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.g.exists())

    def test_down_and_status_without_gadget(self):
        self.assertEqual(self.run_gadget('down').returncode, 0)
        self.assertEqual(self.run_gadget('down', 'a.usb').returncode, 0)
        r = self.run_gadget('status')
        self.assertEqual(r.returncode, 0)
        self.assertIn('not set up', r.stdout)

    # --- the gadget ---

    def test_up_builds_composite_gadget(self):
        self.udc('382280000.usb')
        r = self.run_gadget('up')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read('idVendor'), '0x1d6b')
        self.assertEqual(self.read('idProduct'), '0x0104')
        self.assertEqual(self.read('bDeviceClass'), '0xEF')
        self.assertEqual(self.read('bDeviceSubClass'), '0x02')
        self.assertEqual(self.read('bDeviceProtocol'), '0x01')
        self.assertEqual(self.read('strings/0x409/manufacturer'), 'Aurora Silicon')
        self.assertEqual(self.read('strings/0x409/product'), 'Aurora USB Gadget')
        self.assertEqual(self.read('functions/ncm.usb0/ifname'), 'usbaurora%d')
        links = sorted(p.name for p in (self.g / 'configs/c.1').iterdir() if p.is_symlink())
        self.assertEqual(links, ['acm.GS0', 'ncm.usb0'])
        self.assertEqual(self.read('UDC'), '382280000.usb')
        self.assertIn('bound to 382280000.usb', r.stdout)

    def test_identity_is_stable_and_per_machine(self):
        self.udc('a.usb')
        self.run_gadget('up')
        dev, host = self.read('functions/ncm.usb0/dev_addr'), self.read('functions/ncm.usb0/host_addr')
        serial = self.read('strings/0x409/serialnumber')
        for m in (dev, host):
            self.assertRegex(m, r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$')
            first = int(m[:2], 16)
            self.assertEqual(first & 0x03, 0x02, m)  # locally administered, unicast
        self.assertNotEqual(dev, host)
        self.assertRegex(serial, r'^[0-9a-f]{16}$')
        self.assertNotIn(serial, MACHINE_ID)

        self.run_gadget('down')
        self.run_gadget('up')
        self.assertEqual(self.read('functions/ncm.usb0/dev_addr'), dev)
        self.assertEqual(self.read('strings/0x409/serialnumber'), serial)

        self.run_gadget('down')
        self.machine_id.write_text('fedcba9876543210fedcba9876543210\n')
        self.run_gadget('up')
        self.assertNotEqual(self.read('functions/ncm.usb0/dev_addr'), dev)
        self.assertNotEqual(self.read('strings/0x409/serialnumber'), serial)

    def test_up_is_idempotent(self):
        self.udc('a.usb')
        self.run_gadget('up', 'a.usb')
        (self.g / 'idVendor').write_text('untouched\n')
        r = self.run_gadget('up', 'a.usb')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('already bound to a.usb', r.stdout)
        self.assertEqual(self.read('idVendor'), 'untouched')

    def test_second_controller_waits_then_takes_over(self):
        self.udc('a.usb')
        self.udc('b.usb')
        self.run_gadget('up', 'a.usb')
        r = self.run_gadget('up', 'b.usb')
        self.assertIn('b.usb left free', r.stdout)
        self.assertEqual(self.read('UDC'), 'a.usb')
        # Unplug from a.usb: its service stops, the gadget moves to b.usb.
        shutil.rmtree(self.sysfs / 'class/udc/a.usb')
        r = self.run_gadget('down', 'a.usb')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read('UDC'), 'b.usb')
        # Stopping a.usb's service again leaves b.usb alone.
        self.run_gadget('down', 'a.usb')
        self.assertEqual(self.read('UDC'), 'b.usb')

    def test_stale_binding_is_rebuilt(self):
        self.udc('a.usb')
        self.run_gadget('up', 'a.usb')
        shutil.rmtree(self.sysfs / 'class/udc/a.usb')
        self.udc('a.usb')
        (self.g / 'UDC').write_text('gone.usb\n')
        self.run_gadget('up', 'a.usb')
        self.assertEqual(self.read('UDC'), 'a.usb')

    def test_down_removes_everything(self):
        self.udc('a.usb')
        (self.run_dir / 'NetworkManager').mkdir()
        self.run_gadget('up')
        self.assertTrue((self.run_dir / 'NetworkManager/system-connections/aurora-usb-gadget.nmconnection').exists())
        r = self.run_gadget('down')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.g.exists())
        self.assertFalse((self.run_dir / 'NetworkManager/system-connections/aurora-usb-gadget.nmconnection').exists())

    def test_functions_from_config(self):
        self.udc('a.usb')
        self.config('FUNCTIONS=ncm\n')
        self.run_gadget('up')
        self.assertFalse((self.g / 'functions/acm.GS0').exists())
        self.assertFalse((self.g / 'configs/c.1/acm.GS0').exists())
        self.assertTrue((self.g / 'configs/c.1/ncm.usb0').is_symlink())

    # --- disk ---

    def test_storage_read_only_by_default(self):
        self.udc('a.usb')
        image = Path(self.tmp.name) / 'disk.img'
        image.write_bytes(b'\0' * 4096)
        self.config(f'STORAGE_IMAGE="{image}"\n')
        self.run_gadget('up')
        lun = 'functions/mass_storage.usb0/lun.0'
        self.assertEqual(self.read(f'{lun}/file'), str(image))
        self.assertEqual(self.read(f'{lun}/ro'), '1')
        self.assertEqual(self.read(f'{lun}/cdrom'), '0')
        self.assertTrue((self.g / 'configs/c.1/mass_storage.usb0').is_symlink())
        self.assertTrue(any('usb_f_mass_storage' in c for c in self.calls()), self.calls())
        # Unplugged: the image is let go.
        self.run_gadget('down', 'a.usb')
        self.assertEqual(self.read(f'{lun}/file'), '')

    def test_storage_writable_and_cdrom(self):
        self.udc('a.usb')
        image = Path(self.tmp.name) / 'disk.iso'
        image.write_bytes(b'\0' * 4096)
        self.config(f'STORAGE_IMAGE={image}\nSTORAGE_READONLY=no\nSTORAGE_CDROM=yes\n')
        self.run_gadget('up')
        self.assertEqual(self.read('functions/mass_storage.usb0/lun.0/ro'), '0')
        self.assertEqual(self.read('functions/mass_storage.usb0/lun.0/cdrom'), '1')

    def test_storage_image_refused(self):
        self.udc('a.usb')
        image = Path(self.tmp.name) / 'disk.img'
        image.write_bytes(b'\0' * 4096)
        self.config(f'STORAGE_IMAGE={image}\n')
        self.run_gadget('up')
        self.run_gadget('down', 'a.usb')
        # The kernel refuses the backing file: the attribute keeps its old value.
        (self.g / 'functions/mass_storage.usb0/lun.0/file').chmod(0o444)
        r = self.run_gadget('up', 'a.usb')
        self.assertIn('did not take', r.stdout)

    def test_storage_image_missing(self):
        self.udc('a.usb')
        self.config('STORAGE_IMAGE=/nonexistent/disk.img\n')
        r = self.run_gadget('up')
        self.assertIn('not found', r.stdout)
        self.assertFalse((self.g / 'functions/mass_storage.usb0').exists())
        self.assertEqual(self.read('UDC'), 'a.usb')

    # --- network ---

    def nm_profile(self):
        path = self.run_dir / 'NetworkManager/system-connections/aurora-usb-gadget.nmconnection'
        return path, path.read_text()

    def test_networkmanager_link_local(self):
        self.udc('a.usb')
        (self.run_dir / 'NetworkManager').mkdir()
        self.run_gadget('up')
        # The link comes up with NetworkManager too: the host only sees the network once it is up.
        (self.g / 'functions/ncm.usb0/ifname').write_text('usbaurora0\n')
        self.run_gadget('down', 'a.usb')
        self.run_gadget('up', 'a.usb')
        self.assertIn('ip link set dev usbaurora0 up', self.calls())
        path, text = self.nm_profile()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn('interface-name=usbaurora*', text)
        self.assertRegex(text, r'\[ipv4\]\nmethod=link-local')
        self.assertRegex(text, r'\[ipv6\]\nmethod=link-local')
        self.assertIn(f'nmcli connection load {path}', self.calls())

    def test_networkmanager_shared(self):
        self.udc('a.usb')
        (self.run_dir / 'NetworkManager').mkdir()
        self.config('IPV4=shared\n')
        self.run_gadget('up')
        self.assertRegex(self.nm_profile()[1], r'\[ipv4\]\nmethod=shared')

    def test_networkd(self):
        self.udc('a.usb')
        (self.run_dir / 'systemd/netif').mkdir(parents=True)
        self.run_gadget('up')
        path = self.run_dir / 'systemd/network/70-aurora-usb-gadget.network'
        text = path.read_text()
        self.assertIn('Name=usbaurora*', text)
        self.assertIn('LinkLocalAddressing=yes', text)
        self.assertNotIn('DHCPServer', text)
        self.assertIn('networkctl reload', self.calls())
        self.run_gadget('down')
        self.config('IPV4=shared\n')
        self.run_gadget('up')
        text = path.read_text()
        self.assertIn('DHCPServer=yes', text)
        self.assertIn('Address=10.42.0.1/24', text)

    def test_no_network_manager_brings_link_up(self):
        self.udc('a.usb')
        self.run_gadget('up')
        # The kernel resolves the name at bind; the fake tree cannot, so set it.
        (self.g / 'functions/ncm.usb0/ifname').write_text('usbaurora0\n')
        self.run_gadget('down', 'a.usb')
        r = self.run_gadget('up', 'a.usb')
        self.assertIn('network interface usbaurora0', r.stdout)
        self.assertIn('ip link set dev usbaurora0 up', self.calls())
        self.assertEqual(self.read('functions/ncm.usb0/ifname'), 'usbaurora0')

    # --- serial console ---

    def test_console_off_by_default(self):
        self.udc('a.usb')
        self.run_gadget('up')
        self.assertIn('systemctl --no-block stop serial-getty@ttyGS0.service', self.calls())
        self.assertNotIn('systemctl --no-block start serial-getty@ttyGS0.service', self.calls())

    def test_console_enable_and_disable(self):
        self.udc('a.usb')
        self.config('# comment\nCONSOLE=no\nIPV4=shared\n')
        self.run_gadget('up')
        r = self.run_gadget('console', 'enable')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.conf.read_text(), '# comment\nCONSOLE=yes\nIPV4=shared\n')
        self.assertIn('systemctl --no-block start serial-getty@ttyGS0.service', self.calls())
        self.assertIn('login prompt enabled', self.run_gadget('status').stdout)
        self.run_gadget('console', 'disable')
        self.assertIn('CONSOLE=no\n', self.conf.read_text())
        self.assertEqual(self.calls()[-1], 'systemctl --no-block stop serial-getty@ttyGS0.service')

    def test_console_enable_without_config_file(self):
        r = self.run_gadget('console', 'enable')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.conf.read_text(), 'CONSOLE=yes\n')
        self.assertEqual(self.run_gadget('console', 'bogus').returncode, 2)

    # --- status ---

    def test_status(self):
        self.udc('a.usb', state='configured', speed='super-speed')
        self.run_gadget('up')
        r = self.run_gadget('status')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('gadget: bound to a.usb', r.stdout)
        self.assertIn('state: configured', r.stdout)
        self.assertIn('speed: super-speed', r.stdout)
        self.assertIn('usb id: 0x1d6b:0x0104', r.stdout)
        self.assertRegex(r.stdout, r'network: \(not registered\), MAC 02|network: .*MAC [0-9a-f]{2}:')
        self.assertIn('serial: /dev/ttyGS0, login prompt disabled', r.stdout)

    def test_usage(self):
        self.assertEqual(self.run_gadget().returncode, 2)
        self.assertEqual(self.run_gadget('--help').returncode, 0)


class DataRole(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sysfs = Path(self.tmp.name) / 'sys'
        (self.sysfs / 'class/typec').mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def port(self, name, role='[host] device', partner=False, label=None):
        d = self.sysfs / 'class/typec' / name
        d.mkdir()
        (d / 'data_role').write_text(role + '\n')
        if partner:
            (self.sysfs / 'class/typec' / f'{name}-partner').mkdir()
        if label:
            node = d / 'device/of_node/connector'
            node.mkdir(parents=True)
            (node / 'label').write_bytes(label.encode() + b'\0')
        return d / 'data_role'

    def role(self, *args):
        env = dict(os.environ, AURORA_GADGET_SYSFS=str(self.sysfs),
                   AURORA_GADGET_CONF=str(Path(self.tmp.name) / 'none.conf'))
        return subprocess.run(['bash', str(SCRIPT), 'role', *args], env=env,
                              capture_output=True, text=True, timeout=20)

    def test_swap_on_the_only_port_with_a_partner(self):
        self.port('port0')
        attr = self.port('port1', partner=True, label='USB-C Left Back')
        r = self.role('device')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(attr.read_text(), 'device\n')
        self.assertIn('port1 (USB-C Left Back): data role host', r.stdout)
        self.assertIn('port1 (USB-C Left Back): data role device', r.stdout)

    def test_port_by_name_or_label(self):
        a = self.port('port0', partner=True, label='USB-C Right')
        b = self.port('port1', partner=True, label='USB-C Left Back')
        self.assertEqual(self.role('device', 'USB-C Left Back').returncode, 0)
        self.assertEqual(b.read_text(), 'device\n')
        self.assertEqual(self.role('device', 'port0').returncode, 0)
        self.assertEqual(a.read_text(), 'device\n')
        r = self.role('device', 'port7')
        self.assertEqual(r.returncode, 1)
        self.assertIn('no USB-C port port7', r.stdout)

    def test_ambiguous_or_no_partner(self):
        self.port('port0', partner=True, label='USB-C Right')
        self.port('port1', partner=True)
        r = self.role('host')
        self.assertEqual(r.returncode, 1)
        self.assertIn('port0 (USB-C Right)', r.stdout)
        self.assertIn('  port1', r.stdout)
        shutil.rmtree(self.sysfs / 'class/typec/port0-partner')
        shutil.rmtree(self.sysfs / 'class/typec/port1-partner')
        r = self.role('host')
        self.assertEqual(r.returncode, 1)
        self.assertIn('no USB-C port has anything attached', r.stdout)

    def test_already_in_role(self):
        attr = self.port('port0', role='host [device]', partner=True)
        r = self.role('device')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(attr.read_text(), 'host [device]\n')

    def test_role_did_not_change(self):
        d = self.sysfs / 'class/typec/port0'
        d.mkdir()
        (self.sysfs / 'class/typec/port0-partner').mkdir()
        # Writes vanish and the role reads back unchanged, as when the
        # partner refuses the swap.
        (d / 'data_role').symlink_to('/dev/null')
        r = self.role('device')
        self.assertEqual(r.returncode, 1)
        self.assertIn('did not change to device', r.stdout)

    def test_bad_arguments(self):
        self.assertEqual(self.role('sideways').returncode, 2)


class Integration(unittest.TestCase):
    def test_udev_rule_starts_the_service_per_controller(self):
        text = RULES.read_text()
        self.assertRegex(text, r'SUBSYSTEM=="udc", TAG\+="systemd", '
                               r'ENV\{SYSTEMD_ALIAS\}="/sys/subsystem/udc/devices/%k", '
                               r'ENV\{SYSTEMD_WANTS\}\+="aurora-usb-gadget@%k.service"')
        self.assertIn('ENV{DEVTYPE}=="gadget", KERNEL=="usbaurora[0-9]*", ENV{NM_UNMANAGED}="0"', text)
        # After NetworkManager's 85-nm-unmanaged.rules, before 99-systemd.rules.
        self.assertTrue(85 < int(RULES.name.split('-')[0]) < 99)

    def test_unit_binds_to_the_controller(self):
        text = UNIT.read_text()
        self.assertIn('BindsTo=sys-subsystem-udc-devices-%i.device', text)
        self.assertIn('ExecStart=/usr/bin/aurora-usb-gadget up %I', text)
        self.assertIn('ExecStop=/usr/bin/aurora-usb-gadget down %I', text)
        self.assertNotIn('[Install]', text)

    def test_shipped_config_matches_defaults(self):
        conf = (ROOT / 'usb-gadget/usb-gadget.conf').read_text()
        keys = dict(re.findall(r'^([A-Z0-9_]+)=(.*)$', conf, re.M))
        self.assertEqual(keys, {'ENABLED': 'yes', 'FUNCTIONS': '"ncm acm"', 'IPV4': 'link-local',
                                'CONSOLE': 'no', 'STORAGE_IMAGE': '', 'STORAGE_READONLY': 'yes',
                                'STORAGE_CDROM': 'no'})

    @unittest.skipUnless(shutil.which('shellcheck'), 'shellcheck not installed')
    def test_shellcheck(self):
        r = subprocess.run(['shellcheck', str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == '__main__':
    unittest.main()
