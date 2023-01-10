# https://hackaday.io/project/5301-reverse-engineering-a-low-cost-usb-co-monitor

import hid
import os
import sys
import time
import logging as log
from .config import ConfigFile

CO2_USB_MFG = 'Holtek'
CO2_USB_PRD = 'USB-zyTemp'

# Ignore first 5 measurements during self-calibration after power-up
IGNORE_N_MEASUREMENTS = 5

# key used to decrypt
KEY = [0xc4, 0xc6, 0xc0, 0x92, 0x40, 0x23, 0xdc, 0x96]

l = log.getLogger('zytemp')


class ZyTemp():
    MEASUREMENTS = {
        0x42: {
            'name': 'Temperature',
            'unit': '°C',
            'conversion': lambda x: x / 16 - 273.15,
            'ha_device_class': 'temperature',
            'ha_icon': 'mdi:thermometer',
        },
        0x50: {
            'name': 'CO2',
            'unit': 'ppm',
            'conversion': lambda x: x,
            'ha_device_class': 'carbon_dioxide',
            'ha_icon': 'mdi:molecule-co2',
        },
    }

    def __init__(self, hiddev, mqtt):
        self.cfg = ConfigFile()
        self.m = mqtt
        self.h = hiddev
        self.h.send_feature_report(b'\xc4\xc6\xc0\x92\x40\x23\xdc\x96')
        self.measurements_to_ignore = IGNORE_N_MEASUREMENTS
        self.values = {v['name']: None for v in ZyTemp.MEASUREMENTS.values()}

    def __del__(self):
        self.h.close()

    """ MQTT Discovery for Home Assistant """

    def discovery(self):
        if not len(self.cfg.discovery_prefix):
            return

        for meas in ZyTemp.MEASUREMENTS.values():
            id = os.path.basename(self.cfg.mqtt_topic)
            config_content = {
                'device': {
                    'identifiers': [id],
                    'manufacturer': CO2_USB_MFG,
                    'model': CO2_USB_PRD,
                    'name': self.cfg.friendly_name,
                },
                'enabled_by_default': True,
                'state_class': 'measurement',
                'device_class': meas['ha_device_class'],
                'name': ' '.join((self.cfg.friendly_name, meas['name'])),
                'state_topic': self.cfg.mqtt_topic,
                'unique_id': '_'.join((id, meas['name'])),
                'unit_of_measurement': meas['unit'],
                'value_template': '{{ value_json.%s }}' % meas['name'],
                'icon': meas['ha_icon']
            }
            self.m.publish(
                os.path.join(
                    self.cfg.discovery_prefix, 'sensor', config_content['unique_id'], 'config'
                ),
                config_content,
                retain=True
            )

        self.m.run(0.1)

    def update(self, key, value):
        if self.values[key] == value:
            return

        self.values[key] = value

        if any(v is None for v in self.values.values()):
            return

        self.m.publish(self.cfg.mqtt_topic, self.values)

    def run(self):
        self.discovery()
        while True:
            try:
                r = self.h.read(8)
            except OSError as err:
                l.log(log.ERROR, f'OS error: {err}')
                return

            if not r:
                l.log(log.ERROR, f'Read error')
                self.h.close()
                return

            if r[4] != 0x0d:
                l.log(log.DEBUG, f'Encrypted data from device')
                r = decrypt(KEY, r)

            if r[3] != sum(r[0:3]) & 0xff:
                l.log(log.ERROR, f'Checksum error')
                continue

            m_type = r[0]
            m_val = r[1] << 8 | r[2]

            try:
                m = ZyTemp.MEASUREMENTS[m_type]
            except KeyError:
                l.log(log.DEBUG, f'Unknown key {m_type:02x}')
                continue

            m_name, m_unit, m_reading = m['name'], m['unit'], m['conversion'](
                m_val)

            ignore = self.measurements_to_ignore > 0

            l.log(log.DEBUG, f'{m_name}: {m_reading:g} {m_unit}' +
                  (' (ignored)' if ignore else ''))

            if not ignore:
                self.update(m_name, m_reading)
            self.m.run(0.1)

            if m_name == 'CO2' and self.measurements_to_ignore:
                self.measurements_to_ignore -= 1


def get_hiddev():
    hid_sensors = [
        e for e in hid.enumerate()
        if e['manufacturer_string'] == CO2_USB_MFG
        and e['product_string'] == CO2_USB_PRD
    ]

    p = []
    for s in hid_sensors:
        intf, path, vid, pid = (
            s[k] for k in
            ('interface_number', 'path', 'vendor_id', 'product_id')
        )
        path_str = path.decode('utf-8')
        l.log(log.INFO,
              f'Found CO2 sensor at intf. {intf}, {path_str}, VID={vid:04x}, PID={pid:04x}')
        p.append(path)

    if not p:
        l.log(log.ERROR, 'No device found')
        return None

    l.log(log.INFO, f'Using device at {p[0].decode("utf-8")}')
    h = hid.device()
    h.open_path(path)
    return h

# taken from https://hackaday.io/project/5301-reverse-engineering-a-low-cost-usb-co-monitor/log/17909-all-your-base-are-belong-to-us
def decrypt(key,  data):
    cstate = [0x48,  0x74,  0x65,  0x6D,  0x70,  0x39,  0x39,  0x65]
    shuffle = [2, 4, 0, 7, 1, 6, 5, 3]

    phase1 = [0] * 8
    for i, o in enumerate(shuffle):
        phase1[o] = data[i]

    phase2 = [0] * 8
    for i in range(8):
        phase2[i] = phase1[i] ^ key[i]

    phase3 = [0] * 8
    for i in range(8):
        phase3[i] = ( (phase2[i] >> 3) | (phase2[ (i-1+8)%8 ] << 5) ) & 0xff

    ctmp = [0] * 8
    for i in range(8):
        ctmp[i] = ( (cstate[i] >> 4) | (cstate[i]<<4) ) & 0xff

    out = [0] * 8
    for i in range(8):
        out[i] = (0x100 + phase3[i] - ctmp[i]) & 0xff

    return out