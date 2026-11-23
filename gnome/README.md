# GNOME Shell: Charge to Full Now toggle

`charge-full-now@aurorasilicon.org` adds a "Charge to Full" toggle to GNOME
Shell 50 and 51 quick settings, for `aurora-charge-full-now`
([power/README.md](../power/README.md)). GNOME has nothing equivalent: Settings
offers only the persistent choice between Maximize Charge and Preserve Battery
Health.

The toggle shows while a battery that UPower can limit has the limit on, is
below full and the adapter is connected, and while a one-shot runs. It is
checked while `/var/lib/aurora/charge-full-now/active` exists. Clicking it runs
`aurora-charge-full-now start` or `cancel`; a failure shows as a notification.
It follows UPower's `PropertiesChanged` and a file monitor on the marker, with
no timers and no blocking D-Bus calls.

`make install-gnome-charge-full-now` installs it system wide. It is not part of
`make install` and stays off until a user enables it:

```
gnome-extensions enable charge-full-now@aurorasilicon.org
```

Tested on a headless GNOME Shell 51 session (toggle state follows the service); not yet on a desktop session with a person at it.
