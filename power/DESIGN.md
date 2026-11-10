# CPU performance per watt: the closed loop, design

Status: the Power Saver budget (`aurora-power-budget`) is the first step.
This note says what the rest of a macOS-style closed-loop CPU power policy
would take on Linux, in order, and how each step is measured.

## Where Linux is today

| Layer | macOS | Aurora Linux now |
| --- | --- | --- |
| Frequency choice | Performance controller picks each cluster's frequency from load and thread QoS, with feedback | `schedutil` on every Mac |
| Placement | QoS classes steer work between E and P cores | Energy-aware scheduling: the scheduler uses the device tree energy model and the per-core capacities (M2: E 571, P 1024; `sched_energy_aware=1`) |
| Thermal | Firmware and kernel limits on die temperature | PMP fast-die limit and thermal zones per board (step-wise / power allocator) |
| Fabric and memory | Demand-driven fabric and memory floors | Apple CLPC driver sets fabric and memory floors from demand |
| Low Power Mode | Short-window package budget split across CPU, GPU, ANE | `aurora-power-budget`: CPU budget split across clusters every 100 ms (this package) |
| High Power Mode | More fan headroom, same silicon ceilings | Nothing; the SMC keeps its own fan policy |
| Performance profile | Same clocks as Automatic | `aurora-performance` tuned profile keeps `schedutil` (no pinned clocks) |

On the M2 the hardware already holds the all-core P clock near 2.2 GHz
whatever cpufreq asks for: with four P threads, caps from 2208 to 3204 MHz
give identical throughput and package power (+6.5 W over idle). So some of the
loop is closed below Linux already, at least on fanless Macs.

## Steps

1. **CPU budget for Power Saver** (done, this package). Feed-forward from the
   device tree energy model, 100 ms window, budget = energy-model power of
   every CPU at 0.45 x its cluster maximum, spent on the best capacity per
   milliwatt, plus a 0.64 x maximum ceiling on the biggest cores. Both
   fractions reproduce the MacBook Pro M4 Pro's macOS Low Power Mode on its
   own energy model. Measured on the M2: see the README.

2. **Measured correction.** Scale the model by a slow (1 s) integral term
   from the SMC package power sensor (`Heatpipe Power` on the M2 and M4 Pro
   boards) so a budget can be stated in real watts. This only becomes useful
   once the policy is stated in watts. One M4 Pro capture exists (about 13 W
   CPU-only in Low Power vs about 60 W in Automatic); captures on an M1/M2
   class Mac and a CPU+GPU load would tell whether a wattage or a ratio
   transfers better between Macs. Until then the budget stays a ratio of the
   model and the correction would change nothing.

3. **GPU leg.** The GPU driver's absolute power request ([linux pull 1065](https://github.com/aurora-silicon/linux/pull/1065), draft)
   gives the budget a second actuator. The daemon would then split the budget
   CPU vs GPU by demand, as macOS does (CPU+GPU under 13.5 W on the M4 Pro).
   Gate: the request's achieved power must be qualified on hardware first.

4. **Kernel owner.** Move the split into the kernel so it runs without a
   daemon and composes with thermal limits in one place:
   - DTPM is already built into the fleet kernel (`CONFIG_DTPM=y`,
     `CONFIG_DTPM_CPU=y`). An Apple DTPM hierarchy (package -> CPU clusters,
     GPU) needs a small SoC table driver, as Rockchip has; DTPM splits a
     parent limit by each child's maximum power, so the dynamic demand split
     still needs a governor on top, or IPA-style allocation inside the driver.
   - `platform_profile` is ACPI-only in its registration today; Apple DT boots
     have no `/sys/firmware/acpi`. A DT-capable class device would let tuned
     and power-profiles-daemon drive the kernel owner directly, with no Apple
     code in either daemon.

5. **QoS-driven placement.** macOS runs background QoS on E cores. Linux has
   the mechanism (`uclamp.max` on cgroups, e.g. a slice for background
   services) but no policy. Measure first: E-only background slice vs
   default for idle wakeups and power on the M2 and J616s.

6. **Performance per watt vs macOS.** On the J616s (M4 Pro), same workload
   on macOS Automatic and Linux Balanced: throughput, package power, energy
   per run. Use `powermetrics` on macOS and the SMC sensors on Linux, idle
   subtracted on both. This is the number that says whether steps 2 to 5 are
   worth their complexity.

## Not to do

- No pinned clocks or `performance` governor for any profile.
- No fixed frequency table posing as Low Power Mode parity: the macOS
  behaviour is a budget, so a load on one cluster can still run fast.
- No CPU offlining or affinity changes for power saving.
- No writes to undocumented power-gating registers from userspace.
