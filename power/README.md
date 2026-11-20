# Power saver on Apple silicon

`aurora-power-budget` gives the desktop's **Power Saver** profile a real CPU
power limit, the way macOS Low Power Mode does. Balanced and Performance are
untouched: outside Power Saver the daemon waits on D-Bus and writes nothing.

## What macOS Low Power Mode does

Apple describes Low Power Mode as reducing energy use and fan noise
([About Power Modes on your Mac](https://support.apple.com/en-us/101613)).
We measured it on a 14-inch MacBook Pro M4 Pro (macOS 27.0, AC, CPU-only
counting loads, `powermetrics` at 250 ms, Automatic vs Low Power, two rounds):

| Load | Automatic | Low Power |
| --- | --- | --- |
| 1 thread | 7.8 to 9.6 W, 1651 to 1662 M iterations | 2.9 to 3.0 W, 968 M iterations (-41%); the busy P core held at 2868 MHz (0.64 of 4512) |
| 10 threads | 61 W, P clusters 3852 MHz | 12.7 W, P clusters 1800 MHz (0.40), E 1677 to 1849 MHz |
| 14 threads | 57 to 62 W, P 3616 to 3758 MHz, E 2592 MHz | 13.0 W, P 1800 MHz, E 2592 MHz (its maximum) |

Published measurements on an M4 Pro Mac mini agree on the shape: CPU-only load
held near 14 W instead of about 32 W within 0.1 to 0.2 s, and CPU plus GPU held
under 13.5 W with the GPU below 1 GHz
([CPU](https://eclecticlight.co/2025/01/16/how-low-power-mode-controls-cpu-cores/),
[CPU+GPU](https://eclecticlight.co/2025/01/22/m4-pro-full-on-when-cpu-and-gpu-draw-over-50-w-and-how-low-power-mode-changes-that/)).

So Low Power Mode is a short-window power budget shared between the clusters
(and the GPU), spent first on the efficient E cores, plus a ceiling on a busy
P core. It is not a governor change and does not move work off the P cores.

## What the daemon does

The power profile daemon (tuned-ppd on Fedora, power-profiles-daemon on Arch
Linux ARM and NixOS) publishes the active profile on D-Bus. While it is
`power-saver`, every 100 ms `aurora-power-budget`

1. reads each CPU's busy time from `/proc/stat` and each cluster's current
   frequency,
2. estimates each cluster's power demand from the device tree energy model
   (`opp-microwatt` on the cluster's operating points), asking for what
   schedutil would (1.25 x the current frequency x the busiest CPU's load),
3. if the demand exceeds the budget, spends the budget step by step on the
   frequency increase that buys the most CPU capacity per milliwatt (the E
   cores keep their clock and the P cores give way, as on macOS); an idle
   cluster keeps what one CPU can afford from the rest, and
4. caps each cluster at the operating point it got, through
   `scaling_max_freq`; the biggest cores are also held at the Power Saver
   ceiling.

`scaling_max_freq` is a frequency QoS request, so the strictest of the profile
cap and the thermal limits always wins; nothing here can raise a clock above a
thermal limit. The governor stays `schedutil`. A cap is lowered at once and
raised only after it has held for two periods. When the profile leaves
Power Saver, or the daemon stops, the request is removed (written unbounded),
never "restored" from a read-back value, because reading `scaling_max_freq`
returns the resolved limit including thermal requests.

Under the budget only the ceiling applies. `/run/aurora-power-budget/status.json`
reports the profile, the budget, the estimated CPU power and the caps once a
second while capping.

### The budget and the ceiling

There are no per-Mac numbers; two policy fractions apply to every Mac's own
device tree energy model:

- **Budget:** the energy-model power of every CPU at 0.45 x its cluster's
  maximum frequency (interpolated between operating points), summed.
- **Ceiling:** the biggest cores (highest scheduler capacity) never go above
  the highest operating point at or below 0.64 x their maximum.

Both come from the MacBook Pro M4 Pro measurement above. Run against that
Mac's energy model (`t604x.dtsi`) the policy gives 2868 MHz for one busy P
core, E 2592 + P 1800/1800 MHz for 14 threads, and P 2088/1800 MHz for 10
threads (macOS: 2868; 2592 + 1800/1800; 1800/1800). Energy-model units are
relative: on the M2 they read about half the measured package delta at the top
of the P table and about the same in the middle, so the budget is a ratio of
the model, not a wattage. `/etc/aurora/power-budget.conf` can change both:

```
# fraction of each cluster's maximum frequency that sets the budget
fraction = 0.45
# or an absolute budget in energy-model milliwatts
# budget_mw = 2500
# fraction of the biggest cores' maximum frequency; 1 turns the ceiling off
ceiling = 0.64
```

Machines whose device tree has no energy model get no CPU budget; the daemon
says so in the journal and stays idle.

### When the kernel budgets Power Saver itself

On the M5 Pro and Max (T6050) the kernel's CLPC driver registers a platform
profile (`/sys/class/platform-profile/*/name` reads `apple-clpc`) whose
low-power profile holds the package to macOS Low Power Mode's measured power
itself. With that handler present a second budget here would cap the CPU
twice, so the daemon writes nothing, logs
`kernel apple-clpc profile handler present; not capping`, and says so in
`/run/aurora-power-budget/status.json`.

## Games

Games that ask for Feral GameMode run on the P cores with the governor left
at `schedutil`; see [GAMEMODE.md](GAMEMODE.md).

## The other legs: `aurora-power-modes`

`aurora-power-modes` runs next to the budget and gives each profile the parts
of a power mode that are not CPU clocks. It follows the same D-Bus profile and
writes only what a profile changes; every value it touches is read at start
and put back outside those profiles and when the service stops.

| Leg | Power Saver | Balanced | Performance |
| --- | --- | --- | --- |
| `rt_floor` (`kernel.sched_util_clamp_min_rt_default`) | 0: realtime threads (threaded IRQs, PipeWire) no longer run their CPU at its top clock | as found | as found |
| `aspm` (PCIe ASPM policy) | `powersupersave`: Wi-Fi, Bluetooth and SD reader links use L1.1/L1.2 | as found | as found |
| `platform` (every `/sys/class/platform-profile` handler) | `low-power` | `balanced` | `performance`: on Macs whose kernel knows High Power mode, this tells the SMC to give the fans their full range |

The `platform` leg is there because tuned-ppd drives only the ACPI
`platform_profile` file, which Apple device tree boots do not have.
power-profiles-daemon writes the class itself; the daemon then writes the same
value.

### Low Power Mode on battery

macOS lets the user run Low Power Mode never, always, only on battery or only
on the power adapter. `/etc/aurora/power-modes.conf` has the same setting:

```
# never (default, as on macOS) | always | battery | adapter
low_power_mode = battery
# turn a leg off, or change its value for one profile
# aspm = off
# rt_floor.balanced = 0
```

When the power source starts to match, the profile switches to Power Saver;
when it stops matching, the previous profile comes back, unless the user
picked a profile in between. GNOME's own "Automatic Power Saver" (Power
Saver at low battery, `power-saver-profile-on-low-battery`) is separate and
stays on.

### Measured (MacBook Pro 16-inch M4 Pro, Fedora 45, kernel 7.3.0-aurora-fleet75, AC)

Same boot, SELinux enforcing, panel off. SMC `Total System Power`, 60 s idle
rows; loads as in the budget results (perl counting threads) plus a GPU ALU
row. "Stock" is tuned-ppd plus `aurora-power-budget`; "with modes" adds
`aurora-power-modes`.

| Row | Balanced | Power Saver, stock | Power Saver, with modes |
| --- | ---: | ---: | ---: |
| idle | 5.43 to 5.60 W | 5.10 W | 4.55 W |
| 1 thread | 15.4 to 16.9 W | 9.47 W | 10.0 W |
| 14 threads | 74.9 to 79.8 W | 21.4 W | 20.9 W |
| GPU ALU | 37.9 W, 7516 GFLOPS | 37.5 W, 7516 GFLOPS | 37.1 W, 7518 GFLOPS |

The two legs take another 0.55 W off Power Saver at idle (-11 %), so Power
Saver idles 1 W (-19 %) under Balanced. Under load they change nothing. macOS
Low Power Mode on the same Mac does not lower idle power. Leaving Power
Saver put the values found at start back (`rt_floor` 1024, `aspm` default).

The GPU row shows the leg that is still missing. On this Mac, macOS Low Power
Mode lowers GPU power by a third (16.4 to 11.0 W on the same ALU load, -12.5 %
throughput) and holds CPU plus GPU near 12 W. Linux has no GPU power control
to drive yet.

## Charge to Full Now: `aurora-charge-full-now`

macOS offers "Charge to Full Now" in the battery menu while its charge limit
holds the battery below full: the battery charges to 100 % once, then the
limit applies again. On Linux the limit belongs to UPower 1.91: GNOME
Settings > Power > Preserve Battery Health calls the battery's
`EnableChargeThreshold` method, UPower writes `charge_control_*_threshold`
(on Apple silicon `macsmc-battery`, 80 % from the udev `CHARGE_LIMIT`) and
keeps the on/off state in `/var/lib/upower/charging-threshold-status`.

```
aurora-charge-full-now start           # charge to 100 % once
aurora-charge-full-now start --by 07:30  # hold the limit, be full by the next 07:30
aurora-charge-full-now cancel          # stop now and clear a schedule; the limit is back on
aurora-charge-full-now status [--json]
```

`start` asks systemd for `aurora-charge-full-now.service`. The polkit rule
`50-aurora-charge-full-now.rules` lets the active local user start and stop
that one unit, start the planner `aurora-charge-full-now-schedule@HHMM.service`
and stop the schedule timer without a password, as UPower's own
`org.freedesktop.UPower.enable-charging-limit` action does
(`allow_active=yes`); anyone else is asked for an administrator password.

The service lifts the limit only through UPower, so GNOME always shows what
the battery does (Settings shows "Maximize Charge" during the one-shot):

1. It writes `/var/lib/aurora/charge-full-now/active`, then calls
   `EnableChargeThreshold(false)`.
2. It waits on D-Bus signals, not a timer: UPower's `PropertiesChanged` on the
   battery and on UPower itself (`OnBattery`), UPower restarts, and logind's
   `PrepareForSleep`, so a resume from s2idle is seen at once.
3. It ends when the battery is full (`State` fully-charged or 100 %), when the
   adapter goes away (macOS keeps the request until full; here a one-shot
   never outlives the charge session it was asked for), on `cancel`, or after
   12 hours counted on `CLOCK_BOOTTIME`, so time in suspend counts.
   `RuntimeMaxSec=13h` is a backstop.
4. It calls `EnableChargeThreshold(true)` and removes the marker. If the user
   turns Preserve Battery Health back on during the one-shot, the one-shot
   ends and UPower's limit is left as the user set it.

While the marker exists, every way out restores the limit: `ExecStopPost`
runs `aurora-charge-full-now restore` when the process ends however it ends
(also on a crash or kill), and `aurora-charge-full-now-restore.service`
(enable it) runs it at boot after a power loss or a hard reset, when UPower
has come up with the limit off. `restore` waits up to 60 s for UPower to
list the battery.

Nothing happens, with a message and exit status 0, when there is no battery
(desktop Macs), when UPower cannot limit the battery, when the limit is off,
when the battery is already full, or on battery power. Nothing in it is
specific to a Mac: it works on any laptop whose battery UPower reports with
`ChargeThresholdSupported`.

GNOME 51.1 has no equivalent: Settings offers only the persistent choice
between Maximize Charge and Preserve Battery Health, and the Shell reads
`ChargeThresholdEnabled` only to draw the plugged-in battery icon.
[`gnome/`](../gnome/README.md) has an optional quick settings toggle that calls
`aurora-charge-full-now start` and `cancel`; it is not installed by default.

### Full by a time: `start --by HH:MM`

Like macOS Optimised Battery Charging, the limit holds overnight and the
battery reaches 100 % just before the usual unplug time. `start --by 07:30`
starts the planner, which picks the next 07:30 in local time and starts
the one-shot at

    07:30 - (EnergyFull - Energy) / rate - 30 min

The charge rate is conservative: the lower of 0.6 x the `input_power_limit`
of the online adapter in `/sys/class/power_supply` and UPower's `EnergyRate`
while charging, never below 10 W (also the value used with no adapter
connected). If that start time has already passed, the one-shot starts at
once. Otherwise the planner arms a transient timer,
`aurora-charge-full-now-schedule.timer` (`systemd-run --on-calendar`,
`AccuracySec=1min`), that starts `aurora-charge-full-now.service`. The plan is
in the planner's journal and in `status`.

- The planner never touches the limit. Only the one-shot lifts it, with the
  restore paths above, and it repeats its own checks when the timer fires:
  with the adapter unplugged, the limit off or the battery full at that
  moment, nothing happens.
- The timer does not wake the Mac. A start time that passes during suspend
  fires at resume (systemd catches up calendar timers that elapsed while the
  clock was suspended).
- A schedule does not survive a reboot or power-off: the transient timer is
  gone and the limit simply keeps holding (the battery stays at the limit,
  which is the safe side). Set it again after a restart.
- `start` without `--by` and `cancel` clear a pending schedule.

## Not covered yet

- **GPU and ANE.** macOS also lowers the GPU's power target in Low Power Mode.
  Linux has no qualified GPU power-limit interface yet (the absolute power
  request in the GPU driver is still being qualified), and none for the ANE.
- **Display refresh and brightness.** macOS Low Power Mode and High Power
  Mode leave the panel's refresh range and brightness unchanged on the
  MacBook Pro M4 Pro, so the profiles do not change them either.
- **Fans.** The SMC keeps its own fan policy. Where the kernel sends macOS's
  High Power mode record to the SMC (the performance profile), the SMC lets
  the fans use their full range; the `platform` leg makes that reachable on
  Fedora.
- **Measured feedback.** The model is feed-forward. A one-second correction
  from the SMC package power sensor, and a kernel coordinator (DTPM or a
  platform-profile provider) that owns the CPU and GPU legs together, are the
  next steps; see [DESIGN.md](DESIGN.md).

## Results (MacBook Air M2, Fedora 44, kernel 7.3.0-aurora-fleet25, AC)

One boot, SELinux enforcing, panel on. Each pass: 40 s settled idle, then 1, 4
and 8 unpinned counting threads for 12 s. Power is the SMC `Total System
Power` mean; "M iters" counts busy-loop iterations (throughput). Means of the
passes; balanced and Power Saver passes alternated.

| Profile | Idle | 1 thread | 4 threads | 8 threads | 8 threads, M iters per W above idle |
| --- | ---: | ---: | ---: | ---: | ---: |
| Balanced (6 passes) | 3350 mW | 6342 mW, 492 M | 12776 mW, 1912 M | 16480 mW, 2854 M | 217 |
| Power Saver, stock tuned-ppd (2) | 3387 mW | 6707 mW, 508 M | 12732 mW, 1915 M | 16494 mW, 2818 M | 215 |
| Balanced, daemon running (2) | 3318 mW | 6594 mW, 506 M | 12834 mW, 1917 M | 16541 mW, 2890 M | 219 |
| Power Saver with this daemon (2) | 3019 mW | 5277 mW, 432 M | 7042 mW, 1233 M | 6378 mW, 1428 M | 425 |

Stock Power Saver changes nothing. With the daemon, idle drops 330 mW (10%),
one thread costs 17% less power for 12% less work, and eight threads 61% less
power for 50% less work, so twice the work per watt above idle. The caps under
eight threads were E 2256 MHz and P 660 MHz. That is the same order of cut as
macOS on the M4 Pro (single thread -41%, all-core power 13 W instead of 60 W).
Running in Balanced, the daemon costs nothing measurable. Raw data, the energy
model sweep and the two earlier allocators (a demand-proportional split
starved the E cores; a 0.6 budget without a ceiling saved only under eight
threads) are kept with the project's traces.
