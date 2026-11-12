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

## Not covered yet

- **GPU and ANE.** macOS also lowers the GPU's power target in Low Power Mode.
  Linux has no qualified GPU power-limit interface yet (the absolute power
  request in the GPU driver is still being qualified), and none for the ANE.
- **Display refresh and brightness.** Whether macOS drops to 60 Hz in Low
  Power Mode is not established; brightness is left to the user.
- **Fans.** The SMC keeps its own fan policy in every profile.
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
