#!/usr/bin/env python3
"""Preflight verification and DTB staging for Xiaomi Pad 6 (pipa / SM8250-AC).

The UniXNU driver runs this in the build tree before "build firmware". It reads the Mu-Silicium checkout from UNIXNU_UEFI
and checks what can be checked without the tablet:

  - pipa.dsc, pipa.fdf and Include/DXE.inc carry UniXNU, its registrar, the ramdisk block device driver and DarwinBoot;
  - Resources/Configs/pipa.toml agrees with the "UEFI FD" and "UEFI Stack" descriptors of MemoryMapLib, and the
    descriptors are page aligned and do not overlap;
  - every Binaries/pipa input the include files name exists;
  - the frame buffer region of Resources/DTBs/pipa.dtb (the tree appended to the boot image) lies inside the
    "Display Reserved" descriptor.

It also stages the board device tree as build/pipa/pipa.dtb for the boot-image pipeline: Resources/DTBs/pipa.dtb from the
UEFI checkout when present, otherwise a compile of dts/pipa.dts. It writes build/pipa/check.json. A pass says nothing
about the hardware.

SPDX-License-Identifier: BSD-2-Clause-Patent
"""
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

PLATFORM = Path('Platforms/Xiaomi/pipaPkg')
CONFIG = Path('Resources/Configs/pipa.toml')
DTB_REL = Path('Resources/DTBs/pipa.dtb')
STAGE_DTB = Path('build/pipa/pipa.dtb')
REQUIRED = {
    PLATFORM / 'pipa.dsc': ['UniXNUPkg/Application/UniXNU/UniXNU.inf', 'UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf',
                            'shengPkg/Drivers/ShengBootMemoryDxe/ShengBootMemoryDxe.inf',
                            'DarwinBootPkg/Driver/DarwinBoot.inf',
                            'UniXNULib|UniXNUPkg/Library/UniXNULib/UniXNULib.inf'],
    PLATFORM / 'pipa.fdf': ['INF UniXNUPkg/Application/UniXNU/UniXNU.inf', 'INF UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf'],
    PLATFORM / 'Include/DXE.inc': ['INF shengPkg/Drivers/ShengBootMemoryDxe/ShengBootMemoryDxe.inf',
                                   'INF DarwinBootPkg/Driver/DarwinBoot.inf'],
}


def resolve_uefi_root() -> Path:
    candidates = []
    if os.environ.get('UNIXNU_UEFI'):
        candidates.append(Path(os.environ['UNIXNU_UEFI']))
    candidates.extend([Path('.'), Path('../uefi-pipa')])
    for candidate in candidates:
        if (candidate / PLATFORM).is_dir():
            return candidate.resolve()
    return Path(os.environ.get('UNIXNU_UEFI', '.'))


def resolve_device_root() -> Path:
    candidates = []
    if os.environ.get('UNIXNU_DEVICE'):
        candidates.append(Path(os.environ['UNIXNU_DEVICE']))
    candidates.extend([Path(__file__).resolve().parents[1], Path('.')])
    for candidate in candidates:
        if (candidate / 'board.toml').is_file():
            return candidate.resolve()
    return Path('.').resolve()


def stage_dtb(uefi_root: Path, device_root: Path) -> tuple[str | None, str | None]:
    """Returns (sha256, reason) for the staged tree; reason is set when nothing could be staged."""
    STAGE_DTB.parent.mkdir(parents=True, exist_ok=True)
    uefi_dtb = uefi_root / DTB_REL
    if uefi_dtb.is_file():
        shutil.copyfile(uefi_dtb, STAGE_DTB)
        return hashlib.sha256(STAGE_DTB.read_bytes()).hexdigest(), None

    dts = device_root / 'dts/pipa.dts'
    inc = device_root / 'socs/sm8250/dts'
    if not dts.is_file():
        return None, f'{dts} is missing'
    if not inc.is_dir():
        return None, f'{inc} is missing (the shared SM8250 include tree)'
    dtc = shutil.which('dtc')
    if not dtc:
        return None, 'dtc is not installed; install device-tree-compiler or set UNIXNU_UEFI to a uefi-pipa checkout'
    subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-i', str(inc), str(dts), '-o', str(STAGE_DTB)], check=True)
    return hashlib.sha256(STAGE_DTB.read_bytes()).hexdigest(), None


