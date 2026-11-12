import hashlib
import importlib.machinery
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'secboot/aurora-secboot'
loader = importlib.machinery.SourceFileLoader('aurora_secboot', str(CLI))
spec = importlib.util.spec_from_loader('aurora_secboot', loader)
sb = importlib.util.module_from_spec(spec)
loader.exec_module(sb)

MID = '0123456789abcdef0123456789abcdef'
KVER = '7.2.10-400.aurora.fc44.aarch64'
OLD = '7.2.9-400.aurora.fc44.aarch64'


def fake_pe(sections, payload=b''):
    """A minimal PE32+ image with the named sections and no signature."""
    opt_size = 240
    dos = bytearray(0x40)
    dos[:2] = b'MZ'
    struct.pack_into('<I', dos, 0x3c, 0x40)
    coff = b'PE\0\0' + struct.pack('<HHIIIHH', 0xaa64, len(sections), 0, 0, 0, opt_size, 0x22)
    opt = bytearray(opt_size)
    struct.pack_into('<H', opt, 0, 0x20b)
    struct.pack_into('<I', opt, 108, 16)
    secs = b''.join(name.encode().ljust(8, b'\0') + bytes(32) for name in sections)
    return bytes(dos) + coff + bytes(opt) + secs + payload


SECURITY_DIR = 0x40 + 24 + 112 + 4 * 8

FAKE_TOOLS = {
    'ukify': r'''
import json, os, sys
sys.path.insert(0, os.environ['FAKE_LIB'])
from fakepe import fake_pe
a = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write(json.dumps(['ukify'] + a) + '\n')
out = a[a.index('--output') + 1]
cmd = a[a.index('--cmdline') + 1]
open(out, 'wb').write(fake_pe(['.text', '.osrel', '.cmdline', '.uname', '.linux', '.initrd'],
                              b'CMDLINE:' + cmd.encode() + b'\n'))
''',
    'sbsign': r'''
import hashlib, json, os, struct, sys
a = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write(json.dumps(['sbsign'] + a) + '\n')
cert = open(a[a.index('--cert') + 1], 'rb').read()
data = bytearray(open(a[-1], 'rb').read())
struct.pack_into('<II', data, %d, len(data), 16)
open(a[a.index('--output') + 1], 'wb').write(bytes(data) + b'SIG:' + hashlib.sha256(cert).hexdigest().encode())
''' % SECURITY_DIR,
    'sbverify': r'''
import hashlib, json, os, sys
a = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write(json.dumps(['sbverify'] + a) + '\n')
cert = open(a[a.index('--cert') + 1], 'rb').read()
data = open(a[-1], 'rb').read()
sys.exit(0 if data.endswith(b'SIG:' + hashlib.sha256(cert).hexdigest().encode()) else 1)
''',
    'openssl': r'''
import base64, json, os, sys
a = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write(json.dumps(['openssl'] + a) + '\n')
subj = a[a.index('-subj') + 1].encode()
der = b'0\x82FAKE-X509' + subj + os.urandom(8)
open(a[a.index('-keyout') + 1], 'w').write('-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n')
open(a[a.index('-out') + 1], 'w').write('-----BEGIN CERTIFICATE-----\n' + base64.encodebytes(der).decode() +
                                         '-----END CERTIFICATE-----\n')
''',
}


