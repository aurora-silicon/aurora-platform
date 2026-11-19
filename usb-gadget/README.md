# USB device mode

Plug a USB-C cable from another computer (the host) into a Mac port that the
kernel puts in device mode, and the Mac shows up on that host as:

- a **network adapter** (CDC-NCM). macOS, Linux and Windows 11 have drivers
  built in. Both sides get IPv6 and IPv4 link-local addresses, so `ssh` works
  with no setup;
- a **serial port** (CDC-ACM), `/dev/ttyGS0` on the Mac. It has no login
  prompt unless you turn one on;
- optionally a **USB disk** backed by an image file, read-only by default.

Nothing needs to be started by hand. The USB-C port controller picks the data
role. When a host is attached as the data host, the kernel registers a USB
device controller under `/sys/class/udc`, and `86-aurora-usb-gadget.rules`
starts `aurora-usb-gadget@<controller>.service`. That service builds the
gadget in configfs and binds it. At unplug the controller goes away, the
service stops (`BindsTo=`) and the gadget is unbound.

## Which Macs and ports

The kernel decides this, not this package: `dwc3-apple` registers a device
controller only on ports whose PHY supports device mode. The M5 Pro and M5 Max
(T6050) and the fixed hub ports on the MacBook Neo are host-only. On those,
and on any kernel without USB gadget support, `aurora-usb-gadget` does nothing
and exits 0.

Check a port by plugging in a host and running:

    aurora-usb-gadget status

## Using it from the host

`aurora-usb-gadget status` on the Mac prints the Mac's interface name and
addresses, plus the MAC address the host's side of the link will have. Both
stay the same across reboots.

To log in over the network, the Mac must run an SSH server. On Fedora:
`sudo systemctl enable --now sshd`. If Avahi runs on the Mac, as on Fedora
desktops, `ssh user@<hostname>.local` works from all three host systems.
Otherwise use the link-local address:

**macOS.** System Settings > Network lists a new interface, "Aurora USB
Gadget", with a self-assigned 169.254.x.x address. Find its BSD name (`en7`,
for example) with `ifconfig` by matching the host-side MAC address from
`status`, then:

    ssh user@fe80::xxxx:xxxx:xxxx:xxxx%en7
    ssh user@169.254.x.y

Serial port: `screen /dev/cu.usbmodem*` (the name ends in the gadget's serial
number).

**Linux.** The `cdc_ncm` driver creates an `enx…` or `enu…` interface. The
host's NetworkManager tries DHCP and falls back to IPv6 link-local only. Use
`ssh user@fe80::…%<interface>`, or set `IPV4=shared` on the Mac (below) so the
host gets an IPv4 address by DHCP. Serial port: `/dev/ttyACM0`.

**Windows 11.** The in-box UsbNcm driver adds an Ethernet adapter, which
assigns itself a 169.254.x.x address. Use `ssh user@<hostname>.local` or the
Mac's 169.254 address. The serial port appears as a COM port (usbser).

## Configuration

`/etc/aurora/usb-gadget.conf` is read each time a host is plugged in. To apply
a change while a host is attached: `sudo aurora-usb-gadget down && sudo
aurora-usb-gadget up`.

| Key | Default | Meaning |
| --- | --- | --- |
| `ENABLED` | `yes` | `no` turns device mode off. `systemctl mask aurora-usb-gadget@.service` also does. |
| `FUNCTIONS` | `"ncm acm"` | Functions offered: `ncm` (network) and `acm` (serial port). |
| `IPV4` | `link-local` | `link-local`: 169.254.x.x on both sides, as macOS does. `shared`: the Mac is 10.42.0.1, gives the host an address by DHCP and shares its own connection with it (NAT); NetworkManager needs `dnsmasq` for this. `off`: IPv6 link-local only. |
| `CONSOLE` | `no` | Login prompt on the serial port. |
| `STORAGE_IMAGE` | empty | Image file offered as a USB disk. |
| `STORAGE_READONLY` | `yes` | `no` lets the host write. Never do that with an image that is mounted on the Mac at the same time. |
| `STORAGE_CDROM` | `no` | Present the image as a CD-ROM (ISO images). |

### Serial console

A USB host should not get a login prompt unasked, so the prompt is off. To
turn it on, now and whenever a host is plugged in:

    sudo aurora-usb-gadget console enable

`console disable` turns it off again. This sets `CONSOLE=` in the
configuration and starts or stops `serial-getty@ttyGS0.service`.

### Network configuration

The interface is named `usbaurora0`, so the configuration never touches other
`usb*` interfaces (such as a phone tethered to this Mac). Before binding,
`aurora-usb-gadget` writes a runtime profile for whichever network service
runs:

- NetworkManager: `/run/NetworkManager/system-connections/aurora-usb-gadget.nmconnection`,
  matched on `usbaurora*`. NetworkManager ignores USB gadget interfaces by
  default (`85-nm-unmanaged.rules`), so the udev rule marks this one as
  managed.
- systemd-networkd: `/run/systemd/network/70-aurora-usb-gadget.network`.
- Neither: the link is brought up, so the kernel adds an IPv6 link-local
  address.

`aurora-usb-gadget down` removes the profile.

## What the host sees

| Field | Value |
| --- | --- |
| Vendor:Product | `1d6b:0104`, the Linux Foundation "Multifunction Composite Gadget" ID that the kernel's own composite gadget (`g_multi`) uses |
| Device class | `0xEF/0x02/0x01` (Interface Association Descriptors), so hosts bind one driver per function |
| Strings | manufacturer "Aurora Silicon", product "Aurora USB Gadget" |
| Serial number, MAC addresses | Derived from `/etc/machine-id` with SHA-256 and a per-field label, so they are stable per install and the machine ID is not exposed. MACs are locally administered unicast addresses. |

When a Mac has more than one port in device mode, the gadget binds to the
first port that gets a host. Unplugging that port moves it to the next one.

## Commands

    aurora-usb-gadget up [UDC]          build the gadget and bind it (idempotent)
    aurora-usb-gadget down [UDC]        without UDC: unbind and remove it
    aurora-usb-gadget status
    aurora-usb-gadget console enable|disable

## Testing

`tests/test_usb_gadget.py` runs the script against a fake configfs and sysfs
tree (`AURORA_GADGET_CONFIGFS`, `AURORA_GADGET_SYSFS`, `AURORA_GADGET_RUN`,
`AURORA_GADGET_CONF`, `AURORA_GADGET_MACHINE_ID`) with stand-in `nmcli`,
`networkctl`, `systemctl`, `ip` and `modprobe`. The script was also run
against the real configfs with the kernel's `dummy_hcd` virtual controller,
including the udev and systemd hotplug path.
