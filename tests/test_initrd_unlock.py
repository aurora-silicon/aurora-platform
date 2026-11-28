"""Exercise the real unlock loop with synthetic prompt, disk and keyring tools."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'libexec/aurora-initrd-root').read_text()
UNLOCK = SCRIPT[SCRIPT.index('aurora_verify_opened_root() {'):
                SCRIPT.index('if [[ ${BASH_SOURCE[0]} == "$0" ]]')]
PASSWORD = " fixture $() \\ ' \" %\tpassphrase"

# Cache append/deduplication matches systemd's add_to_keyring(). In particular,
# an already cached answer is not moved to the end for pam_gdm to consume.
TOOL = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

path = Path(os.environ['FIXTURE_STATE'])
state = json.loads(path.read_text())
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
event = {'tool': tool, 'args': args, 'cache': state['cache']}
output, status = '', 0
if tool == 'systemd-ask-password':
    answer = state['answers'].pop(0)
    if answer is None:
        status = 1
    else:
        if '--keyname=cryptsetup' in args:
            cache = list(state['cache'] or [])
            if answer not in cache:
                cache.append(answer)
            state['cache'] = cache
        output = answer + '\n'
elif tool == 'cryptsetup':
    assert args == ['open', '--key-file=-', '/fixture/root', 'root']
    status = int(sys.stdin.buffer.read() != state['password'].encode())
    state['opened'] = status == 0
    event['accepted'] = status == 0
elif tool == 'blkid':
    assert args == ['-c', '/dev/null', '-s', 'UUID', '-o', 'value', '/dev/mapper/root']
    assert state['opened']
    output = state['uuid'] + '\n'
elif tool == 'udevadm':
    assert args == ['settle']
elif tool == 'keyctl':
    if args == ['padd', 'user', 'cryptsetup', '@u']:
        payload = sys.stdin.buffer.read()
        event['payload'] = payload.decode()
        if state['cache_error'] == 'add':
            status = 1
        else:
            state['cache'] = payload.decode().split('\0')
            output = '123\n'
    elif args == ['timeout', '123', '150']:
        if state['cache_error'] == 'timeout':
            status = 1
        else:
            state['timeout'] = 150
    elif args == ['revoke', '123']:
        state['cache'] = None
    else:
        raise AssertionError(args)
else:
    raise AssertionError(tool)
state['events'].append(event)
path.write_text(json.dumps(state))
sys.stdout.write(output)
sys.exit(status)
'''


class InitrdUnlockTests(unittest.TestCase):
    def unlock(self, answers, cache=None, cache_error='', temporary_key=False,
               phase='configured', uuid='fixture-uuid'):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            state_file = temporary / 'state.json'
            state_file.write_text(json.dumps(dict(
                answers=answers, password=PASSWORD, cache=cache, events=[],
                cache_error=cache_error, opened=temporary_key, timeout=None, uuid=uuid)))
            for name in ('systemd-ask-password', 'cryptsetup', 'keyctl', 'blkid', 'udevadm'):
                tool = temporary / name
                tool.write_text(TOOL)
                tool.chmod(0o755)
            if temporary_key:
                (temporary / 'omarchy').mkdir()
                (temporary / 'omarchy/luks-key').write_bytes(b'k' * 64)
            result = subprocess.run(['bash', '-c', f'''
                set -euo pipefail
                {UNLOCK}
                STATE_PHASE={phase}
                BOOT_MNT="$FIXTURE_DIR"
                KEY_BYTES=64
                ROOT_DEV=/fixture/root
                AURORA_ROOT_UUID=fixture-uuid
                omarchy_mac_encrypt_fail() {{ echo "$*" >&2; exit 1; }}
                omarchy_mac_encrypt_point_sysroot() {{ echo sysroot; }}
                omarchy_mac_encrypt_open() {{ :; }}
                omarchy_mac_encrypt_require_key() {{ :; }}
                omarchy_mac_encrypt_finish() {{ :; }}
                omarchy_mac_encrypt_open_continue
                [[ ! -v answer ]]
                '''], env=dict(os.environ, PATH=f'{temporary}:{os.environ["PATH"]}',
                               FIXTURE_STATE=str(state_file), FIXTURE_DIR=str(temporary)),
                text=True, capture_output=True, timeout=10)
            state = json.loads(state_file.read_text())
        for secret in (PASSWORD, 'rejected answer'):
            self.assertNotIn(secret, result.stdout + result.stderr)
            for event in state['events']:
                self.assertNotIn(secret, ' '.join(event['args']))
        return result, state

    def assert_published(self, answers, cache=None):
        result, state = self.unlock(answers, cache)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'sysroot\n')
        self.assertEqual(state['cache'], [PASSWORD])
        self.assertEqual(state['timeout'], 150)
        events = state['events']
        publications = [e for e in events if e['tool'] == 'keyctl' and e['args'][0] == 'padd']
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]['payload'], PASSWORD)
        self.assertEqual(publications[0]['cache'], cache)
        for event in events:
            if event['tool'] in ('systemd-ask-password', 'cryptsetup'):
                self.assertEqual(event['cache'], cache)
        # Publish only after the successful cryptsetup and filesystem check.
        self.assertEqual([e['tool'] for e in events][-5:],
                         ['cryptsetup', 'udevadm', 'blkid', 'keyctl', 'keyctl'])
        self.assertTrue(events[-5]['accepted'])
        self.assertEqual(sum(e['tool'] == 'systemd-ask-password' for e in events), len(answers))

    def test_first_answer_is_published_after_verification(self):
        self.assert_published([PASSWORD])

    def test_retries_do_not_publish_rejected_answers(self):
        self.assert_published(['rejected answer', 'another rejected answer', PASSWORD])

    def test_prepopulated_valid_answer_followed_by_rejection_is_replaced(self):
        self.assert_published(['rejected answer', PASSWORD], [PASSWORD])
        self.assert_published(['rejected answer', PASSWORD], [PASSWORD, 'stale last answer'])

    def test_exhausted_or_cancelled_prompts_preserve_the_cache(self):
        for answers in (['rejected answer'] * 3, ['rejected answer', None]):
            with self.subTest(answers=answers):
                cache = [PASSWORD, 'older disk password']
                result, state = self.unlock(answers, cache)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(state['cache'], cache)
                self.assertFalse(any(e['tool'] == 'keyctl' for e in state['events']))

    def test_wrong_root_uuid_does_not_publish(self):
        result, state = self.unlock([PASSWORD], uuid='wrong-root')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(state['cache'])

    def test_cache_failures_do_not_block_boot_and_expiry_failure_revokes(self):
        for failure in ('add', 'timeout'):
            with self.subTest(failure=failure):
                result, state = self.unlock([PASSWORD], cache_error=failure)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'sysroot\n')
                self.assertIsNone(state['cache'])
                if failure == 'timeout':
                    self.assertEqual(state['events'][-1]['args'], ['revoke', '123'])

    def test_provisioning_keys_are_not_published(self):
        for phase in ('configured', 'encrypted'):
            with self.subTest(phase=phase):
                result, state = self.unlock([], temporary_key=True, phase=phase)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([e['tool'] for e in state['events']], ['blkid'])
                self.assertIsNone(state['cache'])


if __name__ == '__main__':
    unittest.main()
