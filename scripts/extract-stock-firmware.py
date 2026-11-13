#!/usr/bin/env python3
"""Extract the Xiaomi Pad 6 (pipa) firmware inputs from an official Xiaomi fastboot ROM.

This runs on the host and needs no tablet and no fastboot. It reads the images/ directory of an unpacked
``pipa_global_images_*.tgz``:

  - hashes every stock image, so the manifest says exactly which ROM a digest came from;
  - opens NON-HLOS.bin and BTFM.bin as FAT16 filesystems (they are, not ext4/sparse images), walks the
    directory tree and extracts image/qca6390/* and the four image/ht* Bluetooth files;
  - parses dtbo.img (big-endian header and 32-byte entries) and extracts the DTB whose model contains
    "xiaomi pipa"; the entry index, id and revision are recorded, and --all-dtbo writes every entry;
  - parses gpt_both*.bin and reports each LUN's partitions, so a template userdata entry that stops at the
    LUN boundary is distinguishable from the live sda34 window;
  - with --touch, extracts the kernel from boot.img and reads the built-in firmware table out of it. The
    Novatek NT36532 image is compiled into the stock kernel, not stored in /vendor, so this is the only
    way to get the stock blob. It runs vmlinux-to-elf to recover kallsyms symbol names, then pairs each
    ``_fw_*_name`` symbol with its ``_fw_*_bin`` data symbol and the sizes in ``__start_builtin_fw``;
  - compares every extracted digest with the value already recorded in proprietary-files.txt, so a
    mismatch is visible before anything is committed;
  - writes OUT/manifest.json and OUT/manifest.tsv.

The extracted blobs stay outside this repository. Only the digests and the provenance belong in
proprietary-files.txt / firmware/README.md.

usage: extract-stock-firmware.py --images /path/to/images [--out DIR] [--touch] [--all-dtbo]
                                 [--vmlinux-to-elf PATH] [--manifest proprietary-files.txt]
Output: DIR/manifest.json, DIR/manifest.tsv, DIR/modem/qca6390/*, DIR/bt/image/*, DIR/dtbo/*.dtb,
        and DIR/touch/*.bin with --touch.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEVICE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = DEVICE_ROOT / 'proprietary-files.txt'

STOCK_IMAGES = (
    'boot.img', 'vendor_boot.img', 'dtbo.img', 'NON-HLOS.bin', 'BTFM.bin',
    'gpt_both0.bin', 'gpt_both1.bin', 'gpt_both2.bin', 'gpt_both3.bin', 'gpt_both4.bin', 'gpt_both5.bin',
)

DTBO_MAGIC = 0xD7B7AB1E
PIPA_MODEL = b'xiaomi pipa'
GPT_MAGIC = b'EFI PART'

# The kernel is loaded at this address on pipa; vmlinux-to-elf recovers it from the relocation table but this
# fallback keeps the raw-kernel offset calculation readable if the ELF has no LOAD segment.
PIPA_KERNEL_LOAD = 0xFFFFFF8008080000


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def recorded_digests(path):
    """SHA-256 values claimed in proprietary-files.txt, keyed by their manifest path."""
    path = Path(path)
    if not path.is_file():
        return {}
    recorded = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [part.strip() for part in line.split('|')]
        if len(parts) == 4:
            recorded[parts[0]] = parts[2]
    return recorded


class Fat16:
    """Read-only FAT16 reader, enough for the firmware images Xiaomi ships as .bin files."""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = self.path.open('rb')
        header = self.handle.read(512)
        if len(header) < 512:
            raise ValueError(f'{self.path} is too small to hold a FAT boot sector')
        (self.bytes_per_sector, self.sectors_per_cluster, self.reserved_sectors, self.fat_count,
         self.root_entry_count, self.total16, self.media, self.fat16_sectors) = \
            struct.unpack_from('<HBHBHHBH', header, 11)
        self.total32 = struct.unpack_from('<I', header, 32)[0]
        self.fat32_sectors = struct.unpack_from('<I', header, 36)[0]
        self.fat_sectors = self.fat16_sectors or self.fat32_sectors
        if not (512 <= self.bytes_per_sector <= 4096) or self.bytes_per_sector & (self.bytes_per_sector - 1):
            raise ValueError(f'{self.path} has an implausible FAT sector size {self.bytes_per_sector}')
        if self.fat16_sectors == 0:
            raise ValueError(f'{self.path} is FAT32, not the FAT16 firmware image this tool expects')
        total_sectors = self.total16 or self.total32
        self.root_sectors = (self.root_entry_count * 32 + self.bytes_per_sector - 1) // self.bytes_per_sector
        self.fat_offset = self.reserved_sectors * self.bytes_per_sector
        self.root_offset = (self.reserved_sectors + self.fat_count * self.fat_sectors) * self.bytes_per_sector
        self.data_offset = (self.reserved_sectors + self.fat_count * self.fat_sectors + self.root_sectors) * \
            self.bytes_per_sector
        self.cluster_bytes = self.bytes_per_sector * self.sectors_per_cluster
        data_sectors = total_sectors - (self.reserved_sectors + self.fat_count * self.fat_sectors + self.root_sectors)
        self.clusters = data_sectors // self.sectors_per_cluster if data_sectors > 0 else 0
        if self.clusters >= 65525:
            raise ValueError(f'{self.path} has {self.clusters} clusters, which is FAT32, not FAT16')

    def close(self):
        self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _fat_entry(self, cluster):
        self.handle.seek(self.fat_offset + cluster * 2)
        return struct.unpack('<H', self.handle.read(2))[0]

    def _chain(self, cluster):
        seen = set()
        while 2 <= cluster < 0xFFF8 and cluster not in seen:
            seen.add(cluster)
            yield cluster
            cluster = self._fat_entry(cluster)

    def _read_cluster(self, cluster):
        self.handle.seek(self.data_offset + (cluster - 2) * self.cluster_bytes)
        return self.handle.read(self.cluster_bytes)

    def _read_chain(self, cluster):
        return b''.join(self._read_cluster(item) for item in self._chain(cluster))

    @staticmethod
    def _long_name(entry, previous):
        part = (entry[1:11] + entry[14:26] + entry[28:32]).decode('utf-16le', 'ignore').split('\x00')[0]
        return part + previous

    def _entries(self, data):
        entries, long_name = [], ''
        for offset in range(0, len(data) - 31, 32):
            entry = data[offset:offset + 32]
            if entry[0] == 0:
                break
            if entry[0] == 0xE5:
                long_name = ''
                continue
            attributes = entry[11]
            if attributes == 0x0F:
                long_name = self._long_name(entry, long_name)
                continue
            if attributes & 0x08:      # volume label
                long_name = ''
                continue
            base = entry[0:8].decode('latin1').rstrip()
            extension = entry[8:11].decode('latin1').rstrip()
            short = base + ('.' + extension if extension else '')
            cluster = struct.unpack_from('<H', entry, 26)[0]
            size = struct.unpack_from('<I', entry, 28)[0]
            entries.append((long_name or short, attributes, cluster, size))
            long_name = ''
        return entries

    def root_entries(self):
        self.handle.seek(self.root_offset)
        return self._entries(self.handle.read(self.root_entry_count * 32))

    def _walk(self, entries, prefix, output):
        for name, attributes, cluster, size in entries:
            if name in ('.', '..'):
                continue
            path = prefix + name
            if attributes & 0x10:
                self._walk(self._entries(self._read_chain(cluster)), path + '/', output)
            else:
                output.append((path, cluster, size))

    def files(self):
        output = []
        self._walk(self.root_entries(), '', output)
        return output

    def read(self, cluster, size):
        return self._read_chain(cluster)[:size]

    def read_path(self, wanted):
        wanted = wanted.lower()
        for path, cluster, size in self.files():
            if path.lower() == wanted:
                return self.read(cluster, size)
        raise KeyError(f'{wanted} is not in {self.path}')


def fat_target(fat_name, path):
    """Where a file from NON-HLOS.bin or BTFM.bin goes under OUT, and its proprietary-files.txt key."""
    lowered = path.lower()
    if fat_name == 'NON-HLOS.bin' and lowered.startswith('image/qca6390/'):
        name = Path(path).name
        return Path('modem/qca6390') / name, f'image/qca6390/{name}'
    if fat_name == 'BTFM.bin' and lowered.startswith('image/') and lowered.count('/') == 1:
        name = Path(path).name
        return Path('bt/image') / name, f'image/{name}'
    return None, None


def parse_dtbo(data):
    if len(data) < 32:
        raise ValueError('dtbo.img is smaller than its header')
    magic, total, header_size, entry_size, entry_count, entries_offset, page_size, version = \
        struct.unpack_from('>8I', data, 0)
    if magic != DTBO_MAGIC:
        raise ValueError(f'dtbo.img magic is {magic:#x}, not {DTBO_MAGIC:#x}')
    if entry_size < 16 or header_size < 32 or entries_offset + entry_count * entry_size > len(data):
        raise ValueError('dtbo.img header/entry table is inconsistent with the file size')
    entries = []
    for index in range(entry_count):
        size, offset, ident, revision = struct.unpack_from('>4I', data, entries_offset + index * entry_size)
        if offset + size > len(data):
            raise ValueError(f'dtbo entry {index} runs past the end of the file')
        entries.append({
            'index': index,
            'size': size,
            'offset': offset,
            'id': ident,
            'rev': revision,
            'blob': data[offset:offset + size],
        })
    return {
        'magic': f'{magic:#x}',
        'total_size': total,
        'header_size': header_size,
        'entry_size': entry_size,
        'entry_count': entry_count,
        'entries_offset': entries_offset,
        'page_size': page_size,
        'version': version,
        'entries': entries,
    }


def parse_gpt(data, name):
    """Read a GPT image's partition table, trying the 4K and 512-byte sector layouts."""
    for lba_bytes in (4096, 512):
        if len(data) >= 2 * lba_bytes and data[lba_bytes:lba_bytes + 8] == GPT_MAGIC:
            break
    else:
        raise ValueError(f'{name}: no GPT header at LBA 1 for 512- or 4096-byte sectors')
    header = data[lba_bytes:lba_bytes + 92]
    (magic, revision, header_size, header_crc, reserved, current_lba, backup_lba, first_usable,
     last_usable) = struct.unpack_from('<8sIIIIQQQQ', header, 0)
    entries_lba, entry_count, entry_size, entries_crc = struct.unpack_from('<QIII', header, 72)
    if entry_size < 128 or not 0 < entry_count <= 1024:
        raise ValueError(f'{name}: implausible GPT entry table ({entry_count} x {entry_size})')
    table_offset = entries_lba * lba_bytes
    if table_offset + entry_count * entry_size > len(data):
        raise ValueError(f'{name}: GPT entry table runs past the end of the image')
    table = data[table_offset:table_offset + entry_count * entry_size]
    partitions = []
    for index in range(entry_count):
        entry = table[index * entry_size:(index + 1) * entry_size]
        if entry[:16] == b'\x00' * 16:
            continue
        first, last = struct.unpack_from('<QQ', entry, 32)
        label = entry[56:128].decode('utf-16le', 'ignore').split('\x00')[0]
        partitions.append({
            'index': index,
            'name': label,
            'first_lba': first,
            'last_lba': last,
            'sectors': last - first + 1 if last >= first else 0,
        })
    return {
        'image': name,
        'lba_bytes': lba_bytes,
        'header': {
            'revision': revision,
            'header_size': header_size,
            'header_crc': f'{header_crc:#x}',
            'current_lba': current_lba,
            'backup_lba': backup_lba,
            'first_usable_lba': first_usable,
            'last_usable_lba': last_usable,
            'entries_lba': entries_lba,
            'entry_count': entry_count,
            'entry_size': entry_size,
            'entries_crc': f'{entries_crc:#x}',
        },
        'partitions': partitions,
    }


