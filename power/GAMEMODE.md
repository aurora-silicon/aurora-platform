# Game mode on Apple silicon

`power/gamemode.ini` is installed as `/etc/gamemode.ini`. It sets two things
for [Feral GameMode](https://github.com/FeralInteractive/gamemode), the
per-game switch that Steam (`gamemoderun %command%`), Lutris, Heroic and
others already turn on:

- **Pin the game to the big cores** (`pin_cores=yes`). GameMode finds them
  from `cpuinfo_max_freq` (every CPU within 10 % of the fastest) and moves
  every thread of the game there, again every 5 s for new threads.
- **Keep `schedutil`** (`desiredgov=schedutil`). GameMode's own default asks
  for the `performance` governor. On Apple silicon that holds every cluster
  at its top clock and turns energy-aware scheduling off, for no gain.

Nothing changes until a game asks for GameMode, and the old placement comes
back when it exits. A user's `~/.config/gamemode.ini` still overrides both
keys.

## Why the big cores

When a game keeps only one or two frames in flight, every frame waits for
the previous one: the GPU finishes frame N-1, the kernel signals the fence,
the game's render thread wakes, records and submits frame N+1, and the
kernel hands it to the firmware. The GPU idles until that chain completes.

Energy-aware scheduling sees a thread that sleeps most of the time and puts
it on an efficiency core. On the MacBook Pro M4 Pro (T6040) a submit thread
on the E cores (2592 MHz, capacity 383) takes about twice as long for every
step of that chain as on a P core (4512 MHz): the submit ioctl goes from 29
to 65 us. At two frames in flight that is the biggest single piece of the
gap to macOS.

## Which Macs pin

| Mac | big cores | GameMode pins |
| --- | --- | --- |
| M1, M2 | 4 P (of 8) | the 4 P cores |
| M1 Pro / Max | 8 P (of 10) | the 8 P cores |
| M4 Pro | 10 P (of 14) | the 10 P cores |
| A18 Pro (MacBook Neo) | 2 P (of 6) | nothing: GameMode needs at least four |

`tests/test_gamemode_config.py` replays GameMode's selection rule on these
tables.

## Results (MacBook Pro M4 Pro, Fedora 45, kernel 7.3.0-aurora-fleet75, AC)

gpubench 0dae261 `render.heavy` at 1080p (geom: 262,144 triangles; fill: 16
full-screen alpha layers) and `compute.heavy` ALU. Microseconds per
submission, lower is better, median of three fresh processes per arm, arms
interleaved. "Stock GameMode" is GameMode 1.8.2's own configuration with the
user in the `gamemode` group, so that its `performance` governor request
succeeds.

| Row | Default | GameMode, this config | Stock GameMode | macOS 27 |
| --- | ---: | ---: | ---: | ---: |
| geom, 2 in flight | 197.3 | 179.0 (-9.3 %) | 154.8 (-21.5 %) | 139.0 |
| fill, 2 in flight | 162.3 | 144.5 (-11.0 %) | 140.7 (-13.3 %) | 126.3 |
| geom / fill, 1 in flight | 344.4 / 278.4 | -0.4 / -0.6 % | -4.8 / -5.9 % | 340.6 / 269.1 |
| geom / fill, 4 to 64 in flight | 137.4-137.8 / 127.4-127.8 | within 0.3 % | geom within 0.2 %, fill +2.0 to +2.2 % | 136.4 / 126.5 |
| ALU, 16 and 64 in flight | 283.4 / 283.1 | +1.0 / +1.1 % | +1.0 / +1.1 % | |

At two frames in flight the submit thread ran on an E core in 99.6 % of
samples by default and never with this config. Saturated ALU loses about 1 %
whenever the submitter sits on a P core (a utilization clamp shows the same).

Sustained 20 s rows, SMC `Total System Power` (idle 4.84 W):

| Row | Default | This config | Stock GameMode |
| --- | --- | --- | --- |
| geom, 2 in flight | 193.5 us, 22.4 W, 3.39 mJ per frame above idle | 170.1 us, 22.4 W, 2.99 mJ (-11.9 %) | |
| fill, 2 in flight | 158.4 us, 44.2 W, 6.23 mJ | 142.1 us, 48.8 W, 6.25 mJ (+0.3 %) | 140.6 us, 53.1 W, 6.78 mJ (+8.8 %) |
| ALU, 2 in flight | 9.28 mJ | 9.41 mJ (+1.4 %) | 10.15 mJ (+9.4 %) |

So the lift comes at the same energy per frame; the `performance` governor
buys a little more speed at shallow depths for 9 to 20 % more energy per
frame, and slows saturated fill. With gamemoded loaded and no game running,
idle power is unchanged.

On the MacBook Neo (A18 Pro) GameMode leaves placement and the governor
alone with this config; every row stays within 1 % of the default.

deqp-vk 1.4.7.0 (7,394-case subset) gives identical results case for case
with and without GameMode.

## What is left

With the submitter on a P core, two frames in flight still take 179 us
against macOS's 139. The rest is in the kernel and firmware: the GPU
completion work runs on E cores (likely where the stock governor's extra
lift comes from, since it clocks those up too), and the submit and completion paths plus the firmware's
doorbell-to-start latency cost about 150 us per round trip.
