#!/usr/bin/env python3
"""Exercise the first-boot console takeover on a private PTY under systemd.

The unit never takes tty1 itself (systemd's TTYReset/TTYVHangup would cut the
boot splash off its keyboard). The text wizard opens the terminal by itself
once the splash is gone. This checks that a service started with
StandardInput=null can do that while another session still owns the terminal
as its controlling terminal, and that the prompt reads what is typed.
"""
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[1]


def wait_for(path, timeout):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if path.exists() and path.read_text():
            return True
        time.sleep(0.05)
    return False


def read_until(fd, text, timeout):
    """Read the terminal's output until text appears; False on timeout."""
    seen = b''
    until = time.monotonic() + timeout
    while text not in seen:
        left = until - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return False
        seen += os.read(fd, 4096)
    return True


def main():
    assert os.geteuid() == 0, 'test requires a disposable root-capable systemd worker'
    service = (ROOT / 'systemd/aurora-firstboot.service').read_text()
    assert 'StandardInput=null' in service and 'TTYPath' not in service
    assert 'plymouth-start.service' in service
    script = (ROOT / 'libexec/aurora-firstboot').read_text()
    takeover = 'exec 0<>"$console" 1>&0 2>&0'
    assert takeover in script
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
    unit = f'aurora-console-fixture-{os.getpid()}.service'
    try:
        assert os.read(ready_r, 1) == b'1'
        with tempfile.TemporaryDirectory(prefix='aurora-console-test-') as temporary:
            marker = Path(temporary) / 'answer'
            prompt = (f'console={tty}; {takeover}; stty sane; '
                      f'read -r -p "Linux username: " answer; printf "%s" "$answer" > {marker}')
            subprocess.run(['systemd-run', '--no-block', '--unit=' + unit,
                            '--property=Type=oneshot', '--property=StandardInput=null',
                            '--property=StandardOutput=journal', '--property=TimeoutStartSec=8s',
                            '/bin/bash', '-c', prompt], check=True)
            # Type the answer once the prompt is on the terminal: the service
            # has taken it over and is reading.
            if not read_until(master, b'Linux username: ', 8):
                subprocess.run(['journalctl', '-u', unit, '--no-pager'])
                raise AssertionError('the prompt did not appear on the terminal')
            os.write(master, b'owner\n')
            if not wait_for(marker, 5):
                subprocess.run(['systemctl', 'status', '--no-pager', unit])
                subprocess.run(['journalctl', '-u', unit, '--no-pager'])
                raise AssertionError('the prompt did not read the typed answer')
            assert marker.read_text() == 'owner', marker.read_text()
        print('A StandardInput=null service took a terminal owned by another session and read the answer.')
    finally:
        subprocess.run(['systemctl', 'stop', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.kill(holder, signal.SIGKILL)
        os.waitpid(holder, 0)
        os.close(ready_r)
        os.close(master)
        os.close(slave)


if __name__ == '__main__':
    main()
