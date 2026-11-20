// SPDX-License-Identifier: MIT
// Charge to Full Now in GNOME's quick settings.
//
// The toggle shows while a battery that UPower can limit has the limit on (or
// a one-shot is running) and the adapter is connected.  It is checked while
// aurora-charge-full-now's marker exists.  Clicking it runs
// "aurora-charge-full-now start" or "cancel"; the polkit rule installed with
// that command lets the active local user do so without a password.
// Everything is event driven: UPower PropertiesChanged and a file monitor on
// the marker.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {QuickToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const UPOWER = 'org.freedesktop.UPower';
const UPOWER_PATH = '/org/freedesktop/UPower';
const DEVICE = 'org.freedesktop.UPower.Device';
const MARKER = '/var/lib/aurora/charge-full-now/active';
const COMMAND = 'aurora-charge-full-now';
const TYPE_BATTERY = 2;
const STATE_FULL = 4;

function prop(proxy, name) {
    const v = proxy?.get_cached_property(name);
    return v ? v.recursiveUnpack() : null;
}

const ChargeFullToggle = GObject.registerClass(
class ChargeFullToggle extends QuickToggle {
    _init() {
        super._init({
            title: 'Charge to Full',
            iconName: 'battery-full-charging-symbolic',
            visible: false,
        });
        this._battery = null;
        this._busy = false;
        this._cancellable = new Gio.Cancellable();

        this.connect('clicked', () => this._run(this.checked ? 'cancel' : 'start'));

        // A file monitor copes with the directory not existing yet.
        this._monitor = Gio.File.new_for_path(MARKER).monitor_file(Gio.FileMonitorFlags.NONE, null);
        this._monitor.connect('changed', () => this._sync());

        this._upower = null;
        this._proxy(UPOWER_PATH, UPOWER).then(upower => {
            this._upower = upower;
            upower.connectObject(
                'g-properties-changed', () => this._sync(),
                'g-signal', () => this._findBattery(),     // DeviceAdded, DeviceRemoved
                'notify::g-name-owner', () => this._findBattery(), this);
            this._findBattery();
        }).catch(logError);
    }

    _proxy(path, iface) {
        return new Promise((resolve, reject) => {
            Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, null,
                UPOWER, path, iface, this._cancellable, (o, res) => {
                    try {
                        resolve(Gio.DBusProxy.new_for_bus_finish(res));
                    } catch (e) {
                        reject(e);
                    }
                });
        });
    }

    async _findBattery() {
        let battery = null;
        try {
            const reply = await new Promise((resolve, reject) => {
                this._upower.call('EnumerateDevices', null, Gio.DBusCallFlags.NONE, -1,
                    this._cancellable, (p, res) => {
                        try {
                            resolve(p.call_finish(res));
                        } catch (e) {
                            reject(e);
                        }
                    });
            });
            const [paths] = reply.deepUnpack();
            const devices = await Promise.all(paths.map(path => this._proxy(path, DEVICE)));
            battery = devices.find(d => prop(d, 'Type') === TYPE_BATTERY && prop(d, 'PowerSupply') &&
                prop(d, 'ChargeThresholdSupported')) ?? null;
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return;
        }
        this._setBattery(battery);
    }

    _setBattery(battery) {
        this._battery?.disconnectObject(this);
        this._battery = battery;
        this._battery?.connectObject('g-properties-changed', () => this._sync(), this);
        this._sync();
    }

    _sync() {
        const b = this._battery;
        const running = GLib.file_test(MARKER, GLib.FileTest.EXISTS);
        const limit = prop(b, 'ChargeThresholdEnabled');
        const full = prop(b, 'State') === STATE_FULL || (prop(b, 'Percentage') ?? 0) >= 100;
        const onBattery = prop(this._upower, 'OnBattery');
        this.visible = !!b && (running || (limit && !full && !onBattery));
        this.checked = running;
        this.reactive = !this._busy;
        this.subtitle = running
            ? 'Until 100 %'
            : `Limit ${prop(b, 'ChargeEndThreshold') ?? ''} %`;
    }

    _run(verb) {
        this._busy = true;
        this._sync();
        let proc;
        try {
            proc = Gio.Subprocess.new([COMMAND, verb],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE);
        } catch (e) {
            this._done(`${COMMAND} is not installed`);
            return;
        }
        proc.communicate_utf8_async(null, this._cancellable, (p, res) => {
            let out = '';
            try {
                [, out] = p.communicate_utf8_finish(res);
            } catch (e) {
                out = e.message;
            }
            this._done(p.get_successful() ? null : out.trim());
        });
    }

    _done(error) {
        this._busy = false;
        if (error)
            Main.notifyError('Charge to Full', error);
        this._sync();
    }

    destroy() {
        this._cancellable.cancel();
        this._monitor.cancel();
        this._battery?.disconnectObject(this);
        this._upower?.disconnectObject(this);
        super.destroy();
    }
});

const Indicator = GObject.registerClass(
class Indicator extends SystemIndicator {
    _init() {
        super._init();
        this.quickSettingsItems.push(new ChargeFullToggle());
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

export default class ChargeFullNowExtension extends Extension {
    enable() {
        this._indicator = new Indicator();
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
