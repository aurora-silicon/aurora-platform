# HDMI-CEC on the built-in HDMI port

macOS (since 14.4) uses HDMI-CEC on the built-in HDMI port of Macs that have one: when the Mac starts or wakes it turns the TV on and switches the TV to the Mac's input, and when the Mac sleeps it puts the TV in standby. `aurora-cec` does the same on Linux. It is on by default and needs no setup.

It needs the kernel's CEC adapter for the display coprocessor (`CONFIG_DRM_APPLE_CEC`, aurora-silicon/linux#2230), which appears as `/dev/cecN`, and `cec-ctl` from v4l-utils.

## What it does

- `70-aurora-cec.rules` starts `aurora-cec@cecN.service` when an adapter of the `apple-dcp` driver appears. Macs without an HDMI port, or kernels without the adapter, never run it. Other CEC adapters (USB dongles) are left alone.
- `aurora-cec wake cecN` (that service, at boot and again after every resume) claims a Playback logical address with the Mac's model as its name (`Mac mini`, `MacBook Pro`), waits up to 15 s for a TV, then sends Image View On to the TV and broadcasts Active Source with the Mac's physical address. The kernel keeps the logical address and claims it again when the TV is unplugged and plugged back in.
- `aurora-cec-sleep.service` (enabled by a preset, part of `sleep.target`) runs `aurora-cec suspend` before the Mac sleeps: it asks which device is the active source and sends Standby to the TV, unless another device (a game console, say) answers as the active source. It takes at most about 1 s and never blocks sleep. After resume it restarts the wake service.

No adapter, no TV, or a TV that does not answer CEC: nothing is sent, nothing is logged, and the exit status is 0. Each command that was sent is logged in one line (`journalctl -u 'aurora-cec*'`).

## Turning it off

`systemctl mask aurora-cec@cec0.service aurora-cec-sleep.service`. On NixOS, `aurora.cec.enable = false;`.

## Tools

`cec-ctl`, `cec-follower` and `cec-compliance` come from v4l-utils (on Fedora `cec-compliance` is in `v4l-utils-devel-tools`). The rule also tags `/dev/cec*` for `uaccess`, so the person at the Mac can run them without root; systemd itself only gives the device to group `video`. Examples:

```
cec-ctl -d0 -S                 # adapter state and the devices on the bus
cec-ctl -d0 --to 0 --standby   # TV off
cec-ctl -d0 -M                 # watch CEC traffic
```

## Not done yet

The Mac does not yet answer the TV's requests beyond what the kernel answers itself (physical address, name, vendor, CEC version): Request Active Source, Give Device Power Status, Set Stream Path and remote control keys are not handled. Turning the TV on when the display wakes from idle blanking is up to the desktop and is not done here.

Run the checks with `python3 -m unittest tests.test_cec`. They use a stand-in `cec-ctl` and a fake sysfs; they do not need an adapter.
