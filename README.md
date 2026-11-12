# UniXNU-pipa

Xiaomi Pad 6 (`pipa`, SM8250-AC / Kona) device support for UniXNU (`iPadOS 17.7.11`, build `21H461`).

Firmware repository: [uefi-pipa](https://github.com/xVanTuring/uefi-pipa) (branch `pipa`)  
Reference port: [xVanTuring/UniXNU-pipa](https://github.com/xVanTuring/UniXNU-pipa)  
Specification: [UniXNU Porting Guide](https://unixnu.com/zh/porting/)

This fork starts from the Lenovo TB-J716F board (same SM8250-AC SoC) and replaces its board-specific parts
with the measured Xiaomi Pad 6 values. The upstream pipa port demonstrated touch, buttons, GPU and Metal on a
16 GB unit; this fork's pipa build has **not** been run on hardware, so every "ported" item below is a build
and source claim until someone boots it.

## Device Specifications

- **Device**: Xiaomi Pad 6 (`pipa`)
- **SoC**: Qualcomm SM8250-AC (Snapdragon 870)
- **CPU**: 8-core Kryo 585 (1x Cortex-A77 prime + 3x Cortex-A77 + 4x Cortex-A55), MPIDR `0x000..0x700`
- **GPU**: Qualcomm Adreno 650 (`0x06050002`)
- **Display**: 11" 1800x2880 panel, UEFI GOP scanout at `0x9c000000`, UI scale 2
- **Touch**: Novatek `NT36532` over QUPv3_0 SE4 GENI SPI (`0x990000`, wrapper `0x9c0000`, 9.6 MHz, IRQ GPIO39)
- **Keys**: PM8150 PON `kpdpwr` / `resin` (power, volume down), PM8150 GPIO6 active-low (volume up)
- **DRAM**: measured 16 GB unit, banks `0x80000000+0x37700000`, `0xc0000000+0x1c0000000`, `0x800000000+0x200000000`
- **Storage**: UFS 3.1 host controller at `0x01d84000` (APFS OS + SystemOS volumes on `userdata`)
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
├── proprietary-files.txt                # Firmware manifest; pipa-only digests still UNMEASURED
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

- `.cache/firmware/gpu/a650_gmu.bin`, `a650_zap.elf` - the vendor `/firmware` partition
- `.cache/firmware/linux-firmware/a650_sqe.fw` - linux-firmware `qcom/a650_sqe.fw`
- `.cache/firmware/touch/novatek_nt36532_m82_fw_tm.bin` - the stock kernel's built-in `_tm` image

`proprietary-files.txt` lists the remaining Wi-Fi/Bluetooth images and marks the pipa-only digests as
`UNMEASURED` until they are extracted and hashed.

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
ROM-specific. The Novatek `NT36532` touch firmware is normally built into the stock kernel, so it comes from
`vendor_boot`/`boot` (via `unpack_bootimg`), not from `/vendor`; the extractor leaves it `UNMEASURED` on
purpose rather than recording a custom-kernel blob as stock. Replace `UNMEASURED` only with digests from this
manifest.

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
- The board file carries the **16 GB** reference unit's memory map, including an 8 GiB bank at 32 GiB. An
  8 GB unit must drop that bank and gets a different `dram_size` and `owned_memory_size`; `--ddr-gb 8` makes
  `scripts/check-pipa-board.py` fail until it does.
- `[userdata]` is a **128 GB** unit's window (114.4 GB). A 256 GB unit needs its own `last_lba` from the
  target GPT, because that window is what the erase step writes. `--storage-gb 256` asserts it.
- `proprietary-files.txt` still has `UNMEASURED` pipa digests and the Adreno 650 digests were recorded on a
  TB-J716F vendor partition.