class Elf:
    """The small part of ELF64 needed to read vmlinux-to-elf's symbol table and LOAD segments."""

    def __init__(self, path):
        self.data = Path(path).read_bytes()
        if self.data[:4] != b'\x7fELF' or self.data[4] != 2 or self.data[5] != 1:
            raise ValueError(f'{path} is not a little-endian ELF64 file')
        program_offset = struct.unpack_from('<Q', self.data, 0x20)[0]
        section_offset = struct.unpack_from('<Q', self.data, 0x28)[0]
        program_size = struct.unpack_from('<H', self.data, 0x36)[0]
        program_count = struct.unpack_from('<H', self.data, 0x38)[0]
        section_size = struct.unpack_from('<H', self.data, 0x3A)[0]
        section_count = struct.unpack_from('<H', self.data, 0x3C)[0]
        self.loads = []
        for index in range(program_count):
            offset = program_offset + index * program_size
            p_type, = struct.unpack_from('<I', self.data, offset)
            p_offset, p_vaddr, p_paddr, p_filesz = struct.unpack_from('<QQQQ', self.data, offset + 8)
            if p_type == 1 and p_filesz:
                self.loads.append((p_vaddr, p_offset, p_filesz))
        if not self.loads:
            raise ValueError(f'{path} has no PT_LOAD segment')

        sections = []
        for index in range(section_count):
            offset = section_offset + index * section_size
            sh_name, sh_type = struct.unpack_from('<II', self.data, offset)
            sh_addr, sh_offset, sh_size = struct.unpack_from('<QQQ', self.data, offset + 16)
            sh_link, = struct.unpack_from('<I', self.data, offset + 40)
            sh_entsize, = struct.unpack_from('<Q', self.data, offset + 56)
            sections.append((sh_name, sh_type, sh_addr, sh_offset, sh_size, sh_link, sh_entsize))
        self.load_vaddr = self.loads[0][0]
        self.symbols = {}
        for sh_name, sh_type, sh_addr, sh_offset, sh_size, sh_link, sh_entsize in sections:
            if sh_type != 2 or sh_link >= len(sections):
                continue
            string_offset = sections[sh_link][3]
            count = sh_size // (sh_entsize or 24)
            for index in range(count):
                offset = sh_offset + index * (sh_entsize or 24)
                st_name, st_info, st_other, st_shndx, st_value, st_size = \
                    struct.unpack_from('<IBBHQQ', self.data, offset)
                if st_name == 0:
                    continue
                end = self.data.find(b'\x00', string_offset + st_name)
                name = self.data[string_offset + st_name:end].decode('latin1')
                self.symbols[name] = st_value

    def va_to_offset(self, address):
        for vaddr, offset, size in self.loads:
            if vaddr <= address < vaddr + size:
                return offset + (address - vaddr)
        raise ValueError(f'{address:#x} is not inside a LOAD segment')

    def cstring(self, address):
        offset = self.va_to_offset(address)
        end = self.data.find(b'\x00', offset)
        return self.data[offset:end].decode('latin1')


