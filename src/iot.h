#ifndef IOT_RECORD_H
#define IOT_RECORD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

enum {
    IOT_HEADER = 52,
    IOT_ACK_BYTES = 44,
    IOT_MAX_PAYLOAD = 16384,
    IOT_MAX_RECORDS = 1024,
    IOT_STATE = 1,
    IOT_EVENT = 2,
    IOT_ACK = 3,
    IOT_UPDATED = 1,
    IOT_STALE = 2,
    IOT_ACCEPTED = 3,
    IOT_DUPLICATE = 4,
};

struct iot_record {
    uint32_t id, source, payload_len;
    uint64_t serial, generated, deadline, maximum_age;
    uint8_t kind, priority, severity;
    const unsigned char *payload;
};

struct iot_ack {
    uint32_t id;
    uint64_t stream_id, received, latest_generated, latest_serial;
    uint8_t status;
    bool on_time;
};

uint32_t iot_read_u32(const unsigned char *data);
uint64_t iot_read_u64(const unsigned char *data);
uint64_t iot_payload_hash(const struct iot_record *record);
bool iot_decode_record(const unsigned char *data, size_t size, struct iot_record *record);
size_t iot_encode_record(unsigned char *data, const struct iot_record *record);
bool iot_decode_ack(const unsigned char *data, size_t size, struct iot_ack *ack);
void iot_encode_ack(unsigned char *data, const struct iot_ack *ack);

enum entry_state { IOT_SCHEDULED, IOT_PENDING, IOT_SUPERSEDED, IOT_OBSOLETE, IOT_SUBMITTED, IOT_ACKED };

struct iot_entry {
    struct iot_record record;
    unsigned char *wire;
    uint64_t release, submitted, stream_id;
    enum entry_state state;
};

struct iot_workload {
    struct iot_entry entries[IOT_MAX_RECORDS];
    size_t count, next, outstanding;
};

bool iot_load_workload(struct iot_workload *workload, const char *file);
void iot_free_workload(struct iot_workload *workload);
bool iot_admit(struct iot_workload *workload, uint64_t elapsed, bool replace,
               size_t *index, uint32_t *superseded);
struct iot_entry *iot_pending(struct iot_workload *workload);

struct iot_sensor {
    uint32_t id;
    uint64_t generated, serial;
    unsigned char *payload;
    uint32_t payload_len;
};

struct iot_receiver {
    struct iot_sensor sensors[IOT_MAX_RECORDS];
    struct iot_event_id {
        uint32_t source;
        uint64_t serial;
    } event_ids[IOT_MAX_RECORDS];
    bool record_ids[IOT_MAX_RECORDS + 1];
    struct iot_ack acks[IOT_MAX_RECORDS];
    size_t sensors_count, events_count, count, next;
};

const struct iot_ack *iot_receive(struct iot_receiver *receiver, const struct iot_record *record,
                                  uint64_t stream_id, uint64_t received);
void iot_free_receiver(struct iot_receiver *receiver);

#endif
