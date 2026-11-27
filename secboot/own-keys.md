# Using your own boot keys

When you set up Aurora, the Recovery step has a **Startup Security Keys…** button with three choices:

- **Aurora-managed** (the default): Aurora signs what it ships. Your Mac keeps no signing key.
- **My own key**: setup makes a signing key for this Mac. You sign your own startup software and kernels.
- **Both**: the Mac accepts what Aurora signs and what you sign.

Aurora doesn't have release signing keys yet. Until it does, Aurora-managed checks nothing, and Both
works like My own key.

If a choice is greyed out, this release's files don't include the startup software that can hold your
own key. Use Aurora-managed, or a newer release.

## After setup

1. Start Linux once. At first boot your key moves from the startup partition into the encrypted system.
   Check it with `sudo aurora-secboot status`: it shows `key owner: owner` (or `both`) and your
   Stage 2 key id.
2. Make your kernel keys, sign the kernel, use it, and sign the startup software:

   ```
   sudo aurora-secboot keys create
   sudo aurora-secboot uki build "$(uname -r)"
   sudo aurora-secboot enable
   sudo aurora-secboot stage2 sign
   ```

   New kernels and startup software updates get signed automatically from now on.
3. Restart and run `sudo aurora-secboot status` again. Stage 1 should say Stage 2 is `verified`, and the
   UKI should say `verified`.

So far everything is checked and recorded, but nothing is refused.

## Refusing anything you didn't sign

Once `status` looks right, build an enforcing Stage 1 and enrol it from recoveryOS:

```
sudo aurora-secboot stage1 build --enforce
```

It refuses if something would stop your Mac from starting, for example if your kernel isn't signed or
the startup software has no kernel keys. It then writes the recoveryOS commands to
`aurora/stage1/ENROLL-enforce.txt` on the startup partition. From then on, startup software or a kernel
that isn't signed with your keys doesn't start.

## If something goes wrong

- **Before enforcing:** `sudo aurora-secboot disable` brings back the normal boot loader. Nothing is
  refused anyway.
- **After enforcing, the Mac doesn't start Aurora:** hold the power button and pick macOS in the startup
  picker. Then, in recoveryOS, enrol the previous Stage 1 (`stage1-audit.macho`) with the command in
  `ENROLL-enforce.txt`. Start Aurora, fix and re-sign, then enrol the enforcing one again.
- **Back up `/var/lib/aurora/secboot/keys`** somewhere safe and offline. Without these keys you can't
  sign updates, and you have to go back to the previous Stage 1 from recoveryOS.

More detail: [stage1-existing-install.md](stage1-existing-install.md) and [README.md](README.md).