def find_vmlinux_to_elf(explicit):
    if explicit:
        return explicit
    found = shutil.which('vmlinux-to-elf')
    if found:
        return found
    raise SystemExit('--touch needs vmlinux-to-elf; install it (pip install vmlinux-to-elf) or pass its path')


def extract_kernel(boot_image):
    """Return (kernel bytes, page size, header version) from an Android boot image."""
    data = Path(boot_image).read_bytes()
    if data[:8] != b'ANDROID!':
        raise ValueError(f'{boot_image} does not start with ANDROID!')
    kernel_size = struct.unpack_from('<I', data, 8)[0]
    header_version = struct.unpack_from('<I', data, 40)[0]
    page_size = 4096
    if header_version in (0, 1, 2):
        page_size = struct.unpack_from('<I', data, 36)[0] or page_size
    if kernel_size == 0 or page_size + kernel_size > len(data):
        raise ValueError(f'{boot_image} kernel_size {kernel_size:#x} does not fit after a {page_size}-byte header')
    return data[page_size:page_size + kernel_size], page_size, header_version


def builtin_firmware(kernel, vte, workdir):
    """Extract the stock kernel's built-in firmware table, using vmlinux-to-elf for the symbol names."""
    kernel_path = workdir / 'kernel'
    elf_path = workdir / 'vmlinux.elf'
    kernel_path.write_bytes(kernel)
    result = subprocess.run([vte, str(kernel_path), str(elf_path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f'vmlinux-to-elf failed:\n{result.stdout}{result.stderr}')
    elf = Elf(elf_path)
    start = elf.symbols.get('__start_builtin_fw')
    end = elf.symbols.get('__end_builtin_fw')
    if start is None or end is None:
        raise SystemExit('the kernel has no __start_builtin_fw/__end_builtin_fw symbols')

    # vmlinux-to-elf maps the raw kernel image at this LOAD address; symbol values are kernel virtual addresses.
    base = elf.load_vaddr
    if not (base <= start < end <= base + len(kernel)):
        raise SystemExit(f'builtin_fw table {start:#x}..{end:#x} is outside the kernel image at {base:#x}')

    count = (end - start) // 24
    sizes = [struct.unpack_from('<QQQ', kernel, (start - base) + index * 24)[2] for index in range(count)]
    names = sorted((value, name) for name, value in elf.symbols.items()
                   if name.startswith('_fw_') and name.endswith('_name'))
    if len(names) != count:
        raise SystemExit(f'{len(names)} _fw_*_name symbols do not match the {count} builtin_fw entries')
    if any(size == 0 or size > len(kernel) for size in sizes):
        raise SystemExit('the builtin_fw table contains an implausible firmware size')

    extracted = []
    for index, (name_address, name_symbol) in enumerate(names):
        name = elf.cstring(name_address)
        data_symbol = name_symbol[:-len('_name')] + '_bin'
        data_address = elf.symbols.get(data_symbol)
        if data_address is None:
            raise SystemExit(f'{name_symbol} has no {data_symbol} data symbol')
        data_offset = data_address - base
        blob = kernel[data_offset:data_offset + sizes[index]]
        if len(blob) != sizes[index]:
            raise SystemExit(f'{name} runs past the end of the kernel image')
        extracted.append({'name': name, 'size': len(blob), 'sha256': sha256_bytes(blob), 'blob': blob})
    return extracted


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--images', required=True, type=Path, help='the stock ROM images/ directory')
    parser.add_argument('--out', type=Path, help='output directory (default: the ROM directory\'s ../extracted)')
    parser.add_argument('--manifest', type=Path, default=MANIFEST, help='proprietary-files.txt to compare against')
    parser.add_argument('--touch', action='store_true', help='extract the built-in touch firmware from boot.img')
    parser.add_argument('--all-dtbo', action='store_true', help='write every dtbo entry, not only the pipa one')
    parser.add_argument('--vmlinux-to-elf', default=os.environ.get('VMLINUX_TO_ELF', ''),
                        help='vmlinux-to-elf executable (env VMLINUX_TO_ELF)')
    args = parser.parse_args()

    if not args.images.is_dir():
        raise SystemExit(f'{args.images} is not a directory')
    out = args.out or (args.images.parent.parent / 'extracted'
                       if args.images.name == 'images' else args.images / 'extracted')
    out.mkdir(parents=True, exist_ok=True)
    recorded = recorded_digests(args.manifest)
    report = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source': {'images': str(args.images.resolve()), 'files': []},
        'fat16': [],
        'dtbo': None,
        'gpt': [],
        'touch': None,
        'manifest': str(args.manifest),
    }
    errors, mismatches = [], []

    for name in STOCK_IMAGES:
        path = args.images / name
        if path.is_file():
            report['source']['files'].append({'name': name, 'size': path.stat().st_size, 'sha256': sha256_file(path)})

    # The modem and Bluetooth partitions are FAT16 images, not filesystem images Android mounts directly.
    for name in ('NON-HLOS.bin', 'BTFM.bin'):
        path = args.images / name
        if not path.is_file():
            errors.append(f'{name} is missing')
            continue
        try:
            with Fat16(path) as fat:
                files = []
                for source_path, cluster, size in fat.files():
                    target, manifest_key = fat_target(name, source_path)
                    if target is None:
                        continue
                    blob = fat.read(cluster, size)
                    target_path = out / target
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_bytes(blob)
                    entry = {
                        'path': source_path,
                        'out': str(target),
                        'manifest': manifest_key,
                        'size': len(blob),
                        'sha256': sha256_bytes(blob),
                    }
                    if manifest_key in recorded:
                        entry['recorded_sha256'] = recorded[manifest_key]
                        entry['matches_recorded'] = recorded[manifest_key] == entry['sha256']
                        if not entry['matches_recorded']:
                            mismatches.append(manifest_key)
                    files.append(entry)
                report['fat16'].append({
                    'image': name,
                    'cluster_bytes': fat.cluster_bytes,
                    'clusters': fat.clusters,
                    'files': files,
                })
                print(f'{name}: FAT16, {fat.clusters} clusters, {len(files)} firmware files extracted')
        except (OSError, ValueError) as error:
            errors.append(f'{name}: {error}')

    # dtbo entries point outside the entry table; each entry is a complete DTB. The pipa model string is unique
    # to the pipa overlay, while panel strings appear in many device overlays.
    dtbo_path = args.images / 'dtbo.img'
    if dtbo_path.is_file():
        try:
            dtbo = parse_dtbo(dtbo_path.read_bytes())
            pipa_entries = [entry for entry in dtbo['entries'] if PIPA_MODEL in entry['blob']]
            report['dtbo'] = {
                'header': {key: value for key, value in dtbo.items() if key != 'entries'},
                'entries': [],
            }
            for entry in dtbo['entries']:
                pipa = entry in pipa_entries
                record = {key: entry[key] for key in ('index', 'size', 'offset', 'id', 'rev')}
                record['pipa'] = pipa
                if pipa or args.all_dtbo:
                    target = out / 'dtbo' / f"{entry['index']:02d}-id{entry['id']:04x}-rev{entry['rev']:04x}.dtb"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(entry['blob'])
                    record['out'] = str(target.relative_to(out))
                    record['sha256'] = sha256_bytes(entry['blob'])
                report['dtbo']['entries'].append(record)
            print(f"dtbo.img: {dtbo['entry_count']} entries, {len(pipa_entries)} with the {PIPA_MODEL.decode()} model")
            if not pipa_entries:
                errors.append('dtbo.img has no entry with the xiaomi pipa model')
        except (OSError, ValueError) as error:
            errors.append(f'dtbo.img: {error}')
    else:
        errors.append('dtbo.img is missing')

    # The stock GPT images are templates: their last LUN may end early, so a zero-length or clipped userdata entry
    # here does not describe a retail unit. Parsing them makes that visible next to the measured live GPT.
    for name in STOCK_IMAGES:
        if not name.startswith('gpt_both'):
            continue
        path = args.images / name
        if not path.is_file():
            continue
        try:
            gpt = parse_gpt(path.read_bytes(), name)
            report['gpt'].append(gpt)
            userdata = next((part for part in gpt['partitions'] if part['name'] == 'userdata'), None)
            if userdata is None:
                print(f'{name}: {gpt["lba_bytes"]}-byte LBAs, {len(gpt["partitions"])} partitions, no userdata')
            else:
                print(f'{name}: {gpt["lba_bytes"]}-byte LBAs, {len(gpt["partitions"])} partitions, '
                      f'userdata {userdata["first_lba"]:#x}..{userdata["last_lba"]:#x} '
                      f'({userdata["sectors"]} sectors)')
        except (OSError, ValueError) as error:
            errors.append(f'{name}: {error}')

    # The NT36532 image lives in the stock kernel's built-in firmware table, not in /vendor.
    boot_path = args.images / 'boot.img'
    if args.touch:
        if not boot_path.is_file():
            errors.append('boot.img is missing, so the built-in touch firmware cannot be read')
        else:
            vte = find_vmlinux_to_elf(args.vmlinux_to_elf)
            kernel, page_size, header_version = extract_kernel(boot_path)
            with tempfile.TemporaryDirectory(prefix='pipa-stock-') as temporary:
                touch = builtin_firmware(kernel, vte, Path(temporary))
            files = []
            for entry in touch:
                target = out / 'touch' / entry['name']
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(entry['blob'])
                manifest_key = f"firmware/{entry['name']}"
                record = {
                    'name': entry['name'],
                    'out': str(target.relative_to(out)),
                    'manifest': manifest_key,
                    'size': entry['size'],
                    'sha256': entry['sha256'],
                }
                if manifest_key in recorded:
                    record['recorded_sha256'] = recorded[manifest_key]
                    record['matches_recorded'] = recorded[manifest_key] == record['sha256']
                    if not record['matches_recorded']:
                        mismatches.append(manifest_key)
                files.append(record)
            report['touch'] = {
                'boot': boot_path.name,
                'boot_sha256': sha256_file(boot_path),
                'page_size': page_size,
                'header_version': header_version,
                'kernel_sha256': sha256_bytes(kernel),
                'kernel_size': len(kernel),
                'files': files,
            }
            print(f'boot.img: kernel {len(kernel)} bytes, {len(files)} built-in firmware images extracted')

    (out / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    with (out / 'manifest.tsv').open('w') as handle:
        handle.write('kind\tpath\tmanifest\tsize\tsha256\trecorded\n')
        for group in report['fat16']:
            for entry in group['files']:
                handle.write(f"{group['image']}\t{entry['path']}\t{entry['manifest']}\t{entry['size']}\t"
                             f"{entry['sha256']}\t{entry.get('recorded_sha256', '-')}\n")
        if report['dtbo']:
            for entry in report['dtbo']['entries']:
                if 'out' in entry:
                    handle.write(f"dtbo\t{entry['out']}\t-\t{entry['size']}\t{entry['sha256']}\t-\n")
        for gpt in report['gpt']:
            for part in gpt['partitions']:
                if part['name'] == 'userdata':
                    handle.write(f"gpt\t{gpt['image']}:userdata\t-\t{part['sectors']}\t-\t-\n")
        if report['touch']:
            for entry in report['touch']['files']:
                handle.write(f"touch\t{entry['out']}\t{entry['manifest']}\t{entry['size']}\t"
                             f"{entry['sha256']}\t{entry.get('recorded_sha256', '-')}\n")

    for key in mismatches:
        errors.append(f'{key}: extracted digest does not match proprietary-files.txt')
    for line in errors:
        print(f'error: {line}')
    print(f"{len(report['source']['files'])} stock images hashed, {len(mismatches)} digest mismatch(es), "
          f"{len(errors)} error(s); wrote {out / 'manifest.json'}")
    return int(bool(errors))


if __name__ == '__main__':
    sys.exit(main())