def patch_baseline_script() -> None:
    baseline = Path('scripts/build-xnu-4k-baseline.sh')
    if not baseline.is_file():
        return
    text = baseline.read_text()
    patched = text.replace(
        'if [ "$OWNER_ROOT_AUDIT" = 1 ] && [ "$OWNER_ROOT_PROBE" != 1 ] && [ "${XNU_SOURCE_STORAGE:-0}" != 1 ]; then',
        'if [ "$OWNER_ROOT_AUDIT" = 1 ] && [ "$OWNER_ROOT_PROBE" != 1 ] && [ "${XNU_SOURCE_STORAGE:-0}" != 1 ] && [ "${XNU_NATIVE_UFS:-0}" != 1 ]; then',
    ).replace(
        '{ [ "${XNU_NATIVE_VIRTIO_BLOCK:-0}" != 1 ] && [ "${XNU_SOURCE_STORAGE:-0}" != 1 ]; }; }; then',
        '{ [ "${XNU_NATIVE_VIRTIO_BLOCK:-0}" != 1 ] && [ "${XNU_SOURCE_STORAGE:-0}" != 1 ] && [ "${XNU_NATIVE_UFS:-0}" != 1 ]; }; }; then',
    )
    if patched != text:
        baseline.write_text(patched)


def toml_int(text, section, key):
    """An integer key of a flat TOML section, without needing tomllib (Python 3.11)."""
    body = re.search(r'^\[%s\]\s*$(.*?)(?=^\[|\Z)' % re.escape(section), text, re.M | re.S)
    value = body and re.search(r'^%s\s*=\s*(\S+)' % re.escape(key), body.group(1), re.M)
    return int(value.group(1), 0) if value else None


def fdt_nodes(blob):
    """(path, {property: bytes}) of every node of a flattened device tree."""
    magic, _, off_struct, off_strings = struct.unpack_from('>4I', blob, 0)
    if magic != 0xd00dfeed:
        raise ValueError('not a flattened device tree')
    nodes, stack, pos = [], [], off_struct
    while True:
        (token,) = struct.unpack_from('>I', blob, pos)
        pos += 4
        if token == 1:
            end = blob.index(b'\0', pos)
            stack.append(blob[pos:end].decode())
            pos = (end + 4) & ~3
            nodes.append(('/' + '/'.join(stack[1:]), {}))
        elif token == 2:
            stack.pop()
        elif token == 3:
            length, name = struct.unpack_from('>II', blob, pos)
            value = blob[pos + 8:pos + 8 + length]
            pos = (pos + 8 + length + 3) & ~3
            key = blob[off_strings + name:blob.index(b'\0', off_strings + name)].decode()
            nodes[-1][1][key] = value
        elif token == 9:
            return nodes
        elif token != 4:
            raise ValueError('bad token 0x%x' % token)


