// SPDX-License-Identifier: Apache-2.0
// Point-report firmware (pipa's NT36532): checksum, slot parsing, recovery patterns, the start of reporting on a small chip
// model, and (with a firmware image) the host CRC against the image header.
#include "../NvtProtocol.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MEM_SIZE 0x200000
static uint8_t mem[MEM_SIZE];
static uint32_t page;
static unsigned reads_of_state;

static int xfer(void *ctx, const uint8_t *tx, uint8_t *rx, size_t len) {
    (void)ctx;
    if (tx[0] == 0xff && len == 3 && !rx) {page = ((uint32_t)tx[1] << 15) | ((uint32_t)tx[2] << 7); return 0;}
    uint32_t addr = page | (tx[0] & 0x7f);
    if (tx[0] & 0x80) {for (size_t i = 1; i < len; ++i) mem[addr + i - 1] = tx[i]; return 0;}
    // A read: [offset, dummy, data...]; the reset state advances once the host has looked at it a few times.
    if (addr == 0x125800 + 0x60 && ++reads_of_state == 3) mem[addr] = NVT_RESET_STATE_REK;
    rx[0] = rx[1] = 0;
    for (size_t i = 2; i < len; ++i) rx[i] = mem[addr + i - 2];
    return 0;
}
static void delay_ms(void *ctx, unsigned ms) {(void)ctx; (void)ms;}

static void seal(uint8_t *d) {
    uint8_t sum = 0;
    for (unsigned i = 0; i < NVT_POINT_CHECKSUM_LEN - 1; ++i) sum = (uint8_t)(sum + d[i]);
    d[NVT_POINT_CHECKSUM_LEN - 1] = (uint8_t)(~sum + 1);
}
// A slot as the stock nvt_ts_work_func reads it: big-endian x and y, the width in byte 5, the pressure at 98 + slot.
static void slot(uint8_t *d, unsigned i, unsigned id, unsigned state, unsigned x, unsigned y, unsigned w, unsigned p) {
    uint8_t *s = d + 6 * i;
    s[0] = (uint8_t)(id << 3 | state); s[1] = (uint8_t)(x >> 8); s[2] = (uint8_t)x;
    s[3] = (uint8_t)(y >> 8); s[4] = (uint8_t)y; s[5] = (uint8_t)w; d[NVT_POINT_PRESSURE + i] = (uint8_t)p;
}
static uint32_t le32(const uint8_t *p) {return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24;}

