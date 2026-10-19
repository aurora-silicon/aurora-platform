#!/usr/bin/env python3
"""Exercise actual systemd controlling-terminal handoff on a private PTY."""
import fcntl
import os
from pathlib import Path
import pty
import signal
import subprocess
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def wait_for(path, timeout):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def main():
    assert os.geteuid() == 0, 'test requires a disposable root-capable systemd worker'
    source = (ROOT / 'systemd/aurora-firstboot.service').read_text()
    mode = next(line.split('=', 1)[1] for line in source.splitlines()
                if line.startswith('StandardInput='))
    assert mode == 'tty-force'
    assert 'plymouth-start.service' in source
    master, slave = pty.openpty()
    tty = os.ttyname(slave)
    ready_r, ready_w = os.pipe()
    holder = os.fork()
    if holder == 0:
        try:
            os.close(ready_r)
            os.close(master)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            os.write(ready_w, b'1')
            while True:
                signal.pause()
        finally:
            os._exit(0)
    os.close(ready_w)
    units = []
    try:
        assert os.read(ready_r, 1) == b'1'
        with tempfile.TemporaryDirectory(prefix='aurora-console-test-') as temporary:
            for kind, expected in [('tty', False), (mode, True)]:
                unit = f'aurora-console-fixture-{os.getpid()}-{kind}.service'
                units.append(unit)
                marker = Path(temporary) / kind
                run('systemd-run', '--no-block', '--unit=' + unit,
                    '--property=Type=oneshot', '--property=RemainAfterExit=yes',
                    '--property=StandardInput=' + kind,
                    '--property=TTYPath=' + tty, '--property=StandardOutput=journal',
                    '--property=StandardError=journal', '--property=TimeoutStartSec=8s',
                    '--property=TimeoutStopSec=2s', '/usr/bin/touch', str(marker))
                started = wait_for(marker, 2)
                if started != expected:
                    subprocess.run(['systemctl', 'status', '--no-pager', unit])
                    subprocess.run(['journalctl', '-u', unit, '--no-pager'])
                    raise AssertionError(f'{kind}: prompt execution={started}, expected={expected}')
                run('systemctl', 'stop', unit)
                print(f'{kind}: existing TTY owner; prompt executed={started}', flush=True)
        print('Observed tty waiting; actual first-boot tty-force acquired its private console.')
    finally:
        for unit in units:
            subprocess.run(['systemctl', 'stop', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.kill(holder, signal.SIGKILL)
        os.waitpid(holder, 0)
        os.close(ready_r)
        os.close(master)
        os.close(slave)


if __name__ == '__main__':
    main()
