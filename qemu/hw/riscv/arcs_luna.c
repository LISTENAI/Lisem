/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "crypto/hash.h"
#include "qemu/error-report.h"

static bool read_memory(void *opaque, uint32_t address, void *data, size_t size)
{
    return address_space_read(&address_space_memory, address,
                              MEMTXATTRS_UNSPECIFIED, data, size) == MEMTX_OK;
}

static bool write_memory(void *opaque, uint32_t address, const void *data, size_t size)
{
    return address_space_write(&address_space_memory, address,
                               MEMTXATTRS_UNSPECIFIED, data, size) == MEMTX_OK;
}

static bool sha256(const void *data, size_t size, char hex[65])
{
    g_autofree char *digest = NULL;
    if (qcrypto_hash_digest(QCRYPTO_HASH_ALGO_SHA256, data, size, &digest, NULL) < 0) {
        return false;
    }
    memcpy(hex, digest, 65);
    return true;
}

static void schedule(void *opaque, uint64_t delay_ns)
{
    ArcsLUNA *s = opaque;
    timer_mod(s->completion, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + delay_ns);
}

static void cancel(void *opaque)
{
    ArcsLUNA *s = opaque;
    timer_del(s->completion);
}

static G_NORETURN void fatal(void *opaque, const char *message)
{
    ArcsLUNA *s = opaque;
    error_report("%s", message);
    s->soc->report(s->soc->report_opaque, "unsupported-luna");
    exit(1);
}

static void complete(void *opaque)
{
    ArcsLUNA *s = opaque;
    lisem_luna_complete(s->backend);
}

static uint64_t read_reg(void *opaque, hwaddr offset, unsigned size)
{
    ArcsLUNA *s = opaque;
    return lisem_luna_read(s->backend, offset, size);
}

static void write_reg(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    ArcsLUNA *s = opaque;
    lisem_luna_write(s->backend, offset, value, size);
}

static bool read_safe(void *opaque, hwaddr offset)
{
    ArcsLUNA *s = opaque;
    return s->safe_reads && lisem_luna_read_safe(offset);
}

static const MemoryRegionOps ops = {
    .read = read_reg, .write = write_reg, .endianness = DEVICE_LITTLE_ENDIAN,
    .icount_read_safe = read_safe,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_luna_init(ArcsSoC *soc)
{
    ArcsLUNA *s = &soc->luna;
    s->soc = soc;
    s->safe_reads = g_strcmp0(getenv("ARCS_QEMU_LUNA_SAFE_READS"), "1") == 0;
    s->completion = timer_new_ns(QEMU_CLOCK_VIRTUAL, complete, s);
    const LisemLunaHost host = {
        .abi = LISEM_LUNA_ABI, .opaque = s,
        .read = read_memory, .write = write_memory, .sha256 = sha256,
        .schedule = schedule, .cancel = cancel, .fatal = fatal,
    };
    s->backend = lisem_luna_create(&host);
    if (!s->backend) { fatal(s, "LUNA backend ABI mismatch"); }
    memory_region_init_io(&s->io, OBJECT(soc), &ops, s, "arcs-luna", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x49000000, &s->io);
}

void arcs_luna_reset(ArcsSoC *soc)
{
    lisem_luna_reset(soc->luna.backend);
}