class Fixture:
    """A --root tree with fake ukify, sbsign, sbverify and openssl on PATH."""

    def __init__(self, base, owner='owner', conf=True, extra_conf=''):
        self.base = Path(base)
        self.root = self.base / 'root'
        self.bin = self.base / 'bin'
        self.lib = self.base / 'lib'
        self.log = self.base / 'tools.log'
        for d in (self.bin, self.lib, self.root / 'etc/kernel', self.root / f'usr/lib/modules/{KVER}',
                  self.root / f'usr/lib/modules/{OLD}', self.root / 'boot/efi/EFI/BOOT',
                  self.root / 'usr/lib/systemd/boot/efi', self.root / 'proc/sys/kernel'):
            d.mkdir(parents=True, exist_ok=True)
        (self.lib / 'fakepe.py').write_text('import struct\n\n\n' + inspect.getsource(fake_pe))
        for name, body in FAKE_TOOLS.items():
            p = self.bin / name
            p.write_text(f'#!{sys.executable}\n' + body)
            p.chmod(0o755)
        (self.root / 'etc/machine-id').write_text(MID + '\n')
        (self.root / 'etc/os-release').write_text('NAME="Fedora Linux"\nID=fedora\nVERSION_ID=44\n')
        (self.root / 'etc/kernel/cmdline').write_text('root=UUID=x ro rhgb quiet systemd.debug_shell=1 panic=10\n')
        (self.root / 'proc/sys/kernel/osrelease').write_text(KVER + '\n')
        (self.root / 'usr/lib/systemd/boot/efi/linuxaa64.efi.stub').write_bytes(fake_pe(['.text']))
        for k in (KVER, OLD):
            (self.root / f'usr/lib/modules/{k}/vmlinuz').write_bytes(b'kernel ' + k.encode())
            (self.root / f'boot/initramfs-{k}.img').write_bytes(b'initramfs ' + k.encode())
        (self.root / 'boot/efi/EFI/BOOT/BOOTAA64.EFI').write_bytes(fake_pe(['.text', '.sbat']) + b'shim')
        if conf:
            self.write_conf(owner, extra_conf)

    def write_conf(self, owner, extra=''):
        text = (ROOT / 'secboot/secboot.conf').read_text().replace('KEY_OWNER=aurora', f'KEY_OWNER={owner}')
        (self.root / 'etc/aurora').mkdir(parents=True, exist_ok=True)
        (self.root / 'etc/aurora/secboot.conf').write_text(text + extra)

    @property
    def env(self):
        return {**os.environ, 'PATH': f'{self.bin}:/usr/bin:/bin', 'FAKE_LOG': str(self.log),
                'FAKE_LIB': str(self.lib)}

    def run(self, *args, env=None):
        return subprocess.run([sys.executable, str(CLI), '--root', str(self.root), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env or self.env)

    def calls(self, tool=None):
        if not self.log.exists():
            return []
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c for c in calls if tool is None or c[0] == tool]

    @property
    def esp(self):
        return self.root / 'boot/efi'

    @property
    def keys(self):
        return self.root / 'var/lib/aurora/secboot/keys'

    def uki(self, kver=KVER):
        return self.esp / f'EFI/Linux/aurora-{MID}-{kver}.efi'

    def conf(self):
        return (self.root / 'etc/aurora/secboot.conf').read_text()


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def fixture(self, **kw):
        return Fixture(self.tmp, **kw)


class CmdlineTests(TempCase):
    def test_contract_example(self):
        fx = self.fixture()
        r = fx.run('cmdline')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, 'root=UUID=x ro rhgb quiet lockdown=integrity\n')
        self.assertIn("removed 'systemd.debug_shell=1'", r.stderr)
        self.assertIn("removed 'panic=10'", r.stderr)

    def test_lab_keeps_only_the_dead_man(self):
        fx = self.fixture()
        (fx.root / 'etc/kernel/cmdline').write_text(
            'root=UUID=x ro systemd.debug_shell=1 panic=10 systemd.watchdog_sec=60 init=/bin/sh\n')
        r = fx.run('cmdline', '--lab')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, 'root=UUID=x ro panic=10 systemd.watchdog_sec=60 lockdown=integrity\n')
        self.assertIn('never ship', r.stderr)

    def test_refusal_table(self):
        refused = ['systemd.debug_shell', 'systemd.debug_shell=1', 'systemd.debug-shell=tty9',
                   'rd.systemd.debug_shell', 'init=/bin/sh', 'rdinit=/bin/sh', 'rd.break', 'rd.break=pre-mount',
                   'rd.shell', 'rd.shell=1', 'single', 'emergency', 'rescue', 's', 'S', '1', '-s', '-b',
                   'systemd.unit=rescue.target', 'rd.systemd.unit=emergency.target', 'lockdown=none',
                   'module.sig_enforce=0', 'module.sig-enforce=0', 'efi=novamap', 'panic=10',
                   'systemd.watchdog_sec=60', 'systemd.watchdog-sec=60']
        kept = ['root=UUID=x', 'ro', 'rhgb', 'quiet', 'rootflags=subvol=root', 'rd.luks.uuid=y',
                'systemd.show_status=auto', 'initrd_extra=1', 'singleuser=0', 'apple_dcp.show_notch=1',
                'foo="a b"', 'panicky=1']
        for token in refused:
            self.assertIsNotNone(sb.refused(token, False), token)
        for token in kept:
            self.assertIsNone(sb.refused(token, False), token)
        for token in ('panic=10', 'systemd.watchdog_sec=60'):
            self.assertIsNone(sb.refused(token, True), token)
        for token in ('systemd.debug_shell=1', 'init=/bin/sh', 'lockdown=none', 'efi=novamap'):
            self.assertIsNotNone(sb.refused(token, True), token)

    def test_quoted_values_stay_one_token(self):
        self.assertEqual(sb.split_cmdline('a foo="x y" b\n'), ['a', 'foo="x y"', 'b'])

    def test_proc_cmdline_fallback_and_confidentiality(self):
        fx = self.fixture(extra_conf='LOCKDOWN=confidentiality\n')
        (fx.root / 'etc/kernel/cmdline').unlink()
        (fx.root / 'proc/cmdline').write_text(
            'BOOT_IMAGE=(hd0,gpt2)/vmlinuz-7 root=UUID=x ro initrd=\\initrd lockdown=none\n')
        r = fx.run('cmdline')
        self.assertEqual(r.stdout, 'root=UUID=x ro lockdown=confidentiality\n')
        self.assertEqual(r.stderr.count('removed'), 1)

    def test_bad_config_value_is_refused(self):
        fx = self.fixture(extra_conf='LOCKDOWN=none\n')
        r = fx.run('cmdline')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_usage_error_is_two(self):
        fx = self.fixture()
        self.assertEqual(fx.run('uki').returncode, 2)
        self.assertEqual(fx.run('frobnicate').returncode, 2)