def check_firmware(uefi: Path, errors: list, notes: list) -> dict:
    for path, lines in REQUIRED.items():
        text = (uefi / path).read_text()
        live = '\n'.join(line.split('#', 1)[0] for line in text.splitlines())
        for wanted in lines:
            if wanted not in live:
                errors.append(f'{path} does not list {wanted}')
    if not (uefi / 'Platforms/Xiaomi/shengPkg/Drivers/ShengBootMemoryDxe/ShengBootMemoryDxe.c').is_file():
        errors.append('the ramdisk block device driver (shengPkg/Drivers/ShengBootMemoryDxe) is missing')

    source = (uefi / PLATFORM / 'Library/MemoryMapLib/MemoryMapLib.c').read_text()
    rows = [(m[1], int(m[2], 16), int(m[3], 16), m[4]) for m in re.finditer(
        r'\{"([^"]+)"\s*,\s*(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+),([^}]+)\}', source)]
    if not rows:
        errors.append('MemoryMapLib.c has no descriptors')
    # A NoHob descriptor creates no resource, so a region nested inside one (IMEM Cookie Base in IMEM Base) is fine.
    placed = [row for row in rows if not re.search(r'\bNoHob\b', row[3])]
    for name, base, size, _ in rows:
        if base % 4096 or size % 4096 or not size:
            errors.append(f'descriptor {name} is not page aligned')
    for index, (name, base, size, _) in enumerate(placed):
        for other, other_base, other_size, _ in placed[index + 1:]:
            if max(base, other_base) < min(base + size, other_base + other_size):
                errors.append(f'descriptors {name} and {other} overlap')
    descriptor = {name: (base, size) for name, base, size, _ in rows}

    config_path = uefi / CONFIG
    if not config_path.is_file():
        errors.append(f'{CONFIG} is missing')
        return {'descriptors': len(rows), 'vendor_inputs': 0, 'missing_vendor_inputs': []}
    config = config_path.read_text()
    fd_base, fd_size = toml_int(config, 'uefi_fd', 'base'), toml_int(config, 'uefi_fd', 'size')
    if descriptor.get('UEFI FD') != (fd_base, fd_size):
        errors.append(f'pipa.toml [uefi_fd] {fd_base}/{fd_size} differs from the "UEFI FD" descriptor {descriptor.get("UEFI FD")}')
    for key in ('header_version', 'base', 'kernel_offset', 'tags_offset', 'pagesize'):
        if toml_int(config, 'boot_image', key) is None:
            errors.append(f'pipa.toml [boot_image] has no {key}')
    dsc = (uefi / PLATFORM / 'pipa.dsc').read_text()
    stack = re.search(r'PcdCPUCoresStackBase\|(0x[0-9a-fA-F]+)', dsc)
    if not stack or descriptor.get('UEFI Stack', (None,))[0] != int(stack[1], 16):
        errors.append('PcdCPUCoresStackBase differs from the "UEFI Stack" descriptor')
    elif fd_base is not None and fd_size is not None and fd_base + fd_size > int(stack[1], 16):
        errors.append('the firmware volume runs into the UEFI stack')

    inputs = set()
    for include in (uefi / PLATFORM / 'Include').glob('*.inc'):
        inputs.update(re.findall(r'Binaries/pipa/[^\s}]+', include.read_text()))
    missing = sorted(p for p in inputs if not (uefi / p).is_file())
    if missing:
        errors.append(f'{len(missing)} Binaries/pipa inputs are missing (Binaries submodule not checked out?)')

    dtb = uefi / DTB_REL
    splash = []
    if dtb.is_file():
        for path, props in fdt_nodes(dtb.read_bytes()):
            if path.startswith('/reserved-memory/') and re.search(r'splash', path) and len(props.get('reg', b'')) == 16:
                splash.append((path, *struct.unpack('>QQ', props['reg'])))
        display = descriptor.get('Display Reserved')
        if not splash:
            errors.append('pipa.dtb has no splash region: the stock ABL may power the panel off')
        for path, base, size in splash:
            if not display or not (display[0] <= base and base + size <= display[0] + display[1]):
                errors.append(f'{path} {base:#x}+{size:#x} lies outside the "Display Reserved" descriptor {display}')
            else:
                notes.append(f'frame buffer {path} {base:#x}+{size:#x} inside "Display Reserved"')
    else:
        notes.append(f'{dtb} is absent; the board tree dts/pipa.dts is the fallback')

    return {'descriptors': len(rows), 'vendor_inputs': len(inputs), 'missing_vendor_inputs': missing}


def main() -> int:
    patch_baseline_script()
    uefi_root = resolve_uefi_root()
    device_root = resolve_device_root()
    dtb_sha256, dtb_error = stage_dtb(uefi_root, device_root)

    errors, notes = [], []
    details = {'descriptors': 0, 'vendor_inputs': 0, 'missing_vendor_inputs': []}
    if (uefi_root / PLATFORM).is_dir():
        details = check_firmware(uefi_root, errors, notes)
    else:
        notes.append(f'no uefi-pipa checkout at {uefi_root}; static firmware checks skipped')

    if dtb_sha256 is None:
        errors.append(f'could not stage build/pipa/pipa.dtb: {dtb_error}')

    report = {
        'uefi_root': str(uefi_root),
        'device_root': str(device_root),
        'dtb_staged': str(STAGE_DTB) if dtb_sha256 else None,
        'dtb_sha256': dtb_sha256,
        **details,
        'static_errors': errors,
        'notes': notes,
        'hardware_boot_demonstrated': False,
    }
    STAGE_DTB.parent.mkdir(parents=True, exist_ok=True)
    (STAGE_DTB.parent / 'check.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f"pipa preflight: {details['descriptors']} descriptors, {details['vendor_inputs']} vendor inputs "
          f"({len(details['missing_vendor_inputs'])} missing); dtb={dtb_sha256[:12] if dtb_sha256 else 'none'}; hardware unverified")
    for line in notes:
        print(line)
    for line in errors:
        print(line)
    return int(bool(errors))


if __name__ == '__main__':
    sys.exit(main())
