#include "iot.h"

#include <stdio.h>
#include <stdlib.h>

bool iot_load_workload(struct iot_workload *workload, const char *path)
{
    FILE *file = fopen(path, "rb");
    if (!file) {
        perror(path);
        return false;
    }
    unsigned char prefix[12];
    bool ok = true;
    for (;;) {
        size_t bytes = fread(prefix, 1, sizeof(prefix), file);
        if (!bytes && feof(file))
            break;
        if (bytes != sizeof(prefix) || workload->count == IOT_MAX_RECORDS) {
            ok = false;
            break;
        }
        uint64_t release = iot_read_u64(prefix);
        uint32_t size = iot_read_u32(prefix + 8);
        if (size < IOT_HEADER || size > IOT_HEADER + IOT_MAX_PAYLOAD || release > 30000000 ||
            (workload->count && release < workload->entries[workload->count - 1].release)) {
            ok = false;
            break;
        }
        struct iot_entry *entry = &workload->entries[workload->count++];
        entry->release = release;
        entry->wire = malloc((size_t)size);
        if (!entry->wire || fread(entry->wire, 1, (size_t)size, file) != size ||
            !iot_decode_record(entry->wire, (size_t)size, &entry->record) ||
            entry->record.id != workload->count || entry->record.generated > release ||
            entry->record.deadline > 120000000) {
            ok = false;
            break;
        }
    }
    fclose(file);
    if (!ok || !workload->count) {
        fprintf(stderr, "Invalid or empty IoT workload %s at record %zu\n", path, workload->count);
        return false;
    }
    return true;
}

void iot_free_workload(struct iot_workload *workload)
{
    for (size_t i = 0; i < workload->count; i++)
        free(workload->entries[i].wire);
}

bool iot_admit(struct iot_workload *workload, uint64_t elapsed, bool replace,
               size_t *index, uint32_t *superseded)
{
    if (workload->next == workload->count || workload->entries[workload->next].release > elapsed)
        return false;

    *index = workload->next++;
    *superseded = 0;
    struct iot_entry *incoming = &workload->entries[*index];
    incoming->state = IOT_PENDING;
    if (!replace || incoming->record.kind == IOT_EVENT)
        return true;

    for (size_t i = 0; i < *index; i++) {
        struct iot_entry *old = &workload->entries[i];
        if (old->state != IOT_PENDING || old->record.kind != IOT_STATE ||
            old->record.source != incoming->record.source)
            continue;
        bool newer = incoming->record.generated > old->record.generated ||
            (incoming->record.generated == old->record.generated &&
             incoming->record.serial > old->record.serial);
        struct iot_entry *drop = newer ? old : incoming;
        drop->state = IOT_SUPERSEDED;
        *superseded = drop->record.id;
        break;
    }
    return true;
}

struct iot_entry *iot_pending(struct iot_workload *workload)
{
    for (size_t i = 0; i < workload->next; i++)
        if (workload->entries[i].state == IOT_PENDING)
            return &workload->entries[i];
    return NULL;
}
