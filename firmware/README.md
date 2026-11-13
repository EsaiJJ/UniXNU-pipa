# Firmware inputs

This directory documents the private firmware the port needs. The binaries themselves are **not** in this
repository: official Xiaomi firmware must not be republished, and the device-signed Adreno ZAP images are
per-unit. Only digests and provenance live in `../proprietary-files.txt`.

The archive builders look for the blobs under `.cache/firmware/` (overridable with the `PIPA_*` environment
variables):

| Build input | Path |
| --- | --- |
| Adreno 650 GMU / SQE / ZAP | `.cache/firmware/gpu/a650_gmu.bin`, `a650_sqe.fw`, `a650_zap.elf` |
| Novatek NT36532 touch | `.cache/firmware/touch/novatek_nt36532_m82_fw_csot.bin` |
| QCA6390 Wi-Fi / Bluetooth | `.cache/firmware/` (default-off; see the manifest) |

## Where each file comes from

The measured unit is the global Xiaomi Pad 6, 8 GB / 256 GB, panel `m82 42 02 0b` (display-maker `0x42`).
The stock reference is the fastboot ROM `pipa_global_images_OS2.0.16.0.UMZMIXM_14.0` (HyperOS 2.0.16,
Android 14):

- `NON-HLOS.bin` and `BTFM.bin` are FAT16 filesystems. `image/qca6390/amss20.bin`, `m3.bin` and `bdwlan.elf`
  come from the first; `image/htbtfw20.tlv` and `image/htnv20.bin` come from the second.
- The Novatek image is compiled into the stock `boot.img` kernel's built-in firmware table. The live touch DT
  has two configs: `cfg_0`, display-maker `0x42`, names the CSOT image, and `cfg_1`, display-maker `0x36`, names
  the `_tm` image. This unit's panel is `m82 42 02 0b`, so use the CSOT file; the Himax image in the same table
  is for the other panel supplier and is not used.
- The Adreno 650 files come from the tablet's live `/vendor/firmware` (read through the ported ColorOS ROM, not
  yet cross-checked against the stock `super.img`). `a650_gmu.bin`, `a650_sqe.fw` and `a650_zap.b00`/`.b02` match
  the TB-J716F reference port; `a650_zap.mdt`, `.b01` and `.elf` are device-signed and differ, so only the pipa
  digests are correct for hardware.
- `WCNSS_qcom_cfg_qca6390.ini` also comes from the live `/vendor/etc/wifi/qca6390`; its stock copy lives inside
  `super.img`, which this extraction does not unpack. The Bluetooth images are byte-identical on both sources.

## Reproducing the digests

From an unpacked stock ROM:

```sh
python3 ../scripts/extract-stock-firmware.py --images <rom>/images --touch
```

This writes the extracted tree plus `manifest.json`/`manifest.tsv`, and compares every digest with
`../proprietary-files.txt`. It needs no tablet. `--touch` requires `vmlinux-to-elf` to recover the kernel's
symbol names. The extracted tree is private; keep it out of the checkout.
