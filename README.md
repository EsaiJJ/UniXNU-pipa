# UniXNU-pipa

Xiaomi Pad 6 (`pipa`, SM8250-AC / Kona) device support for UniXNU (`iPadOS 17.7.11`, build `21H461`).

Firmware repository: [uefi-pipa](https://github.com/xVanTuring/uefi-pipa) (branch `pipa`)  
Reference port: [xVanTuring/UniXNU-pipa](https://github.com/xVanTuring/UniXNU-pipa)  
Specification: [UniXNU Porting Guide](https://unixnu.com/zh/porting/)

This fork starts from the Lenovo TB-J716F board (same SM8250-AC SoC) and replaces its board-specific parts
with the measured Xiaomi Pad 6 values. The upstream pipa port demonstrated touch, buttons, GPU and Metal on a
16 GB unit; this fork targets an 8 GB / 256 GB unit and has read its memory map and GPT from the tablet, but the
pipa build has **not** been run on hardware, so every "ported" item below is a build and source claim until
someone boots it.

## Device Specifications

- **Device**: Xiaomi Pad 6 (`pipa`)
- **SoC**: Qualcomm SM8250-AC (Snapdragon 870)
- **CPU**: 8-core Kryo 585 (1x Cortex-A77 prime + 3x Cortex-A77 + 4x Cortex-A55), MPIDR `0x000..0x700`
- **GPU**: Qualcomm Adreno 650 (`0x06050002`)
- **Display**: 11" 1800x2880 panel `m82 42 02 0b` (display-maker `0x42`), UEFI GOP scanout at `0x9c000000`, UI scale 2
- **Touch**: Novatek `NT36532` over QUPv3_0 SE4 GENI SPI (`0x990000`, wrapper `0x9c0000`, 9.6 MHz, IRQ GPIO39);
  the live DT's `cfg_0` (display-maker `0x42`) and the stock kernel's built-in firmware table both name
  `novatek_nt36532_m82_fw_csot.bin`
- **Keys**: PM8150 PON `kpdpwr` / `resin` (power, volume down), PM8150 GPIO6 active-low (volume up)
- **DRAM**: measured 8 GB target, banks `0x80000000+0x3bb00000`, `0xc0000000+0x1c0000000`, span `0x200000000`;
  the 16 GB reference unit also carried `0x800000000+0x200000000`
- **Storage**: UFS 3.1 host controller at `0x01d84000` (APFS OS + SystemOS volumes on `userdata`);
  target `userdata` window `0x293000..0x3b037fa` (242.4 GB)
- **Battery**: PM8150B fuel gauge + SMB5 charger, 8840 mAh design capacity
- **Wi-Fi / Bluetooth**: QCA6390 (PCIe0 + QUPv3_0 SE6 GENI UART), carried over and unverified
- **Target OS**: iPadOS 17.7.11 (build `21H461`, device class `j120ap`)

## Hardware Support Status

| Subsystem | Status | Notes |
| --- | --- | --- |
| Board configuration | Ported | `board.toml` (schema 2), `configs/pipa_defconfig`, `dts/pipa.dts`, pipa DRAM/userdata/boot-image values |
| Boot & XNU kernel | Ported, not booted here | `uefi-pipa` -> DarwinBoot -> XNU ARM64 + IOKit; static checks and archive builds only |
| CPU & SMP | Ported, not booted here | 8 cores, GICv3, PSCI SMC, 19.2 MHz timer, `pipa-cpufreq=1` |
| Storage | Ported, not booted here | `ShengUfs` with the SM8250 UFS host and the pipa APFS `userdata` window |
| GPU | Ported, not booted here | Adreno 650 KMD (`pipa-gpu=12`), GMU/SQE/zap firmware, Turnip `nocb` defaults from the upstream pipa port |
| Display | Partial | GOP framebuffer at `0x9c000000`, scale 2, GPU scanout copy; **no backlight driver yet** |
| Touch & buttons | Ported, not booted here | Novatek `NT36532` (`pipa-touch=5`, `NvtProtocol` + `GeniSpi`) and PM8150 PON keys (`pipa-buttons=1`) |
| Battery gauge | Ported, not booted here | PM8150B FG / SMB5 with the 8840 mAh pipa profile |
| Wi-Fi & Bluetooth | Carried over, default-off | QCA6390 code is present but its boot arguments are not set; pin/rail assumptions are unverified on pipa |
| USB diagnostics & native USB | Carried over, default-off | DWC3 CDC-ACM (`pipa-usb=1`) and `AppleUSBXDCI` handoff, unverified on pipa |
| Backlight / brightness | Missing | KTZ8866 over I2C11 is not ported; `sheng-backlight` must stay unset |
| Audio | Planned | No ADSP/WCD938x path yet |

## Repository Layout

```text
UniXNU-pipa/
├── board.toml                           # Format 2 board specification
├── porting.toml                         # Porting record (implementation status, not device verification)
├── feeds.toml                           # Shared dependencies and upstream commits
├── proprietary-files.txt                # Firmware digests and stock-ROM provenance; blobs stay outside Git
├── firmware/
│   └── README.md                        # Where the extracted blobs go and how they were identified
├── configs/
│   └── pipa_defconfig                   # Kernel build configuration
├── dts/
│   └── pipa.dts                         # Fallback board tree (uefi-pipa's pipa.dtb is preferred)
├── drivers/
│   ├── Adreno650/                       # SM8250 Adreno 650 kernel driver
│   ├── AdrenoMetal/                     # Metal-to-Turnip userspace bridge
│   ├── ArmCpu/                          # ARM64 SMP & interrupt controller driver
│   ├── GopFramebuffer/                  # Display framebuffer & scanout hand-off
│   ├── QcomGeniSpi/                     # GENI SPI transport for the touch controller
│   ├── ShengHidInput/                   # Multi-touch & button HID injection
│   ├── ShengNovatekTouch/               # NT36532 protocol and point-report core
│   └── ShengUfs/                        # SM8250 UFS 3.1 host controller driver
├── kits/
│   └── ArmSmmu4K/                       # Qualcomm ARM SMMU v2 4K context bank kit
├── package/                             # Board probes (GPU, touch, buttons, battery, USB, ramoops)
├── patches/
│   └── xnu/                             # Board-local XNU patch series
└── scripts/
    ├── build-adreno650.py               # libAdreno650.a
    ├── build-pipa-gpu-addon.py          # NativeAdrenoMetal bundle, trust bundle, NGPUADD2 addon
    ├── build-gop-legacy-display.py      # libNativeLegacyDisplay.late.a (display, touch, USB, battery)
    ├── check-pipa.py                    # Firmware preflight and boot DTB staging
    ├── check-pipa-board.py              # board.toml / DTS / firmware-manifest and SKU checks
    ├── extract-pipa-firmware.py         # Host-side identity + firmware pull and SHA-256 manifest
    ├── extract-stock-firmware.py        # Stock fastboot ROM: FAT16, dtbo, boot built-in firmware, digests
    ├── read-pipa-log.py                 # Ramoops reader over `fastboot boot`
    └── package.sh                       # UniXNU release wrapper (do not edit)
```

## Building

The firmware comes from `uefi-pipa`; place both checkouts next to a `UniXNU` checkout:

```sh
git clone -b pipa https://github.com/xVanTuring/uefi-pipa.git
git clone https://github.com/UniXNU/UniXNU.git

cd UniXNU
./unixnu defconfig pipa_defconfig -D ../UniXNU-pipa
python3 ../UniXNU-pipa/scripts/check-pipa.py
bash ../UniXNU-pipa/scripts/package.sh --no-deps --uefi ../uefi-pipa
```

`check-pipa.py` stages `build/pipa/pipa.dtb`: it copies `uefi-pipa/Resources/DTBs/pipa.dtb` when that checkout
is present and otherwise compiles `dts/pipa.dts`. It also checks the pipa platform files, the UEFI memory
descriptors and the firmware inputs, and records `hardware_boot_demonstrated: false`.

The archive builders need a few firmware files outside Git (paths are overridable through `PIPA_*` variables):

- `.cache/firmware/gpu/a650_gmu.bin`, `a650_sqe.fw`, `a650_zap.elf` - the pipa `/vendor/firmware` partition
  (read through the ported ColorOS ROM; the builder also accepts linux-firmware `qcom/a650_sqe.fw` as a fallback)
- `.cache/firmware/touch/novatek_nt36532_m82_fw_csot.bin` - the stock kernel's built-in image for panel `m82 42 02 0b`

`proprietary-files.txt` records the measured digests and their provenance (official stock image or live
partition). The binaries themselves stay outside Git; either extractor below writes them to a private directory.

## Extracting Firmware From a Stock ROM

An official fastboot ROM is the preferred source: its digests are reproducible and it carries the `dtbo` and
`vendor_boot` the UEFI build expects. Unpack the `.tgz`, then point the host-side extractor at its `images/`
directory:

```sh
python3 scripts/extract-stock-firmware.py --images ~/pipa-firmware/stock/<rom>/images --touch
```

It hashes every stock image and then reads the files the way the tablet's boot chain does:

- `NON-HLOS.bin` and `BTFM.bin` are FAT16 filesystems (not ext4 or sparse images); it walks them and extracts
  `image/qca6390/*` and the four `image/ht*` Bluetooth files;
- `dtbo.img` uses the big-endian AOSP header and 32-byte entries; it extracts the one entry whose DT model says
  `xiaomi pipa` (entry 16, board-id `0x34`, both m82 panels and `novatek@0`), and `--all-dtbo` writes all 29;
- the `gpt_both*.bin` templates are parsed per LUN; the stock LUN 0 template ends `userdata` at `0x293000`
  (`last_lba` `0x292fff`, zero sectors), which is why the live `sda34` window is the authority;
- `--touch` extracts the kernel from `boot.img` and reads its built-in firmware table. It runs
  `vmlinux-to-elf` for the symbol names (set `VMLINUX_TO_ELF` or `--vmlinux-to-elf` if it is in a virtualenv),
  then pairs each `_fw_*_name` with its `_fw_*_bin_bin` and the size in `__start_builtin_fw`. This is the only
  stock source for the NT36532 image, which is compiled into the kernel rather than stored in `/vendor`.

The run writes `manifest.json`/`manifest.tsv` and compares every digest with `proprietary-files.txt`, so a
mismatch is visible before anything is committed. The extracted blobs belong outside this checkout.

## Extracting Firmware From the Tablet

The pipa-only files cannot be taken from another device or guessed. They are measured on the tablet itself,
with Android booted and visible to `adb` — not in fastboot, which only exposes whole partitions:

```sh
python3 scripts/extract-pipa-firmware.py --out ~/pipa-firmware
```

It refuses to run unless `ro.product.device` is `pipa`, records `ro.build.fingerprint` and the related
properties as provenance, pulls `/vendor/firmware`, `/vendor/firmware_mnt/image`, `/vendor/bt_firmware/image`
and `/vendor/etc/wifi`, copies `/sys/firmware/fdt` when readable, and writes `manifest.json`/`manifest.tsv`
with a SHA-256 per file. It also flags any pulled Adreno 650 file that disagrees with the digests already
recorded here.

A stock ROM is the preferred source: it gives reproducible digests and the `dtbo`/`vendor_boot` the UEFI build
expects. If the tablet is on a custom ROM, keep its fingerprint in the manifest and treat the results as
ROM-specific. `--provisional` records that distinction in the manifest.

The read-only hardware pass needs no root, no fastboot and changes nothing on the tablet. It works the same on
a stock or a custom ROM:

```sh
python3 scripts/extract-pipa-firmware.py --skip-pull --provisional
```

It prints and stores in `manifest.json` the values that identify the unit:

- every `/memory` range from `/proc/device-tree`, plus `suggested_adt` (`dram_base`, `dram_size`); the target's
  two 8 GB ranges and `0x200000000` span are what `board.toml` and `dts/pipa.dts` now carry;
- `MemTotal` and the `userdata` start/size from sysfs, which is how the 256 GB GPT window was measured;
- panel name/model, multitouch input device, battery design capacity and display size, which identify the
  panel and touch variant and stay valid regardless of ROM;
- the kernel version and the DT `model`/`compatible`, so a custom kernel or spoofed build props are not later
  mistaken for stock.

On a ported ROM, the RAM and storage geometry are hardware and can be used directly. The `/vendor` digests and
kernel version belong to that ROM, so do not copy them into `proprietary-files.txt` as stock values.

The Novatek `NT36532` touch firmware is built into the stock kernel, not stored in `/vendor`, so the adb
extractor deliberately does not record a custom-kernel blob as stock; use the stock-ROM extractor's `--touch`
path for it. The panel is `m82 42 02 0b`, whose display-maker `0x42` selects the CSOT image; the `_tm` image
belongs to the `0x36` panel variant.

## Boot Arguments

The complete list is `[payload].kernel_args` in `board.toml`; most entries there are shared UniXNU and
offline-render flags. The switches below are the pipa-specific ones that decide what the port does:

`board.toml` sets the pipa bring-up set:

- `pipa-touch=5` - Novatek report loop on the attention line
- `pipa-buttons=1` - PM8150 PON keys
- `pipa-cpufreq=1`, `pipa-gpu=12`, `sheng-gpu-addon=1`, `sheng-fb-gpu=1`
- `native-ufs-root=1`, `sheng-data-persist=2`, `sheng-pmgr=2`
- `pipa-restart-after=240`, `pipa-panic-restart-after=10`, `pipa-restart-crashes=3`

Deliberately **not** set until each is validated on pipa:

- `sheng-backlight=1` - the KTZ8866 controller is not implemented
- `sheng-wifi=4`, `sheng-bluetooth=1` - QCA6390 rails/pins need hardware confirmation
- `pipa-usb=1` - USB CDC-ACM and the native `AppleUSBXDCI` handoff are unverified here

## Diagnostics

With `pipa-usb=1` the tablet enumerates as a CDC-ACM port (`1209:0001`) and accepts `status`, `display`,
`gpu.status`, `battery.status`, `screen.snapshot` and `reboot.fastboot`. The ramoops console is at
`0xb0000000`; `package/linux-log` and `scripts/read-pipa-log.py` read it back.

## Known Gaps

- No hardware run of this fork. The reference port's results do not transfer automatically to a different
  UEFI build, firmware manifest or `board.toml`.
- The KTZ8866 backlight, QCA6390 Wi-Fi/Bluetooth, and native USB paths are absent or default-off.
- The DRAM map and `[userdata]` window were measured on the target 8 GB / 256 GB unit through its booted
  ColorOS port: DT `model` is `Qualcomm Technologies, Inc. xiaomi pipa`, banks sum to 7.93 GiB and `userdata`
  is 242.4 GB. The stock ROM confirms the same panel/DT strings, but its template `gpt_both0.bin` ends
  `userdata` at the LUN boundary (`0x293000`), so the live `sda34` geometry is the authority. The port's kernel
  is custom (`4.19.246-N0`), so a stock boot is still the final confirmation. `--ddr-gb 8 --storage-gb 256`
  asserts these values.
- `owned_memory_size` (`0x170000000`) is still the 16 GB reference unit's DarwinBoot window. It fits inside the
  8 GB span on paper, but only the first UEFI boot can confirm the allocation.
- The firmware digests are measured: the QCA6390/BTFM and touch images come from the official stock ROM, while
  the Adreno 650 and WCNSS config were read from the live `/vendor` (not yet cross-checked against `super.img`).
  `a650_zap.mdt`, `.b01` and `.elf` are pipa-signed and differ from the TB-J716F images the fork originally
  carried; use the values in `proprietary-files.txt` for hardware runs.
