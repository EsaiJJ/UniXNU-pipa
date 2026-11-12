#!/usr/bin/env python3
"""Offline checks of board.toml, dts/pipa.dts and proprietary-files.txt.

This is the CI step that used to be an inline heredoc, plus the cross-checks the SKU-dependent values need:

  - board name, SoC, kit, defconfig and device-tree/boot-image paths are the pipa ones;
  - the kernel arguments the port depends on are present and the unported ones are absent;
  - every manifest entry is well formed and at least seven digests are measured;
  - the memory banks in dts/pipa.dts add up to the DRAM the ADT and payload claim; --ddr-gb N also asserts the bank
    total matches that capacity;
  - the [userdata] window is internally consistent, is reported in GiB and GB, and can be asserted against a
    real storage SKU with --storage-gb.

usage: check-pipa-board.py [--board board.toml] [--dts dts/pipa.dts] [--manifest proprietary-files.txt]
                           [--ddr-gb N] [--storage-gb N]
Exit status 1 on any error. The SKU flags are optional: without them the measured values are printed as notes;
with them the bank total and userdata window are asserted against that capacity.
"""
import argparse
import re
import sys
import tomllib
from pathlib import Path

REQUIRED_ARGS = ('pipa-touch=5', 'pipa-buttons=1', 'pipa-gpu=12',
                 'sheng-gpu-addon=1', 'sheng-fb-gpu=1', 'native-ufs-root=1')
FORBIDDEN_ARGS = ('j716f-touch=1', 'j716f-usb=1', 'sheng-tum=s',
                  'sheng-backlight=1', 'sheng-wifi=4', 'sheng-bluetooth=1')


def memory_banks(dts_text):
    """(base, size) of every /memory node, read without a DT compiler."""
    banks = []
    for name, body in re.findall(r'memory@([0-9a-fA-F]+)\s*\{([^}]*)\}', dts_text):
        reg = re.search(r'reg\s*=\s*<([^>]*)>', body)
        if not reg:
            raise ValueError(f'memory@{name} has no reg property')
        cells = [int(value, 16) for value in re.findall(r'0x[0-9a-fA-F]+', reg.group(1))]
        if len(cells) != 4:
            raise ValueError(f'memory@{name} does not use 64-bit address/size cells')
        banks.append(((cells[0] << 32) | cells[1], (cells[2] << 32) | cells[3]))
    return banks


def check(args):
    errors, notes = [], []
    board = tomllib.loads(Path(args.board).read_text())

    # Board identity and boot wiring.
    if board['board']['name'] != 'pipa':
        errors.append(f"board.name is {board['board']['name']!r}, not 'pipa'")
    if board['board']['soc'] != 'SM8250':
        errors.append(f"board.soc is {board['board']['soc']!r}, not 'SM8250'")
    if board['board']['kit'] != 'pipa':
        errors.append(f"board.kit is {board['board']['kit']!r}, not 'pipa'")
    kernel_args = board['payload']['kernel_args']
    for wanted in REQUIRED_ARGS:
        if wanted not in kernel_args:
            errors.append(f'kernel_args is missing {wanted}')
    for absent in FORBIDDEN_ARGS:
        if absent in kernel_args:
            errors.append(f'kernel_args still sets {absent}, which is not ported')
    if board['package']['defconfig'].get('ipados-17.7.11-21H461') != 'pipa_defconfig':
        errors.append('package.defconfig does not select pipa_defconfig')
    if not Path('configs/pipa_defconfig').is_file():
        errors.append('configs/pipa_defconfig is missing')
    if board['device_tree']['source'] != 'device:dts/pipa.dts':
        errors.append('device_tree.source is not device:dts/pipa.dts')
    if board['bootimg']['dtb'] != 'build/pipa/pipa.dtb':
        errors.append('bootimg.dtb is not build/pipa/pipa.dtb')

    # Firmware manifest.
    entries = [line.strip() for line in Path(args.manifest).read_text().splitlines()
               if line.strip() and not line.strip().startswith('#')]
    measured = 0
    for line in entries:
        parts = [part.strip() for part in line.split('|')]
        if len(parts) != 4:
            errors.append(f'bad manifest entry: {line}')
            continue
        if parts[2] == 'UNMEASURED':
            continue
        if not re.fullmatch(r'[0-9a-f]{64}', parts[2]):
            errors.append(f'bad digest: {line}')
        measured += 1
    if len(entries) < 10:
        errors.append(f'only {len(entries)} firmware entries')
    if measured < 7:
        errors.append(f'only {measured} measured firmware digests')

    # Memory: the DT banks, the ADT span and the payload must describe the same device.
    banks = memory_banks(Path(args.dts).read_text())
    if not banks:
        errors.append('dts has no /memory nodes')
    else:
        adt = board['payload']['adt']
        dram_base, dram_size, owned = int(adt['dram_base'], 0), int(adt['dram_size'], 0), int(adt['owned_memory_size'], 0)
        total = sum(size for _, size in banks)
        end = max(base + size for base, size in banks)
        span = end - dram_base
        if dram_size != span:
            errors.append(f'payload.adt.dram_size is {dram_size:#x}, but the banks span {span:#x}')
        if int(board['payload']['dram_size'], 0) != dram_size:
            errors.append('payload.dram_size and payload.adt.dram_size differ')
        if not 0 < owned <= total:
            errors.append(f'owned_memory_size {owned:#x} is outside the {total:#x} bytes of RAM in the DT')
        notes.append(f'{len(banks)} memory banks, {total / 2**30:.2f} GiB usable, span {span:#x} from {dram_base:#x}')
        if args.ddr_gb is not None and round(total / 2**30) != args.ddr_gb:
            errors.append(f'the DT describes {total / 2**30:.1f} GiB of RAM, not {args.ddr_gb} GiB')

    # Userdata: last_lba and the erase step are per storage SKU.
    userdata = board['userdata']
    if userdata['last_lba'] <= userdata['first_lba']:
        errors.append('userdata.last_lba is not after first_lba')
    size = (userdata['last_lba'] - userdata['first_lba'] + 1) * userdata['lba_bytes']
    notes.append(f"userdata LBA {userdata['first_lba']:#x}..{userdata['last_lba']:#x} "
                 f"({userdata['lba_bytes']}-byte blocks), {size / 1e9:.1f} GB / {size / 2**30:.1f} GiB")
    if args.storage_gb is not None:
        if not 0.85 * args.storage_gb * 1e9 <= size <= args.storage_gb * 1e9:
            errors.append(f'the userdata window is {size / 1e9:.1f} GB, which does not fit a {args.storage_gb} GB unit')
        else:
            notes.append(f'the window fits a {args.storage_gb} GB unit; this is the measured GPT, so re-check it '
                         'if the tablet is ever repartitioned')
    else:
        notes.append('pass --storage-gb to assert this window against a storage SKU: a wrong last_lba erases the '
                     'wrong range')

    for line in notes:
        print(f'note: {line}')
    for line in errors:
        print(f'error: {line}')
    print(f"{len(entries)} firmware entries ({measured} measured); {len(errors)} error(s)")
    return int(bool(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--board', default='board.toml')
    parser.add_argument('--dts', default='dts/pipa.dts')
    parser.add_argument('--manifest', default='proprietary-files.txt')
    parser.add_argument('--ddr-gb', type=int, help='assert the DT describes this much RAM (8 or 16 on pipa)')
    parser.add_argument('--storage-gb', type=int, help='assert the userdata window fits this nominal storage size')
    sys.exit(check(parser.parse_args()))


if __name__ == '__main__':
    main()
