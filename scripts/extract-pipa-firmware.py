#!/usr/bin/env python3
"""Collect the Xiaomi Pad 6 (pipa) identity and firmware inputs the port still needs measured.

This runs on the host with the tablet booted into Android and visible to adb, *not* in fastboot: the files live
inside the stock /vendor, modem and bluetooth partitions, which only a booted system exposes as a filesystem.
fastboot is only for dumping whole partitions and for flashing the port afterwards.

What it does:
  - reads the properties that identify the unit and the ROM (provenance for every digest);
  - refuses to run unless ro.product.device is pipa, unless --force is given;
  - pulls /vendor/firmware, /vendor/firmware_mnt/image (modem, qca6390) and /vendor/bt_firmware/image (bluetooth)
    into OUT/root/, plus the Wi-Fi config files under /vendor/etc/wifi;
  - copies the live device tree from /sys/firmware/fdt when the shell can read it;
  - hashes every pulled file and writes OUT/manifest.json and OUT/manifest.tsv;
  - compares the Adreno 650 digests already recorded in proprietary-files.txt against what it pulled.

The Novatek NT36532 firmware is normally built into the stock kernel image, so it will not appear under /vendor;
the manifest notes that it must come from vendor_boot/boot (unpack_bootimg) and is left UNMEASURED on purpose.

usage: extract-pipa-firmware.py [--out DIR] [--serial SERIAL] [--force] [--skip-pull]
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
    return adb(serial, 'shell', command, check=check).decode(errors='replace').strip()


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
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=Path('../pipa-firmware'), help='output directory (default ../pipa-firmware)')
    parser.add_argument('--serial', default=os.environ.get('PIPA_SERIAL', ''), help='adb serial (env PIPA_SERIAL)')
    parser.add_argument('--force', action='store_true', help='continue even when ro.product.device is not pipa')
    parser.add_argument('--skip-pull', action='store_true', help='only read identity and write the manifest')
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

    measured = {entry['manifest'] for entry in files if entry['manifest']}
    manifest = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'provenance': {
            'serial': serial,
            'props': props,
            'source': 'booted Android, adb pull of /vendor, /vendor/firmware_mnt and /vendor/bt_firmware',
        },
        'files': files,
        'expected_but_missing': sorted(EXPECTED - measured),
        'notes': {
            'novatek': 'NT36532 firmware is built into the stock kernel image; extract it from vendor_boot/boot, '
                       'not from /vendor, and record the kernel build it came from.',
            'unmeasured': 'Replace UNMEASURED in proprietary-files.txt only with a digest from this manifest.',
        },
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with (out / 'manifest.tsv').open('w') as handle:
        handle.write('manifest\tpulled\tsha256\tsize\trecorded\n')
        for entry in files:
            handle.write(f"{entry['manifest'] or '-'}\t{entry['pulled']}\t{entry['sha256']}\t{entry['size']}\t"
                         f"{entry.get('recorded_sha256', '-')}\n")

    print(json.dumps({'out': str(out), 'files': len(files), 'missing': manifest['expected_but_missing'],
                      'device': props['ro.product.device'], 'fingerprint': props['ro.build.fingerprint']}, indent=2))


if __name__ == '__main__':
    main()
