/* LUNA backend ABI. Addresses refer to guest memory; buffers are host-owned. */
#ifndef LISEM_LUNA_H
#define LISEM_LUNA_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define LISEM_LUNA_ABI 1u
typedef struct LisemLuna LisemLuna;
typedef struct LisemLunaHost {
    uint32_t abi;
    void *opaque;
    bool (*read)(void *, uint32_t, void *, size_t);
    bool (*write)(void *, uint32_t, const void *, size_t);
    bool (*sha256)(const void *, size_t, char hex[65]);
    void (*schedule)(void *, uint64_t delay_ns);
    void (*cancel)(void *);
    /* Must terminate execution; the backend cannot recover from this failure. */
    void (*fatal)(void *, const char *);
} LisemLunaHost;
typedef struct LisemLunaStatus {
    uint64_t completed;
    uint32_t api, param;
    bool busy;
} LisemLunaStatus;
/* The callback table is copied. Its opaque state must outlive the backend. */
LisemLuna *lisem_luna_create(const LisemLunaHost *host);
void lisem_luna_destroy(LisemLuna *luna);
void lisem_luna_reset(LisemLuna *luna);
uint64_t lisem_luna_read(LisemLuna *luna, uint64_t offset, unsigned size);
void lisem_luna_write(LisemLuna *luna, uint64_t offset, uint64_t value, unsigned size);
/* Called only by the host at the scheduled virtual deadline. */
void lisem_luna_complete(LisemLuna *luna);
bool lisem_luna_read_safe(uint64_t offset);
LisemLunaStatus lisem_luna_status(const LisemLuna *luna);
#endif
