#!/usr/bin/env python3
"""Collect the Xiaomi Pad 6 (pipa) identity and firmware inputs the port still needs measured.

This runs on the host with the tablet booted into Android and visible to adb, *not* in fastboot: the files live
inside the stock /vendor, modem and bluetooth partitions, which only a booted system exposes as a filesystem.
fastboot is only for dumping whole partitions and for flashing the port afterwards.

What it does:
  - reads the properties that identify the unit and the ROM (provenance for every digest);
  - records MemTotal, the /memory banks (from /proc/device-tree, no root needed) and the userdata partition
    geometry (start and size from sysfs), because the board file's DRAM map and userdata window are per-SKU;
  - records the panel name, the multitouch input devices, the battery design capacity and the display size, which
    are read-only hardware facts and stay valid even when the ROM is a port;
  - with --provisional, marks a run on a ported ROM: the RAM/storage/panel facts are still the hardware, but the
    /vendor and kernel digests belong to that ROM and must not be written into proprietary-files.txt as stock;
  - refuses to run unless ro.product.device is pipa, unless --force is given;
  - pulls /vendor/firmware, /vendor/firmware_mnt/image (modem, qca6390) and /vendor/bt_firmware/image (bluetooth)
    into OUT/root/, plus the Wi-Fi config files under /vendor/etc/wifi;
  - copies the live device tree from /sys/firmware/fdt when the shell can read it;
  - hashes every pulled file and writes OUT/manifest.json and OUT/manifest.tsv;
  - compares the Adreno 650 digests already recorded in proprietary-files.txt against what it pulled.

The adb serial is recorded only as a truncated SHA-256: it is a provenance key, not something to publish.

The Novatek NT36532 firmware is normally built into the stock kernel image, so it will not appear under /vendor;
the manifest notes that it must come from vendor_boot/boot (unpack_bootimg) and is left UNMEASURED on purpose.

usage: extract-pipa-firmware.py [--out DIR] [--serial SERIAL] [--force] [--skip-pull] [--provisional]
Output: DIR/manifest.json, DIR/manifest.tsv, DIR/root/... and DIR/dtb/fdt.dtb when readable.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEVICE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = DEVICE_ROOT / 'proprietary-files.txt'

PROPS = [
    'ro.product.device', 'ro.product.model', 'ro.product.name', 'ro.board.platform', 'ro.boot.hardware',
    'ro.boot.hardware.sku', 'ro.boot.product.hardware.sku', 'ro.build.fingerprint', 'ro.build.display.id',
    'ro.build.version.release', 'ro.build.version.incremental', 'ro.boot.slot_suffix',
    'ro.boot.dynamic_partitions', 'ro.boot.verifiedbootstate', 'ro.boot.flash.locked', 'ro.boot.veritymode',
    # A ported ROM can keep a pipa vendor image or replace it; these say which, and whether the DT came from pipa.
    'ro.vendor.build.fingerprint', 'ro.vendor.product.device', 'ro.system.build.fingerprint', 'ro.build.product',
    'ro.boot.hwversion', 'ro.boot.project_name', 'ro.boot.ddr_size',
]

PULL = [
    '/vendor/firmware',
    '/vendor/firmware_mnt/image',
    '/vendor/bt_firmware/image',
    '/vendor/etc/wifi',
]

# Consumer names in proprietary-files.txt that this run is expected to measure.
EXPECTED = {
    'firmware/novatek_nt36532_m82_fw_tm.bin',
    'firmware/WCNSS_qcom_cfg_qca6390.ini',
    'image/qca6390/amss20.bin',
    'image/qca6390/m3.bin',
    'image/qca6390/bdwlan.elf',
    'image/htbtfw20.tlv',
    'image/htnv20.bin',
}


def adb(serial, *args, check=True, binary=False):
    cmd = ['adb'] + (['-s', serial] if serial else []) + list(args)
    return subprocess.run(cmd, check=check, capture_output=True, text=not binary).stdout


def adb_devices():
    out = subprocess.run(['adb', 'devices'], capture_output=True, text=True).stdout
    return [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith('device')]


def shell(serial, command, check=True):
    return adb(serial, 'shell', command, check=check).strip()


def getprop(serial, name):
    return shell(serial, f'getprop {name}', check=False)


def recorded_digests():
    """SHA-256 values already claimed in proprietary-files.txt, keyed by their path."""
    digests = {}
    if not MANIFEST.is_file():
        return digests
    for line in MANIFEST.read_text().splitlines():
        fields = [field.strip() for field in line.split('|')]
        if len(fields) >= 3 and re.fullmatch(r'[0-9a-f]{64}', fields[2]):
            digests[fields[0]] = fields[2]
    return digests


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def memory_banks(serial):
    """The /memory nodes, which the bootloader fills from the unit's actual DDR configuration.

    /proc/device-tree and /sys/firmware/devicetree/base are world-readable on Android, unlike /sys/firmware/fdt,
    so this works on a stock or a ported ROM without root. A node can be /memory or /memory@<base>, and its reg
    property can hold more than one (address, size) pair.
    """
    facts = {}
    for root in ('/proc/device-tree', '/sys/firmware/devicetree/base'):
        nodes = [name for name in shell(serial, f'ls {root} 2>/dev/null', check=False).split()
                 if name == 'memory' or name.startswith('memory@')]
        if not nodes:
            continue
        banks = []
        for node in sorted(nodes):
            raw = shell(serial, f'od -An -tx1 {root}/{node}/reg 2>/dev/null | tr -d " \\n\\r"', check=False)
            raw = re.sub(r'\s+', '', raw)
            try:
                cells = [int(raw[index:index + 8], 16) for index in range(0, len(raw) - 7, 8)]
            except ValueError:
                continue
            if len(cells) % 4:
                continue
            for index in range(0, len(cells), 4):
                banks.append(((cells[index] << 32) | cells[index + 1],
                              (cells[index + 2] << 32) | cells[index + 3]))
        if banks:
            bases = [base for base, _ in banks]
            ends = [base + size for base, size in banks]
            facts['memory_banks'] = [{'base': f'{base:#x}', 'size': f'{size:#x}', 'size_gib': round(size / 2**30, 3)}
                                     for base, size in banks]
            facts['memory_total_bytes'] = sum(size for _, size in banks)
            facts['memory_span_bytes'] = max(ends) - min(bases)
            facts['suggested_adt'] = {
                'dram_base': f'{min(bases):#x}',
                'dram_size': f'{max(ends) - min(bases):#x}',
            }
            return facts
    return facts


def read_sysfs(serial, paths):
    """First line of each readable sysfs file, keyed by the label given in paths."""
    values = {}
    for label, path in paths.items():
        value = shell(serial, f'cat {path} 2>/dev/null', check=False)
        if value:
            values[label] = value.splitlines()[0].strip()
    return values


def abs_bits(text):
    """/proc/bus/input/devices prints bitmaps high word first; return them as one integer."""
    bits = 0
    for index, word in enumerate(reversed(text.split())):
        bits |= int(word, 16) << (32 * index)
    return bits


def hardware_facts(serial):
    """Panel, touch, battery and display facts, all readable without root on a stock or a ported ROM."""
    facts = {}

    # The boot DT stays pipa even when a ported ROM spoofs build.prop, so record it as hardware provenance.
    tree = {}
    model = shell(serial, "tr -d '\\000' < /proc/device-tree/model 2>/dev/null", check=False)
    model = model.replace('\x00', '').strip()
    if model:
        tree['model'] = model
    compatible = shell(serial, "tr '\\000' '\\n' < /proc/device-tree/compatible 2>/dev/null", check=False)
    entries = [line.strip() for line in compatible.replace('\x00', '\n').splitlines() if line.strip()]
    if entries:
        tree['compatible'] = entries
    if tree:
        facts['device_tree'] = tree

    # Qualcomm kernels keep the panel model in the DSI panel node of the device tree.
    nodes = shell(serial, "find /proc/device-tree /sys/firmware/devicetree/base -name 'qcom,mdss-dsi-panel-name' "
                          "-o -name 'qcom,mdss-dsi-panel-model' 2>/dev/null | head -4", check=False)
    panels = []
    for node in nodes.split():
        value = shell(serial, f"tr -d '\\000' < {node} 2>/dev/null", check=False).strip()
        if value and value not in panels:
            panels.append(value)
    if panels:
        facts['panel'] = panels

    # A touchscreen is an input device that advertises ABS_MT_POSITION_X and ABS_MT_POSITION_Y (bits 53 and 54).
    devices, current = [], {}
    for line in shell(serial, 'cat /proc/bus/input/devices 2>/dev/null', check=False).splitlines():
        if not line.strip():
            if current:
                devices.append(current)
            current = {}
        elif line.startswith('N: Name='):
            current['name'] = line.split('Name=', 1)[1].strip().strip('"')
        elif line.startswith('H: Handlers='):
            current['handlers'] = line.split('Handlers=', 1)[1].strip()
        elif line.startswith('B: ABS='):
            current['abs'] = line.split('ABS=', 1)[1].strip()
    if current:
        devices.append(current)
    touch = [device['name'] for device in devices
             if device.get('name') and 'abs' in device
             and abs_bits(device['abs']) & (1 << 53) and abs_bits(device['abs']) & (1 << 54)]
    if touch:
        facts['touch'] = touch

    battery = read_sysfs(serial, {
        'charge_full_design_uah': '/sys/class/power_supply/battery/charge_full_design',
        'voltage_max_design_uv': '/sys/class/power_supply/battery/voltage_max_design',
        'cycle_count': '/sys/class/power_supply/battery/cycle_count',
    })
    if battery:
        facts['battery'] = battery

    display = {}
    for label, command in (('physical_size', 'wm size'), ('density', 'wm density')):
        value = shell(serial, f'{command} 2>/dev/null', check=False)
        if ':' in value:
            display[label] = value.split(':', 1)[1].strip().splitlines()[0]
    if display:
        facts['display'] = display

    kernel = shell(serial, 'cat /proc/version 2>/dev/null', check=False)
    if kernel:
        facts['kernel'] = kernel.splitlines()[0]
    return facts


def device_facts(serial):
    """The per-SKU values board.toml hardcodes: installed RAM and the userdata partition geometry."""
    facts = {}
    total = shell(serial, "awk '/MemTotal/ {print $2}' /proc/meminfo", check=False)
    if total.isdigit():
        facts['memtotal_kib'] = int(total)
    facts.update(memory_banks(serial))
    facts.update(hardware_facts(serial))

    block = shell(serial, 'readlink -f /dev/block/by-name/userdata', check=False)
    facts['userdata_block'] = block or None
    name = block.rsplit('/', 1)[-1] if block else ''
    if re.fullmatch(r'sd[a-z]+\d+', name):
        start = shell(serial, f'cat /sys/class/block/{name}/start', check=False)
        size = shell(serial, f'cat /sys/class/block/{name}/size', check=False)
        if start.isdigit() and size.isdigit():
            first, count = int(start), int(size)   # 512-byte sectors
            facts.update({
                'userdata_start_sector_512': first,
                'userdata_size_sector_512': count,
                'userdata_first_lba_4k': first // 8,
                'userdata_last_lba_4k': (first + count) // 8 - 1,
                'userdata_bytes': count * 512,
            })
    return facts


def rel_to_manifest(relative):
    """Map a pulled path under root/ to the firmware/ or image/ name proprietary-files.txt uses."""
    name = Path(relative).name
    if name == 'WCNSS_qcom_cfg.ini' or name.startswith('WCNSS_qcom_cfg'):
        return 'firmware/WCNSS_qcom_cfg_qca6390.ini'
    if name in {'amss20.bin', 'm3.bin', 'bdwlan.elf'}:
        return f'image/qca6390/{name}'
    if name in {'htbtfw20.tlv', 'htnv20.bin'}:
        return f'image/{name}'
    if name == 'novatek_nt36532_m82_fw_tm.bin':
        return f'firmware/{name}'
    # The Adreno 650 images in proprietary-files.txt were recorded on a TB-J716F; pulling the pipa copies lets the
    # manifest say whether the shared SM8250-AC firmware really is byte-identical.
    if name.startswith('a650_'):
        return f'firmware/{name}'
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=Path('../pipa-firmware'), help='output directory (default ../pipa-firmware)')
    parser.add_argument('--serial', default=os.environ.get('PIPA_SERIAL', ''), help='adb serial (env PIPA_SERIAL)')
    parser.add_argument('--force', action='store_true', help='continue even when ro.product.device is not pipa')
    parser.add_argument('--skip-pull', action='store_true', help='only read identity and write the manifest')
    parser.add_argument('--provisional', action='store_true',
                        help='mark the run as a ported ROM: hardware facts are real, its firmware digests are not stock')
    args = parser.parse_args()

    serial = args.serial
    if not serial:
        devices = adb_devices()
        if len(devices) != 1:
            sys.exit(f'expected exactly one adb device, found {devices}; set PIPA_SERIAL or --serial')
        serial = devices[0]

    props = {name: getprop(serial, name) for name in PROPS}
    if props['ro.product.device'] != 'pipa' and not args.force:
        sys.exit(f"ro.product.device is {props['ro.product.device']!r}, not 'pipa'; pass --force to override")

    out = args.out.resolve()
    root = out / 'root'
    (out / 'dtb').mkdir(parents=True, exist_ok=True)
    facts = device_facts(serial)

    files = []
    if not args.skip_pull:
        for remote in PULL:
            destination = root / remote.lstrip('/')
            destination.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(['adb'] + (['-s', serial] if serial else []) + ['pull', remote, str(destination)],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                print(f'skip {remote}: {result.stderr.strip() or result.stdout.strip()}', file=sys.stderr)

        # /sys/firmware/fdt is root-only on most stock builds; keep the error instead of failing the run.
        fdt = out / 'dtb/fdt.dtb'
        data = adb(serial, 'exec-out', 'cat /sys/firmware/fdt', check=False, binary=True)
        if isinstance(data, str):
            data = data.encode()
        if data.startswith(b'\xd0\x0d\xfe\xed'):
            fdt.write_bytes(data)
            print(f'live device tree: {fdt} ({len(data)} bytes)')
        else:
            print('live device tree unreadable (needs root); dump the dtbo partition with fastboot later', file=sys.stderr)

        for path in sorted(root.rglob('*')):
            if path.is_file():
                files.append({
                    'pulled': str(path.relative_to(root)),
                    'manifest': rel_to_manifest(path.relative_to(root)),
                    'size': path.stat().st_size,
                    'sha256': sha256(path),
                })

    recorded = recorded_digests()
    for entry in files:
        if entry['manifest'] and entry['manifest'] in recorded:
            entry['recorded_sha256'] = recorded[entry['manifest']]
            entry['matches_recorded'] = entry['recorded_sha256'] == entry['sha256']

    notes = {
        'novatek': 'NT36532 firmware is built into the stock kernel image; extract it from vendor_boot/boot, '
                   'not from /vendor, and record the kernel build it came from.',
        'unmeasured': 'Replace UNMEASURED in proprietary-files.txt only with a digest from this manifest.',
        'sku': 'Compare device_facts with [userdata] first_lba/last_lba and the DRAM map in board.toml: both are '
               'per-SKU. The tree carries this 8 GB / 256 GB unit\'s measured values; re-run on a different unit '
               'before flashing it.',
    }
    if args.provisional:
        notes['provisional'] = ('This run came from a ported ROM. The RAM, storage, panel and battery facts are the '
                                'hardware; the /vendor and kernel digests belong to that ROM and are provisional until '
                                'a stock image confirms them.')
    memory_total = facts.get('memory_total_bytes')
    memtotal = facts.get('memtotal_kib')
    if memory_total and memtotal and memory_total < memtotal * 1024:
        notes['memory'] = ('The DT memory nodes hold less than MemTotal, so the live tree does not describe the '
                           'installed RAM; do not use its banks for board.toml.')

    measured = {entry['manifest'] for entry in files if entry['manifest']}
    manifest = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'provenance': {
            'serial_sha256_16': hashlib.sha256(serial.encode()).hexdigest()[:16],
            'props': props,
            'device_facts': facts,
            'source': 'booted Android, adb pull of /vendor, /vendor/firmware_mnt and /vendor/bt_firmware',
            'provisional': args.provisional,
        },
        'files': files,
        'expected_but_missing': sorted(EXPECTED - measured),
        'notes': notes,
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with (out / 'manifest.tsv').open('w') as handle:
        handle.write('manifest\tpulled\tsha256\tsize\trecorded\n')
        for entry in files:
            handle.write(f"{entry['manifest'] or '-'}\t{entry['pulled']}\t{entry['sha256']}\t{entry['size']}\t"
                         f"{entry.get('recorded_sha256', '-')}\n")

    print(json.dumps({'out': str(out), 'files': len(files), 'missing': manifest['expected_but_missing'],
                      'device': props['ro.product.device'], 'fingerprint': props['ro.build.fingerprint'],
                      'memtotal_kib': facts.get('memtotal_kib'),
                      'memory_banks': facts.get('memory_banks'),
                      'suggested_adt': facts.get('suggested_adt'),
                      'panel': facts.get('panel'),
                      'touch': facts.get('touch'),
                      'userdata_bytes': facts.get('userdata_bytes')}, indent=2))


if __name__ == '__main__':
    main()
