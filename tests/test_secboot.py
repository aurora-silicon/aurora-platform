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


class ImageBuilderTests(TempCase):
    """The options an image builder (the NixOS module) passes."""

    def nixos(self):
        fx = self.fixture(conf=False)
        conf = fx.root / 'nix/store/x-secboot.conf'
        conf.parent.mkdir(parents=True)
        conf.write_text('KEY_OWNER=owner\nSECURE_PATH=yes\nKEY_DIR=/var/lib/aurora/secboot/keys\nESP=/boot\n')
        (fx.root / 'etc/aurora').mkdir(parents=True)
        os.symlink(conf, fx.root / 'etc/aurora/secboot.conf')
        gen = fx.root / 'nix/var/system-42'
        gen.mkdir(parents=True)
        (gen / 'kernel').write_bytes(b'kernel')
        (gen / 'kernel-params').write_text('init=/old root=fstab systemd.debug_shell loglevel=4\n')
        (fx.root / 'boot/EFI/BOOT').mkdir(parents=True)
        (fx.root / 'boot/EFI/BOOT/BOOTAA64.EFI').write_bytes(fake_pe(['.text']) + b'systemd-boot')
        (fx.root / 'tmp').mkdir()
        (fx.root / 'tmp/initrd').write_bytes(b'initrd')
        return fx, '/etc/aurora/secboot.conf', '/nix/var/system-42'

    def build(self, fx, conf, gen, *extra):
        return fx.run('--config', conf, 'uki', 'build', '7.2.10', '--image', f'{gen}/kernel', '--initrd', '/tmp/initrd',
                      '--cmdline-file', f'{gen}/kernel-params', '--init', f'{gen}/init',
                      '--output', '/boot/EFI/Linux/aurora-nixos-abc.efi', *extra)

    def test_config_option_replaces_the_default_file(self):
        fx = self.fixture(conf=False)
        (fx.root / 'srv').mkdir()
        (fx.root / 'srv/sb.conf').write_text('LOCKDOWN=confidentiality\n')
        self.assertTrue(fx.run('cmdline').stdout.strip().endswith('lockdown=integrity'))
        r = fx.run('--config', '/srv/sb.conf', 'cmdline')
        self.assertTrue(r.stdout.strip().endswith('lockdown=confidentiality'), r.stdout)
        (fx.root / 'srv/sb.conf').write_text('LOCKDOWN=none\n')
        r = fx.run('--config', '/srv/sb.conf', 'cmdline')
        self.assertEqual(r.returncode, 1)
        self.assertIn('/srv/sb.conf', r.stderr)

    def test_init_is_added_after_the_filter(self):
        fx, conf, gen = self.nixos()
        fx.run('--config', conf, 'keys', 'create')
        r = self.build(fx, conf, gen)
        self.assertEqual(r.returncode, 0, r.stderr)
        args = fx.calls('ukify')[0]
        self.assertEqual(args[args.index('--cmdline') + 1],
                         f'root=fstab loglevel=4 init={gen}/init lockdown=integrity')
        self.assertIn("removed 'init=/old'", r.stderr)
        self.assertTrue((fx.root / 'boot/EFI/Linux/aurora-nixos-abc.efi').exists())

    def test_init_must_be_one_absolute_path(self):
        fx, conf, gen = self.nixos()
        for bad in ('relative/init', '/nix/a b', '/nix/"x'):
            r = fx.run('--config', conf, 'uki', 'build', '7.2.10', '--image', f'{gen}/kernel', '--initrd',
                       '/tmp/initrd', '--init', bad, '--output', '/out.efi')
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn('--init', r.stderr)
        self.assertEqual(fx.calls('ukify'), [])

    def test_stub_option_and_environment(self):
        fx, conf, gen = self.nixos()
        (fx.root / 'usr/lib/systemd/boot/efi/linuxaa64.efi.stub').unlink()
        r = self.build(fx, conf, gen)
        self.assertEqual(r.returncode, 1)
        self.assertIn('stub', r.stderr)
        (fx.root / 'nix/store/s').mkdir()
        (fx.root / 'nix/store/s/env.stub').write_bytes(fake_pe(['.text']))
        (fx.root / 'nix/store/s/opt.stub').write_bytes(fake_pe(['.text']))
        env = {**fx.env, 'AURORA_SECBOOT_STUB': '/nix/store/s/env.stub'}
        r = fx.run('--config', conf, 'uki', 'build', '7.2.10', '--image', f'{gen}/kernel', '--initrd', '/tmp/initrd',
                   '--output', '/a.efi', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        args = fx.calls('ukify')[-1]
        self.assertEqual(args[args.index('--stub') + 1], str(fx.root / 'nix/store/s/env.stub'))
        r = fx.run('--config', conf, 'uki', 'build', '7.2.10', '--image', f'{gen}/kernel', '--initrd', '/tmp/initrd',
                   '--output', '/b.efi', '--stub', '/nix/store/s/opt.stub', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        args = fx.calls('ukify')[-1]
        self.assertEqual(args[args.index('--stub') + 1], str(fx.root / 'nix/store/s/opt.stub'))

    def test_os_release_option_and_sandbox_fallback(self):
        fx, conf, gen = self.nixos()
        (fx.root / 'nix/store/s').mkdir(exist_ok=True)
        (fx.root / 'nix/store/s/os-release').write_text('ID=nixos\nNAME=NixOS\n')
        r = fx.run('--config', conf, 'uki', 'build', '7.2.10', '--image', f'{gen}/kernel', '--initrd', '/tmp/initrd',
                   '--output', '/a.efi', '--os-release', '/nix/store/s/os-release')
        self.assertEqual(r.returncode, 0, r.stderr)
        args = fx.calls('ukify')[-1]
        self.assertEqual(args[args.index('--os-release') + 1], '@' + str(fx.root / 'nix/store/s/os-release'))
        r = fx.run('--config', conf, 'uki', 'build', '7.2.10', '--image', f'{gen}/kernel', '--initrd', '/tmp/initrd',
                   '--output', '/b.efi', '--os-release', '/nix/store/s/missing')
        self.assertEqual(r.returncode, 1)
        self.assertIn('missing', r.stderr)
        # No os-release anywhere (a build sandbox): ukify still gets one.
        for f in ('etc/os-release', 'usr/lib/os-release'):
            if (fx.root / f).exists():
                (fx.root / f).unlink()
        r = fx.run('--config', conf, 'uki', 'build', '7.2.10', '--image', f'{gen}/kernel', '--initrd', '/tmp/initrd',
                   '--output', '/c.efi')
        self.assertEqual(r.returncode, 0, r.stderr)
        args = fx.calls('ukify')[-1]
        text = args[args.index('--os-release') + 1]
        self.assertFalse(text.startswith('@'))
        self.assertIn('PRETTY_NAME="Linux 7.2.10"', text)

    def test_enable_given_uki_and_read_only_config(self):
        fx, conf, gen = self.nixos()
        fx.run('--config', conf, 'keys', 'create')
        self.build(fx, conf, gen)
        uki = '/boot/EFI/Linux/aurora-nixos-abc.efi'
        self.assertEqual(fx.run('--config', conf, 'uki', 'verify', uki).returncode, 0)
        loader = fx.root / 'boot/EFI/BOOT/BOOTAA64.EFI'
        saved = fx.root / 'boot' / sb.SAVED_LOADER
        before = (fx.root / 'nix/store/x-secboot.conf').read_text()
        cfg = fx.root / 'nix/store/x-secboot.conf'
        cfg.write_text(before.replace('SECURE_PATH=yes', 'SECURE_PATH=no'))
        r = fx.run('--config', conf, 'enable', '--uki', uki)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(loader.read_bytes(), (fx.root / uki.lstrip('/')).read_bytes())
        self.assertTrue(saved.read_bytes().endswith(b'systemd-boot'))
        self.assertIn('system configuration', r.stdout)
        self.assertIn('SECURE_PATH=no', cfg.read_text())
        # A UKI that does not verify is refused.
        fx.run('--config', conf, 'uki', 'build', '7.2.10', '--image', f'{gen}/kernel', '--initrd', '/tmp/initrd',
               '--output', '/boot/EFI/Linux/unsigned.efi', '--no-sign')
        r = fx.run('--config', conf, 'enable', '--uki', '/boot/EFI/Linux/unsigned.efi')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not a UKI signed', r.stderr)
        r = fx.run('--config', conf, 'disable')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(loader.read_bytes().endswith(b'systemd-boot'))
        self.assertFalse(saved.exists())

    def test_adopt_with_read_only_config(self):
        fx, conf, gen = self.nixos()
        seed = os.urandom(32)
        d = fx.root / 'boot/aurora/secboot'
        d.mkdir(parents=True)
        (d / 'stage2.key').write_bytes(seed)
        (d / 'stage2.pub').write_bytes(sb.ed25519_public(seed))
        (d / 'owner').write_text('KEY_OWNER=both\n')
        r = fx.run('--config', conf, 'keys', 'adopt')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((fx.keys / 'stage2.key').read_bytes(), seed)
        self.assertFalse((d / 'stage2.key').exists())
        self.assertIn('KEY_OWNER=owner', (fx.root / 'nix/store/x-secboot.conf').read_text())
        self.assertIn('system configuration', r.stderr)


class LoaderRefreshTests(TempCase):
    def ready(self):
        fx = self.fixture()
        fx.run('keys', 'create')
        fx.run('uki', 'build', KVER)
        return fx, fx.esp / sb.BOOT_LOADER, fx.esp / sb.SAVED_LOADER

    def test_enable_refreshes_the_saved_loader_after_a_loader_update(self):
        fx, loader, saved = self.ready()
        fx.run('enable')
        new_shim = fake_pe(['.text', '.sbat']) + b'shim 16.2'
        loader.write_bytes(new_shim)
        r = fx.run('enable')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(saved.read_bytes(), new_shim)
        self.assertEqual(loader.read_bytes(), fx.uki().read_bytes())
        self.assertEqual(fx.run('disable').returncode, 0)
        self.assertEqual(loader.read_bytes(), new_shim)

    def test_disable_keeps_a_fresh_loader_and_drops_the_stale_copy(self):
        fx, loader, saved = self.ready()
        fx.run('enable')
        new_shim = fake_pe(['.text', '.sbat']) + b'shim 16.2'
        loader.write_bytes(new_shim)
        r = fx.run('disable')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(loader.read_bytes(), new_shim)
        self.assertFalse(saved.exists())
        self.assertIn('stale', r.stdout)


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


# --- Stage 1 for an existing install, Stage 2 signing ---------------------------------------------

AURA = os.environ.get('AURORA_TEST_AURA') or str(ROOT.parent / 'aura')
HAVE_AURA = all(os.path.exists(os.path.join(AURA, 'tools', n)) for n in
                ('fill_stage1_config.py', 'secboot_sign.py', 'macho_boot_object.py', 'recovery/enroll-stage1.sh'))
ESP_UUID = '5a3c8f1e-2b4d-4c6e-8f00-1234567890ab'
SEED = bytes(range(32))


def raw_stage1(flavour='secure', keys=True):
    """A raw aura Stage 1 with the blocks and strings the tools look for."""
    raw = bytearray(0x40000)
    raw[:8] = bytes.fromhex('090680d2b4020014')
    raw[0x808:0x810] = b'AURALOAD'
    struct.pack_into('<Q', raw, 0x818, 0x200000)
    raw[0x20000:0x20014] = b'AURORA-S1-CFG01\0' + struct.pack('<I', 1)
    if keys:
        raw[0x21000:0x21014] = b'AURORA-S1-KEY01\0' + struct.pack('<I', 1)
    if flavour:
        tag = b'chosen.aurora,stage1-flavour=' + flavour.encode() + b'\0'
        raw[0x22000:0x22000 + len(tag)] = tag
    raw[0x23000:0x23008] = b'AURSIGT1'
    raw[-8:] = b'STACKBOT'
    return bytes(raw)


def stage2_image(old=False, lines=b'chosen.x=1\n'):
    """An aura Stage 2 laid out like the build (footprint = image size, STACKBOT at its end); a
    current one holds the measure line format string in its code, as aura's does."""
    head = bytearray(0x10000)
    head[0x808:0x810] = b'AURALOAD'
    struct.pack_into('<IIQ', head, 0x810, 1, 1, len(head))
    if not old:
        head[0x9000:0x9008] = b'AURSIGT1'
        head[0x9100:0x9117] = b'aurora,boot-security-v1'
        head[0x9200:0x9220] = b'chosen.aurora,measure-%d=%s;%s;\0'
    head[-8:] = b'STACKBOT'
    return bytes(head) + b'\xd0\x0d\xfe\xed' + bytes(60) + b'\x1f\x8b' + bytes(30) + lines


def key_block(obj):
    off = obj.index(b'AURORA-S1-KEY01\0') + 16
    version, count, floor, flags = struct.unpack_from('<IIII', obj, off)
    return count, floor, flags, obj[off + 16:off + 16 + 32 * count]


@unittest.skipUnless(HAVE_AURA, 'needs aura tools (an aura checkout next to this repo, or AURORA_TEST_AURA)')
class Stage1Tests(TempCase):
    def ready(self, chip='t8112', board='j413', owner='owner', extra='', flavour='secure', stage2=True):
        fx = Fixture(tempfile.mkdtemp(dir=self.tmp), owner=owner, extra_conf='ESP=/boot/efi\n' + extra)
        dt = fx.root / 'proc/device-tree'
        (dt / 'chosen').mkdir(parents=True)
        (dt / 'compatible').write_bytes(f'apple,{board}\0apple,{chip}\0apple,arm-platform\0'.encode())
        (dt / 'chosen/asahi,efi-system-partition').write_bytes(ESP_UUID.encode() + b'\0')
        aura = fx.root / 'usr/share/aura'
        (aura / 'stage1').mkdir(parents=True)
        (aura / 'tools').mkdir()
        for n in ('fill_stage1_config.py', 'secboot_sign.py', 'macho_boot_object.py'):
            shutil.copy(os.path.join(AURA, 'tools', n), aura / 'tools' / n)
        shutil.copy(os.path.join(AURA, 'tools/recovery/enroll-stage1.sh'), aura / 'tools/enroll-stage1.sh')
        (aura / 'stage1/aura-secure-raw.bin').write_bytes(raw_stage1(flavour))
        (aura / 'stage1/aura-raw.bin').write_bytes(raw_stage1(None, keys=False))
        if owner != 'aurora':
            fx.keys.mkdir(parents=True)
            (fx.keys / 'stage2.key').write_bytes(SEED)
            (fx.keys / 'stage2.pub').write_bytes(sb.ed25519_public(SEED))
        if stage2:
            name = 'boot-ctxr.bin' if sb.uses_execute_window(int(chip[1:], 16)) else 'boot.bin'
            (fx.esp / 'm1n1').mkdir(parents=True)
            (fx.esp / 'm1n1' / name).write_bytes(stage2_image())
        return fx

    def record(self, fx, verdict='verified', policy='audit', key_id=None):
        node = fx.root / 'proc/device-tree/chosen/aurora,boot-security'
        m = node / 'measurement@0'
        m.mkdir(parents=True)
        (node / 'state').write_bytes(b'partial\0')
        (node / 'stage1-flavour').write_bytes(b'secure\0')
        for k, v in (('stage', 'stage1'), ('policy', policy), ('component', 'next-stage'), ('verdict', verdict)):
            (m / k).write_bytes(v.encode() + b'\0')
        kid = key_id or hashlib.sha256(sb.ed25519_public(SEED)).digest()[:8]
        (m / 'key-id').write_bytes(kid)
        (m / 'security-version').write_bytes(struct.pack('>I', 1))
        (m / 'sha256').write_bytes(bytes(32))

    def objects(self, fx):
        return fx.esp / 'aurora/stage1'

    def test_audit_build_fills_this_install_and_prints_the_1tr_command(self):
        fx = self.ready()
        r = fx.run('stage1', 'build', '--audit', '--volume', 'Fedora')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.objects(fx)
        obj = (out / 'stage1-audit.macho').read_bytes()
        self.assertEqual(obj[:4], struct.pack('<I', 0xFEEDFACF))
        count, floor, flags, keys = key_block(obj)
        self.assertEqual((count, floor, flags, keys), (1, 1, 0, sb.ed25519_public(SEED)))
        self.assertIn(ESP_UUID.encode(), obj)
        self.assertIn(b';m1n1/boot.bin', obj)
        stock = (out / 'stage1-stock.macho').read_bytes()
        self.assertIn(ESP_UUID.encode(), stock)
        self.assertNotIn(b'AURORA-S1-KEY01', stock)
        self.assertTrue((out / 'enroll-stage1.sh').exists())
        md5 = hashlib.md5(obj).hexdigest()
        smd5 = hashlib.md5(stock).hexdigest()
        cmd = f'bash enroll-stage1.sh --keep stage1-stock.macho {smd5} Fedora stage1-audit.macho {md5}'
        self.assertIn(cmd, r.stdout)
        self.assertIn(cmd, (out / 'ENROLL-audit.txt').read_text())
        self.assertNotIn('--compress', r.stdout)
        manifests = list((fx.root / 'var/lib/aurora/secboot/stage1').glob('*-audit.json'))
        self.assertEqual(len(manifests), 1)
        m = json.loads(manifests[0].read_text())
        self.assertEqual((m['object_md5'], m['esp_partuuid'], m['stage2_path'], m['padded']),
                         (md5, ESP_UUID, 'm1n1/boot.bin', False))
        self.assertEqual(stat.S_IMODE(manifests[0].stat().st_mode), 0o600)

    def test_m4_object_is_padded_and_loads_the_confined_stage2(self):
        fx = self.ready(chip='t8132', board='j713')
        r = fx.run('stage1', 'build', '--audit')
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = self.objects(fx) / 'stage1-audit.macho'
        self.assertGreater(obj.stat().st_size, 146 << 20)
        with open(obj, 'rb') as f:
            head = f.read(1 << 20)
        self.assertIn(b';m1n1/boot-ctxr.bin', head)
        self.assertIn('enroll-stage1.sh --compress --keep stage1-stock.macho', r.stdout)
        self.assertIn('<VOLUME>', r.stdout)

    def test_refusals(self):
        fx = self.ready(owner='aurora')
        r = fx.run('stage1', 'build', '--audit')
        self.assertEqual(r.returncode, 1)
        self.assertIn('KEY_OWNER=aurora', r.stderr)
        fx = self.ready(flavour='dev')
        r = fx.run('stage1', 'build', '--audit')
        self.assertIn('dev-flavour', r.stderr)
        self.assertEqual(fx.run('stage1', 'build', '--audit', '--allow-dev-flavour').returncode, 0)

    def test_m4_needs_a_padding_capable_aura(self):
        fx = self.ready(chip='t8132', board='j713')
        tool_path = fx.root / 'usr/share/aura/tools/macho_boot_object.py'
        tool_path.write_text(tool_path.read_text().replace('--execute-window', '--other'))
        r = fx.run('stage1', 'build', '--audit')
        self.assertEqual(r.returncode, 1)
        self.assertIn('aura#195', r.stderr)

    def test_m4_without_the_confined_stage2_is_refused(self):
        fx = self.ready(chip='t8132', board='j713', stage2=False)
        r = fx.run('stage1', 'build', '--audit')
        self.assertEqual(r.returncode, 1)
        self.assertIn('confined Stage 2', r.stderr)

    def test_image_checks(self):
        fx = self.ready()
        img = fx.root / 'usr/share/aura/stage1/aura-secure-raw.bin'
        img.write_bytes(raw_stage1(keys=False))
        self.assertIn('AURORA-S1-KEY01', fx.run('stage1', 'build', '--audit').stderr)
        img.write_bytes(struct.pack('<I', 0xFEEDFACF) + raw_stage1()[4:])
        self.assertIn('Mach-O', fx.run('stage1', 'build', '--audit').stderr)
        (fx.root / 'proc/device-tree/compatible').write_bytes(b'qemu,virt\0')
        self.assertIn('no Apple chip', fx.run('stage1', 'build', '--audit').stderr)

    def test_no_stage2_key(self):
        fx = self.ready()
        (fx.keys / 'stage2.pub').unlink()
        (fx.keys / 'stage2.key').unlink()
        self.assertIn('keys create', fx.run('stage1', 'build', '--audit').stderr)

    def test_enforce_needs_a_signed_stage2_and_an_audit_boot_that_verified_it(self):
        fx = self.ready()
        self.assertEqual(fx.run('stage1', 'build', '--audit').returncode, 0)
        r = fx.run('stage1', 'build', '--enforce')
        self.assertEqual(r.returncode, 1)
        self.assertIn('--sign-stage2-by-hand', r.stderr)
        (fx.root / 'usr/share/aurora').mkdir(parents=True)
        (fx.root / 'usr/share/aurora/stage2-sign-hook').touch()
        r = fx.run('stage1', 'build', '--enforce')
        self.assertIn('is unsigned', r.stderr)
        self.assertEqual(fx.run('stage2', 'sign').returncode, 0)
        r = fx.run('stage1', 'build', '--enforce')
        self.assertIn('enrol the audit Stage 1 and boot it first', r.stderr)
        self.record(fx, verdict='unsigned')
        r = fx.run('stage1', 'build', '--enforce')
        self.assertIn('recorded Stage 2 as unsigned', r.stderr)
        shutil.rmtree(fx.root / 'proc/device-tree/chosen/aurora,boot-security')
        self.record(fx, key_id=b'\x11' * 8)
        self.assertIn('1111111111111111', fx.run('stage1', 'build', '--enforce').stderr)
        shutil.rmtree(fx.root / 'proc/device-tree/chosen/aurora,boot-security')
        self.record(fx)
        (fx.esp / 'aura-try').write_bytes(b'armed\nm1n1/boot.bin.next\n')
        self.assertIn('trial is armed', fx.run('stage1', 'build', '--enforce').stderr)
        (fx.esp / 'aura-try').unlink()
        r = fx.run('stage1', 'build', '--enforce', '--volume', 'Fedora')
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = (self.objects(fx) / 'stage1-enforce.macho').read_bytes()
        self.assertEqual(key_block(obj)[2], 1)
        audit_md5 = hashlib.md5((self.objects(fx) / 'stage1-audit.macho').read_bytes()).hexdigest()
        self.assertIn(f'--keep stage1-audit.macho {audit_md5} Fedora stage1-enforce.macho', r.stdout)
        self.assertIn('REFUSES', r.stdout)

    def test_enforce_refused_while_stage2_signing_is_off(self):
        fx = self.ready(extra='STAGE2_SIGN=no\n')
        self.record(fx)
        r = fx.run('stage1', 'build', '--enforce', '--sign-stage2-by-hand')
        self.assertIn('Stage 2 signing is off', r.stderr)
        self.assertEqual(r.returncode, 1)

    def test_secver_below_the_floor_is_refused(self):
        fx = self.ready(extra='STAGE2_SECVER=0\n')
        r = fx.run('stage1', 'build', '--audit', '--min-secver', '2')
        self.assertIn('below the Stage 1 floor', r.stderr)

    def test_rollback_option_copies_next_to_the_object(self):
        fx = self.ready()
        mine = fx.root / 'old.macho'
        mine.write_bytes(b'old stage 1')
        r = fx.run('stage1', 'build', '--audit', '--rollback', '/old.macho')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f'--keep old.macho {hashlib.md5(b"old stage 1").hexdigest()} <VOLUME>', r.stdout)
        self.assertEqual((self.objects(fx) / 'old.macho').read_bytes(), b'old stage 1')
        self.assertFalse((self.objects(fx) / 'stage1-stock.macho').exists())

    def test_stage2_sign_verify_and_status(self):
        fx = self.ready()
        boot = fx.esp / 'm1n1/boot.bin'
        before = boot.read_bytes()
        self.assertEqual(fx.run('stage2', 'verify').returncode, 1)
        nxt = fx.esp / 'm1n1/boot.bin.next'
        nxt.write_bytes(stage2_image())
        r = fx.run('stage2', 'sign')
        self.assertEqual(r.returncode, 0, r.stderr)
        data = boot.read_bytes()
        self.assertTrue(data.startswith(before) and data.endswith(b'AURSIGT1'))
        v = fx.run('stage2', 'verify')
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        self.assertEqual(v.stdout.count(': verified'), 2)
        chk = subprocess.run([sys.executable, os.path.join(AURA, 'tools/secboot_sign.py'), 'verify', '--pub',
                              str(fx.keys / 'stage2.pub'), str(boot)], capture_output=True, text=True)
        self.assertEqual(chk.returncode, 0, chk.stdout)
        self.assertIn('already signed', fx.run('stage2', 'sign').stdout)
        # A measure line appended after the image is refused, and nothing is written.
        nxt.write_bytes(stage2_image(lines=b'chosen.aurora,measure-0=x\n'))
        r = fx.run('stage2', 'sign')
        self.assertEqual(r.returncode, 1)
        self.assertIn('measure-', r.stderr)
        nxt.unlink()
        self.assertEqual(boot.read_bytes(), data)
        # Another key: the old trailer is replaced, and the old key no longer verifies it.
        (fx.keys / 'stage2.key').write_bytes(bytes(32))
        (fx.keys / 'stage2.pub').write_bytes(sb.ed25519_public(bytes(32)))
        self.assertEqual(fx.run('stage2', 'sign').returncode, 0)
        self.assertEqual(boot.read_bytes().count(b'aura_sig'), 1)
        self.record(fx)
        st = json.loads(fx.run('status', '--json').stdout)
        self.assertEqual(st['stage1']['policy'], 'audit')
        self.assertEqual(st['stage1']['flavour'], 'secure')
        self.assertEqual(st['stage2']['verdict'], 'verified')
        self.assertIn('Stage 1: policy audit, secure flavour', fx.run('status').stdout)

    def test_stage2_hook(self):
        fx = self.ready()
        src, dst = fx.root / 'in.bin', fx.root / 'out.bin'
        src.write_bytes(stage2_image())
        r = fx.run('stage2', 'hook', '/in.bin', '/out.bin')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('signed', r.stdout)
        self.assertEqual(sb.read_trailer(dst.read_bytes())[0], len(src.read_bytes()))
        # Deterministic: the same input gives the same bytes (update-m1n1 compares them).
        first = dst.read_bytes()
        fx.run('stage2', 'hook', '/in.bin', '/out.bin')
        self.assertEqual(dst.read_bytes(), first)
        src.write_bytes(stage2_image(old=True))
        r = fx.run('stage2', 'hook', '/in.bin', '/out.bin')
        self.assertEqual((r.returncode, dst.read_bytes()), (0, src.read_bytes()))
        self.assertIn('older than aura 1.0.0b11', r.stdout)
        fx.write_conf('owner', 'ESP=/boot/efi\nSTAGE2_SIGN=yes\n')
        self.assertEqual(fx.run('stage2', 'hook', '/in.bin', '/out.bin').returncode, 1)
        src.write_bytes(stage2_image())
        fx.write_conf('owner', 'ESP=/boot/efi\nSTAGE2_SIGN=no\n')
        fx.run('stage2', 'hook', '/in.bin', '/out.bin')
        self.assertEqual(dst.read_bytes(), src.read_bytes())
        (fx.root / 'etc/aurora/secboot.conf').unlink()
        r = fx.run('stage2', 'hook', '/in.bin', '/out.bin')
        self.assertEqual((r.returncode, dst.read_bytes()), (0, src.read_bytes()))
        fx.write_conf('owner', 'ESP=/boot/efi\nSTAGE2_SIGN=yes\n')
        (fx.keys / 'stage2.key').unlink()
        self.assertIn('no', fx.run('stage2', 'hook', '/in.bin', '/out.bin').stderr)

    def test_execute_window_bounds_match_the_installer(self):
        for chip in (0x8103, 0x8112, 0x8122, 0x6000, 0x6020, 0x6031, 0x6034):
            self.assertFalse(sb.uses_execute_window(chip), hex(chip))
        for chip in (0x8132, 0x8140, 0x8142, 0x6040, 0x6041, 0x6050):
            self.assertTrue(sb.uses_execute_window(chip), hex(chip))


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
                                     'usr/share/doc/aurora-secboot/README.md',
                                     'usr/share/doc/aurora-secboot/stage1-existing-install.md'])
            self.assertEqual(stat.S_IMODE((Path(tmp) / 'var/lib/aurora/secboot').stat().st_mode), 0o700)
            self.assertTrue(os.access(Path(tmp) / 'usr/bin/aurora-secboot', os.X_OK))


if __name__ == '__main__':
    unittest.main()