int main(int argc, char **argv) {
    // The range pipa's firmware reported on the tablet (2026-10-24).
    struct nvt_fw_info info = {.abs_x_max = 18000, .abs_y_max = 28800};
    uint8_t d[NVT_POINT_DATA_LEN] = {0};
    // Two fingers: slot 0 id 1 down at (12345, 23456), slot 3 id 4 moving at (17, 28799) without width or pressure.
    slot(d, 0, 1, 1, 12345, 23456, 9, 0x40);
    slot(d, 3, 4, 2, 17, 28799, 0, 0);
    slot(d, 5, 6, 3, 100, 100, 1, 1);          // state 3 (lifted): skipped
    slot(d, 6, 7, 1, 18000, 10, 1, 1);         // at the range's end: skipped, as the stock driver does
    seal(d);
    assert(nvt_points_checksum_ok(d));
    struct nvt_point p[NVT_POINT_SLOTS];
    unsigned n = nvt_parse_points(d, &info, p, NVT_POINT_SLOTS);
    assert(n == 2);
    assert(p[0].id == 1 && p[0].state == 1 && p[0].x == 12345 && p[0].y == 23456 && p[0].width == 9 && p[0].pressure == 0x40);
    assert(p[1].id == 4 && p[1].state == 2 && p[1].x == 17 && p[1].y == 28799 && p[1].width == 1 && p[1].pressure == 1);
    assert(nvt_parse_points(d, &info, p, 1) == 1);
    // The pressures are outside the checksum.
    d[NVT_POINT_PRESSURE] ^= 1;
    assert(nvt_points_checksum_ok(d));
    d[10] ^= 1;
    assert(!nvt_points_checksum_ok(d));
    assert(!nvt_points_recovery(d));
    memset(d, 0x77, 6); assert(nvt_points_recovery(d) == 1);
    uint8_t wdt[6] = {0xfd, 0xfe, 0xfd, 0xfd, 0xfe, 0xfd}; memcpy(d, wdt, 6); assert(nvt_points_recovery(d) == 2);
    memset(d, 0, sizeof d); seal(d);
    assert(nvt_points_checksum_ok(d) && nvt_parse_points(d, &info, p, NVT_POINT_SLOTS) == 0);

    // nvt_start_reports on the model: the reset state reaches REK on the third look, then the info block is read.
    static uint8_t scratch[NVT_TRANSFER_LEN + 8], rxbuf[NVT_READ_MAX + 2];
    struct nvt_bus bus = {NULL, xfer, delay_ms};
    struct nvt_map map = {.name = "model", .event_buf = 0x125800};
    struct nvt_chip chip = {.bus = &bus, .scratch = scratch, .rxbuf = rxbuf, .map = &map};
    uint8_t *fwinfo = mem + 0x125800 + NVT_EVENT_FWINFO;
    fwinfo[0] = 0x19; fwinfo[1] = 0xe6; fwinfo[2] = 38; fwinfo[3] = 60;
    fwinfo[4] = 18000 >> 8; fwinfo[5] = 18000 & 255; fwinfo[6] = 28800 >> 8; fwinfo[7] = 28800 & 255;
    struct nvt_fw_info got;
    assert(!nvt_start_reports(&chip, &got));
    assert(reads_of_state == 3 && got.fw_ver == 0x19 && got.x_num == 38 && got.y_num == 60);
    assert(got.abs_x_max == 18000 && got.abs_y_max == 28800);
    // Points as the stock driver reads them: 108 bytes from offset 0 of the event buffer.
    slot(mem + 0x125800, 2, 3, 1, 9000, 14400, 5, 7); seal(mem + 0x125800);
    assert(!nvt_read_points(&chip, d) && nvt_points_checksum_ok(d));
    assert(nvt_parse_points(d, &got, p, NVT_POINT_SLOTS) == 1 && p[0].id == 3 && p[0].x == 9000 && p[0].y == 14400);
    assert(p[0].width == 5 && p[0].pressure == 7);
    // A state that never comes: a timeout, with the info still read.
    memset(mem + 0x125800 + 0x60, 0, 5); reads_of_state = 100;
    assert(nvt_start_reports(&chip, &got) == NVT_ERR_TIMEOUT && got.abs_x_max == 18000);

    const char *path = argc > 1 ? argv[1] : getenv("NVT_FIRMWARE");
    if (!path || !*path) {puts("PASS: point reports (checksum, slots, recovery, start of reporting on a model); no firmware given"); return 0;}
    FILE *f = fopen(path, "rb");
    if (!f) {puts("PASS: point reports; firmware file not found, CRC check skipped"); return 0;}
    fseek(f, 0, SEEK_END); size_t size = (size_t)ftell(f); fseek(f, 0, SEEK_SET);
    uint8_t *fw = malloc(size); assert(fw && fread(fw, 1, size, f) == size); fclose(f);
    struct nvt_fw img;
    assert(!nvt_fw_parse(&img, fw, size));
    // The image carries the bootloader's golden checksums of ILM and DLM at 0x18 and 0x1c (the stock nvt_bin_header_parser reads
    // them there when the chip has a hardware CRC); the host computation must give the same words.
    printf("ILM crc %08x header %08x, DLM crc %08x header %08x\n", img.part[0].crc, le32(fw + 0x18), img.part[1].crc, le32(fw + 0x1c));
    assert(img.part[0].crc == le32(fw + 0x18) && img.part[1].crc == le32(fw + 0x1c));
    // The version byte and its complement 4 KiB before the end (update_firmware_request's check).
    assert((uint8_t)(fw[size - 4096] + fw[size - 4095]) == 0xff);
    free(fw);
    puts("PASS: point reports (checksum, slots, recovery, start of reporting on a model) and the firmware's header CRCs");
    return 0;
}