class KeyTests(TempCase):
    def test_ed25519_rfc8032_vector(self):
        seed = bytes.fromhex('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60')
        self.assertEqual(sb.ed25519_public(seed).hex(),
                         'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')

    def test_create_refused_for_aurora_owner(self):
        fx = self.fixture(owner='aurora')
        r = fx.run('keys', 'create')
        self.assertEqual(r.returncode, 1)
        self.assertIn('KEY_OWNER=aurora', r.stderr)
        self.assertFalse(fx.keys.exists())
        self.assertEqual(fx.calls(), [])

    def test_create_makes_every_key_with_modes(self):
        fx = self.fixture()
        r = fx.run('keys', 'create')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(stat.S_IMODE(fx.keys.stat().st_mode), 0o700)
        for k in ('PK', 'KEK', 'db'):
            self.assertEqual(stat.S_IMODE((fx.keys / f'{k}.key').stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((fx.keys / f'{k}.crt').stat().st_mode), 0o644)
        seed = (fx.keys / 'stage2.key').read_bytes()
        self.assertEqual(len(seed), 32)
        self.assertEqual(stat.S_IMODE((fx.keys / 'stage2.key').stat().st_mode), 0o600)
        self.assertEqual((fx.keys / 'stage2.pub').read_bytes(), sb.ed25519_public(seed))
        uuid.UUID((fx.keys / 'owner.guid').read_text().strip())
        reqs = fx.calls('openssl')
        self.assertEqual(len(reqs), 3)
        for c in reqs:
            self.assertEqual(c[1:3], ['req', '-new'])
            self.assertIn('rsa:2048', c)
        self.assertIn('root can sign', r.stdout)
        self.assertEqual([p.name for p in fx.keys.iterdir() if p.name.startswith('.')], [])

    def test_create_refuses_existing_and_force_keeps_stage2(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        seed = (fx.keys / 'stage2.key').read_bytes()
        db = (fx.keys / 'db.crt').read_text()
        r = fx.run('keys', 'create')
        self.assertEqual(r.returncode, 1)
        self.assertIn('--force', r.stderr)
        r = fx.run('keys', 'create', '--force')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual((fx.keys / 'db.crt').read_text(), db)
        self.assertEqual((fx.keys / 'stage2.key').read_bytes(), seed)

    def handoff(self, fx, seed, pub=None, owner='KEY_OWNER=owner\n'):
        d = fx.esp / 'aurora/secboot'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'stage2.key').write_bytes(seed)
        (d / 'stage2.pub').write_bytes(pub if pub is not None else sb.ed25519_public(seed))
        if owner:
            (d / 'owner').write_text(owner)
        return d

    def test_adopt_moves_key_records_owner_and_wipes_esp(self):
        fx = self.fixture(owner='aurora')
        seed = os.urandom(32)
        d = self.handoff(fx, seed)
        r = fx.run('keys', 'adopt')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((fx.keys / 'stage2.key').read_bytes(), seed)
        self.assertEqual(stat.S_IMODE((fx.keys / 'stage2.key').stat().st_mode), 0o600)
        self.assertEqual((fx.keys / 'stage2.pub').read_bytes(), sb.ed25519_public(seed))
        self.assertEqual(sorted(p.name for p in d.iterdir()), [])
        self.assertIn('KEY_OWNER=owner', fx.conf())
        self.assertIn('# aurora (default)', fx.conf())
        again = fx.run('keys', 'adopt')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn('nothing to adopt', again.stdout)

    def test_adopt_mismatch_is_refused_and_left(self):
        fx = self.fixture()
        seed = os.urandom(32)
        d = self.handoff(fx, seed, pub=sb.ed25519_public(os.urandom(32)))
        r = fx.run('keys', 'adopt')
        self.assertEqual(r.returncode, 1)
        self.assertIn('does not match', r.stderr)
        self.assertFalse((fx.keys / 'stage2.key').exists())
        self.assertEqual((d / 'stage2.key').read_bytes(), seed)

    def test_adopt_refuses_a_different_key_already_adopted(self):
        fx = self.fixture()
        fx.keys.mkdir(parents=True)
        (fx.keys / 'stage2.key').write_bytes(os.urandom(32))
        self.handoff(fx, os.urandom(32), owner=None)
        r = fx.run('keys', 'adopt')
        self.assertEqual(r.returncode, 1)
        self.assertIn('different Stage 2 key', r.stderr)

    def test_adopt_from_a_directory(self):
        fx = self.fixture()
        seed = os.urandom(32)
        src = fx.root / 'media/stick'
        src.mkdir(parents=True)
        (src / 'stage2.key').write_bytes(seed)
        (src / 'stage2.pub').write_bytes(sb.ed25519_public(seed))
        r = fx.run('keys', 'adopt', '--from', '/media/stick')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((fx.keys / 'stage2.key').read_bytes(), seed)
        self.assertFalse((src / 'stage2.key').exists())


class UkiTests(TempCase):
    def test_build_signs_with_the_db_key(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        r = fx.run('uki', 'build', KVER)
        self.assertEqual(r.returncode, 0, r.stderr)
        uk = fx.calls('ukify')[0]
        self.assertEqual(uk[1], 'build')
        args = uk[2:]

        def opt(name):
            return args[args.index(name) + 1]
        self.assertEqual(opt('--linux'), str(fx.root / f'usr/lib/modules/{KVER}/vmlinuz'))
        self.assertEqual(opt('--initrd'), str(fx.root / f'boot/initramfs-{KVER}.img'))
        self.assertEqual(opt('--uname'), KVER)
        self.assertEqual(opt('--stub'), str(fx.root / 'usr/lib/systemd/boot/efi/linuxaa64.efi.stub'))
        self.assertEqual(opt('--os-release'), '@' + str(fx.root / 'etc/os-release'))
        self.assertEqual(opt('--cmdline'), 'root=UUID=x ro rhgb quiet lockdown=integrity')
        self.assertNotIn('--devicetree', args)
        sign = fx.calls('sbsign')[0]
        self.assertEqual(sign[sign.index('--key') + 1], str(fx.keys / 'db.key'))
        self.assertEqual(sign[sign.index('--cert') + 1], str(fx.keys / 'db.crt'))
        self.assertTrue(fx.uki().exists())
        self.assertEqual(sb.verdict(str(fx.uki()), None), 'other-key')
        self.assertEqual([p.name for p in fx.uki().parent.iterdir()], [fx.uki().name])
        self.assertIn('sha256', r.stdout)

    def test_build_without_keys_is_unsigned_with_a_warning(self):
        fx = self.fixture(owner='aurora')
        r = fx.run('uki', 'build', KVER)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('unsigned', r.stderr)
        self.assertIn('KEY_OWNER=aurora', r.stderr)
        self.assertEqual(fx.calls('sbsign'), [])
        self.assertEqual(sb.verdict(str(fx.uki()), None), 'unsigned')

    def test_build_options(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        (fx.root / 'tmp').mkdir()
        (fx.root / 'tmp/cmd').write_text('root=/dev/x init=/bin/sh panic=5\n')
        (fx.root / 'tmp/a.img').write_bytes(b'a')
        (fx.root / 'tmp/b.img').write_bytes(b'b')
        r = fx.run('uki', 'build', KVER, '--image', f'/usr/lib/modules/{OLD}/vmlinuz', '--initrd', '/tmp/a.img',
                   '--initrd', '/tmp/b.img', '--cmdline-file', '/tmp/cmd', '--lab', '--output', '/tmp/out.efi',
                   '--no-sign')
        self.assertEqual(r.returncode, 0, r.stderr)
        args = fx.calls('ukify')[0]
        self.assertEqual(args[args.index('--cmdline') + 1], 'root=/dev/x panic=5 lockdown=integrity')
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == '--initrd'],
                         [str(fx.root / 'tmp/a.img'), str(fx.root / 'tmp/b.img')])
        self.assertEqual(args[args.index('--linux') + 1], str(fx.root / f'usr/lib/modules/{OLD}/vmlinuz'))
        self.assertTrue((fx.root / 'tmp/out.efi').exists())
        self.assertEqual(fx.calls('sbsign'), [])

    def test_build_refusals(self):
        fx = self.fixture()
        (fx.root / f'boot/initramfs-{KVER}.img').unlink()
        r = fx.run('uki', 'build', KVER)
        self.assertEqual(r.returncode, 1)
        self.assertIn('no initramfs', r.stderr)
        r = fx.run('uki', 'build', '../../etc')
        self.assertEqual(r.returncode, 1)
        env = {**fx.env, 'PATH': '/nonexistent'}
        r = fx.run('uki', 'build', OLD, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn('ukify is not installed', r.stderr)

    def test_failed_ukify_leaves_no_temporary_files(self):
        fx = self.fixture()
        (fx.bin / 'ukify').write_text(f'#!{sys.executable}\nimport sys\nsys.stderr.write("boom\\n")\nsys.exit(3)\n')
        r = fx.run('uki', 'build', KVER)
        self.assertEqual(r.returncode, 1)
        self.assertIn('boom', r.stderr)
        self.assertEqual(list((fx.esp / 'EFI/Linux').iterdir()), [])

    def test_verify_and_remove(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        fx.run('uki', 'build', KVER)
        fx.run('uki', 'build', OLD, '--no-sign')
        r = fx.run('uki', 'verify')
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'{fx.uki()}: verified', r.stdout)
        self.assertIn(f'{fx.uki(OLD)}: unsigned', r.stdout)
        self.assertEqual(fx.run('uki', 'verify', str(fx.uki())).returncode, 0)
        self.assertEqual(fx.run('uki', 'remove', OLD).returncode, 0)
        self.assertFalse(fx.uki(OLD).exists())
        self.assertEqual(fx.run('uki', 'verify').returncode, 0)
        self.assertIn('nothing to remove', fx.run('uki', 'remove', OLD).stdout)

    def test_verify_other_key(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        fx.run('uki', 'build', KVER)
        fx.run('keys', 'create', '--force')
        r = fx.run('uki', 'verify')
        self.assertEqual(r.returncode, 1)
        self.assertIn('other-key', r.stdout)

    def test_verify_needs_a_db_certificate(self):
        fx = self.fixture(owner='aurora')
        fx.run('uki', 'build', KVER)
        r = fx.run('uki', 'verify')
        self.assertEqual(r.returncode, 1)
        self.assertIn('db.crt', r.stderr)

    def test_version_order(self):
        self.assertEqual(sb.newest(['6.9.1', '6.10.0', '6.10.0~rc7']), '6.10.0')
        self.assertEqual(sb.newest([OLD, KVER]), KVER)
        self.assertEqual(sb.vercmp('7.2.10~rc1', '7.2.10'), -1)


class SecurePathTests(TempCase):
    def ready(self, owner='owner'):
        fx = self.fixture(owner=owner)
        if owner != 'aurora':
            fx.run('keys', 'create')
        fx.run('uki', 'build', OLD)
        fx.run('uki', 'build', KVER)
        return fx

    def test_enable_refused_for_aurora_owner(self):
        fx = self.ready(owner='aurora')
        r = fx.run('enable')
        self.assertEqual(r.returncode, 1)
        self.assertIn('KEY_OWNER=aurora', r.stderr)
        self.assertIn('SECURE_PATH=no', fx.conf())
        self.assertTrue((fx.esp / 'EFI/BOOT/BOOTAA64.EFI').read_bytes().endswith(b'shim'))

    def test_enable_refused_without_a_verified_uki(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        fx.run('uki', 'build', KVER, '--no-sign')
        r = fx.run('enable')
        self.assertEqual(r.returncode, 1)
        self.assertIn('no verified UKI', r.stderr)
        self.assertFalse((fx.esp / sb.SAVED_LOADER).exists())

    def test_enable_installs_the_newest_and_disable_restores_shim(self):
        fx = self.ready()
        shim = (fx.esp / 'EFI/BOOT/BOOTAA64.EFI').read_bytes()
        r = fx.run('enable')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((fx.esp / sb.SAVED_LOADER).read_bytes(), shim)
        self.assertEqual((fx.esp / sb.BOOT_LOADER).read_bytes(), fx.uki().read_bytes())
        self.assertIn('SECURE_PATH=yes          # no:', fx.conf())
        self.assertIn('SECURE_PATH=yes', r.stdout)
        again = fx.run('enable')
        self.assertIn('nothing changed', again.stdout)
        self.assertEqual((fx.esp / sb.SAVED_LOADER).read_bytes(), shim)
        r = fx.run('disable')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((fx.esp / sb.BOOT_LOADER).read_bytes(), shim)
        self.assertFalse((fx.esp / sb.SAVED_LOADER).exists())
        self.assertIn('SECURE_PATH=no', fx.conf())
        self.assertIn('nothing changed', fx.run('disable').stdout)

    def test_disable_refuses_without_a_saved_loader(self):
        fx = self.ready()
        fx.run('enable')
        (fx.esp / sb.SAVED_LOADER).unlink()
        r = fx.run('disable')
        self.assertEqual(r.returncode, 1)
        self.assertIn('no saved loader', r.stderr)

    def test_status_reports_verdicts_and_first_gap(self):
        fx = self.ready()
        fx.run('enable')
        bs = fx.root / 'sys/firmware/aurora/boot_security'
        bs.mkdir(parents=True)
        (bs / 'state').write_text('verified\n')
        (bs / 'advisory').write_text('1\n')
        efivars = fx.root / 'sys/firmware/efi/efivars'
        efivars.mkdir(parents=True)
        (efivars / f'SecureBoot-{sb.EFI_GLOBAL}').write_bytes(b'\x06\0\0\0\x01')
        (fx.root / 'sys/kernel/security').mkdir(parents=True)
        (fx.root / 'sys/kernel/security/lockdown').write_text('none [integrity] confidentiality\n')
        st = json.loads(fx.run('status', '--json').stdout)
        self.assertEqual(st['config']['SECURE_PATH'], 'yes')
        self.assertTrue(st['private_keys_on_machine'])
        self.assertEqual(st['efi']['SecureBoot'], 1)
        self.assertEqual(st['lockdown'], 'integrity')
        self.assertEqual({u['kernel']: u['verdict'] for u in st['ukis']}, {KVER: 'verified', OLD: 'verified'})
        self.assertEqual(st['boot_loader']['kind'], 'uki')
        self.assertEqual(st['boot_loader']['verdict'], 'verified')
        self.assertIn('advisory', st['first_gap'])
        (bs / 'advisory').write_text('0\n')
        text = fx.run('status').stdout
        self.assertIn('first gap: none', text)
        self.assertIn('root can sign', text)
        (efivars / f'SecureBoot-{sb.EFI_GLOBAL}').write_bytes(b'\x06\0\0\0\x00')
        self.assertIn('first gap: UEFI Secure Boot', fx.run('status').stdout)


class SeedTests(TempCase):
    def test_seed_parses_back(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        r = fx.run('seed', '-o', '/seed.var')
        self.assertEqual(r.returncode, 0, r.stderr)
        data = (fx.root / 'seed.var').read_bytes()
        hdr = struct.unpack_from('<QQII', data)
        self.assertEqual(hdr[1], 0x0161566966456255)
        self.assertEqual(hdr[2], len(data))
        v = sb.parse_seed(data)
        self.assertEqual(sorted(v), ['KEK', 'PK', 'db', 'dbx'])
        self.assertEqual(v['PK'][0], uuid.UUID('8be4df61-93ca-11d2-aa0d-00e098032b8c'))
        self.assertEqual(v['db'][0], uuid.UUID('d719b2cb-3d3a-4596-a3bc-dad00e67656f'))
        self.assertEqual({a for _, a, _ in v.values()}, {0x27})
        owner = uuid.UUID((fx.keys / 'owner.guid').read_text().strip())
        for k in ('PK', 'KEK', 'db'):
            esl = v[k][2]
            stype, lsize, hsize, ssize = struct.unpack_from('<16sIII', esl)
            self.assertEqual(uuid.UUID(bytes_le=stype), uuid.UUID('a5c059a1-94e4-4aa7-87b5-ab155c2bf072'))
            self.assertEqual((lsize, hsize), (len(esl), 0))
            self.assertEqual(uuid.UUID(bytes_le=esl[28:44]), owner)
            der = sb.pem_certs((fx.keys / f'{k}.crt').read_text())[0]
            self.assertEqual(esl[44:], der)
            self.assertEqual(ssize, 16 + len(der))
        self.assertEqual(v['dbx'][2], sb.no_revocations())

    def test_seed_refuses_microsoft_ca(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        import base64
        der = b'0\x82' + b'CN=Microsoft Corporation UEFI CA 2011'
        (fx.keys / 'db.crt').write_text('-----BEGIN CERTIFICATE-----\n' + base64.b64encode(der).decode() +
                                        '\n-----END CERTIFICATE-----\n')
        r = fx.run('seed', '-o', '/seed.var')
        self.assertEqual(r.returncode, 1)
        self.assertIn('Microsoft Corporation UEFI CA', r.stderr)
        self.assertFalse((fx.root / 'seed.var').exists())

    def test_seed_needs_keys(self):
        fx = self.fixture(owner='aurora')
        r = fx.run('seed', '-o', '/seed.var')
        self.assertEqual(r.returncode, 1)
        self.assertIn('keys create', r.stderr)


class HookTests(TempCase):
    def plugin(self, fx, *args, env=None):
        env = {**fx.env, 'AURORA_SECBOOT_ROOT': str(fx.root),
               'AURORA_SECBOOT_CLI': str(CLI), **(env or {})}
        return subprocess.run(['bash', str(ROOT / 'secboot/92-aurora-secboot.install'), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def test_plugin_without_config_does_nothing(self):
        fx = self.fixture(conf=False)
        r = self.plugin(fx, 'add', KVER, '/boot/x', str(fx.root / f'usr/lib/modules/{KVER}/vmlinuz'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(fx.calls(), [])

    def test_plugin_auto_needs_a_db_key(self):
        fx = self.fixture(owner='aurora')
        r = self.plugin(fx, 'add', KVER, '/boot/x', str(fx.root / f'usr/lib/modules/{KVER}/vmlinuz'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(fx.calls('ukify'), [])

    def test_plugin_add_and_remove(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        staging = Path(self.tmp) / 'staging'
        staging.mkdir()
        (staging / 'initrd').write_bytes(b'staged')
        image = str(fx.root / f'usr/lib/modules/{KVER}/vmlinuz')
        r = self.plugin(fx, 'add', KVER, str(fx.root / 'boot' / MID / KVER), image,
                        env={'KERNEL_INSTALL_STAGING_AREA': str(staging)})
        self.assertEqual(r.returncode, 0, r.stderr)
        args = fx.calls('ukify')[0]
        self.assertEqual(args[args.index('--initrd') + 1], str(staging / 'initrd'))
        self.assertEqual(args[args.index('--linux') + 1], image)
        self.assertTrue(fx.uki().exists())
        self.assertTrue((fx.esp / sb.BOOT_LOADER).read_bytes().endswith(b'shim'))
        r = self.plugin(fx, 'remove', KVER)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(fx.uki().exists())

    def test_plugin_initrd_arguments_and_uki_build_no(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        extra = fx.root / 'boot/extra.img'
        extra.write_bytes(b'x')
        r = self.plugin(fx, 'add', KVER, '/nonexistent', str(fx.root / f'usr/lib/modules/{KVER}/vmlinuz'),
                        str(extra))
        self.assertEqual(r.returncode, 0, r.stderr)
        args = fx.calls('ukify')[0]
        self.assertEqual(args[args.index('--initrd') + 1], str(extra))
        fx.write_conf('owner', 'UKI_BUILD=no\n')
        fx.log.unlink()
        self.plugin(fx, 'add', OLD, '/x', str(fx.root / f'usr/lib/modules/{OLD}/vmlinuz'))
        self.assertEqual(fx.calls('ukify'), [])

    def test_plugin_refreshes_the_secure_path(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        fx.run('uki', 'build', OLD)
        self.assertEqual(fx.run('enable').returncode, 0)
        r = self.plugin(fx, 'add', KVER, '/x', str(fx.root / f'usr/lib/modules/{KVER}/vmlinuz'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((fx.esp / sb.BOOT_LOADER).read_bytes(), fx.uki().read_bytes())
        r = self.plugin(fx, 'remove', KVER)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((fx.esp / sb.BOOT_LOADER).read_bytes(), fx.uki(OLD).read_bytes())

    def test_plugin_failure_only_fails_on_the_secure_path(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        (fx.root / f'boot/initramfs-{KVER}.img').unlink()
        r = self.plugin(fx, 'add', KVER, '/x', str(fx.root / f'usr/lib/modules/{KVER}/vmlinuz'))
        self.assertEqual(r.returncode, 0)
        self.assertIn('no UKI', r.stderr)
        fx.write_conf('owner', 'SECURE_PATH=yes\n')
        r = self.plugin(fx, 'add', KVER, '/x', str(fx.root / f'usr/lib/modules/{KVER}/vmlinuz'))
        self.assertEqual(r.returncode, 1)

    def mkinitcpio(self, fx, *args, kver=KVER):
        env = {**fx.env, 'AURORA_SECBOOT_ROOT': str(fx.root), 'AURORA_SECBOOT_CLI': str(CLI)}
        if kver is not None:
            env['KERNELVERSION'] = kver
        return subprocess.run(['bash', str(ROOT / 'secboot/aurora-secboot.initcpio-post'), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def test_mkinitcpio_post_hook(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        image = str(fx.root / f'usr/lib/modules/{KVER}/vmlinuz')
        fallback = fx.root / 'boot/initramfs-linux-fallback.img'
        fallback.write_bytes(b'f')
        r = self.mkinitcpio(fx, image, str(fallback))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(fx.calls('ukify'), [])
        main = fx.root / 'boot/initramfs-linux.img'
        main.write_bytes(b'm')
        r = self.mkinitcpio(fx, image, str(main), kver=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        args = fx.calls('ukify')[0]
        self.assertEqual(args[args.index('--initrd') + 1], str(main))
        self.assertEqual(args[args.index('--uname') + 1], KVER)
        self.assertTrue(fx.uki().exists())


class InstallTests(unittest.TestCase):
    def test_default_config_is_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'etc/aurora').mkdir(parents=True)
            shutil.copy(ROOT / 'secboot/secboot.conf', Path(tmp) / 'etc/aurora/secboot.conf')
            cfg = sb.System(tmp).config()
        self.assertTrue(cfg.pop('_present'))
        self.assertEqual(cfg, sb.DEFAULTS)

    def test_adopt_unit(self):
        unit = (ROOT / 'secboot/aurora-secboot-adopt.service').read_text()
        self.assertIn('ConditionPathExists=|/boot/efi/aurora/secboot/stage2.key', unit)
        self.assertIn('ExecStart=/usr/bin/aurora-secboot keys adopt', unit)
        self.assertIn('Type=oneshot', unit)

    def test_nothing_shipped_carries_lab_settings(self):
        for name in ('secboot.conf', '92-aurora-secboot.install', 'aurora-secboot.initcpio-post',
                     'aurora-secboot-adopt.service'):
            text = (ROOT / 'secboot' / name).read_text()
            for lab in ('debug_shell', 'panic=', 'watchdog_sec'):
                self.assertNotIn(lab, text, name)

    @unittest.skipUnless(shutil.which('make') and subprocess.run(['install', '--version'], stdout=subprocess.DEVNULL,
                                                                 stderr=subprocess.DEVNULL).returncode == 0,
                         'needs GNU make and install')
    def test_make_install_secboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['make', '-s', '-C', str(ROOT), 'install-secboot', 'install-secboot-mkinitcpio',
                            f'DESTDIR={tmp}'], check=True)
            files = sorted(str(p.relative_to(tmp)) for p in Path(tmp).rglob('*') if p.is_file())
            self.assertEqual(files, ['etc/aurora/secboot.conf', 'usr/bin/aurora-secboot',
                                     'usr/lib/initcpio/post/aurora-secboot',
                                     'usr/lib/kernel/install.d/92-aurora-secboot.install',
                                     'usr/lib/systemd/system/aurora-secboot-adopt.service',
                                     'usr/share/doc/aurora-secboot/README.md'])
            self.assertEqual(stat.S_IMODE((Path(tmp) / 'var/lib/aurora/secboot').stat().st_mode), 0o700)
            self.assertTrue(os.access(Path(tmp) / 'usr/bin/aurora-secboot', os.X_OK))


if __name__ == '__main__':
    unittest.main()
