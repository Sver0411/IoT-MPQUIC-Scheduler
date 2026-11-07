#include "iot.h"

#include <arpa/inet.h>
#include <string.h>

uint32_t iot_read_u32(const unsigned char *data)
{
    uint32_t value;
    memcpy(&value, data, sizeof(value));
    return ntohl(value);
}

static void write_u32(unsigned char *data, uint32_t value)
{
    value = htonl(value);
    memcpy(data, &value, sizeof(value));
}

uint64_t iot_read_u64(const unsigned char *data)
{
    return ((uint64_t)iot_read_u32(data) << 32) | iot_read_u32(data + 4);
}

static void write_u64(unsigned char *data, uint64_t value)
{
    write_u32(data, (uint32_t)(value >> 32));
    write_u32(data + 4, (uint32_t)value);
}

uint64_t iot_payload_hash(const struct iot_record *record)
{
    uint64_t hash = UINT64_C(14695981039346656037);
    for (size_t i = 0; i < record->payload_len; i++)
        hash = (hash ^ record->payload[i]) * UINT64_C(1099511628211);
    return hash;
}

bool iot_decode_record(const unsigned char *data, size_t size, struct iot_record *record)
{
    if (size < IOT_HEADER || memcmp(data, "IOT1", 4) || data[7])
        return false;
    *record = (struct iot_record){
        .kind = data[4], .priority = data[5], .severity = data[6],
        .payload_len = iot_read_u32(data + 8), .id = iot_read_u32(data + 12),
        .source = iot_read_u32(data + 16), .serial = iot_read_u64(data + 20),
        .generated = iot_read_u64(data + 28), .deadline = iot_read_u64(data + 36),
        .maximum_age = iot_read_u64(data + 44), .payload = data + IOT_HEADER,
    };
    if (record->payload_len > IOT_MAX_PAYLOAD || size != IOT_HEADER + record->payload_len ||
        !record->id || record->id > IOT_MAX_RECORDS || !record->source || !record->serial ||
        record->deadline < record->generated)
        return false;
    if (record->kind == IOT_STATE)
        return !record->severity && record->maximum_age > 0;
    return record->kind == IOT_EVENT && record->severity > 0 && !record->maximum_age;
}

size_t iot_encode_record(unsigned char *data, const struct iot_record *record)
{
    memcpy(data, "IOT1", 4);
    data[4] = record->kind;
    data[5] = record->priority;
    data[6] = record->severity;
    data[7] = 0;
    write_u32(data + 8, record->payload_len);
    write_u32(data + 12, record->id);
    write_u32(data + 16, record->source);
    write_u64(data + 20, record->serial);
    write_u64(data + 28, record->generated);
    write_u64(data + 36, record->deadline);
    write_u64(data + 44, record->maximum_age);
    memcpy(data + IOT_HEADER, record->payload, record->payload_len);
    return IOT_HEADER + record->payload_len;
}

bool iot_decode_ack(const unsigned char *data, size_t size, struct iot_ack *ack)
{
    if (size != IOT_ACK_BYTES || memcmp(data, "IOT1", 4) || data[4] != IOT_ACK ||
        data[5] < IOT_UPDATED || data[5] > IOT_DUPLICATE || data[6] > 1 || data[7])
        return false;
    *ack = (struct iot_ack){
        .status = data[5], .on_time = data[6], .id = iot_read_u32(data + 8),
        .stream_id = iot_read_u64(data + 12), .received = iot_read_u64(data + 20),
        .latest_generated = iot_read_u64(data + 28), .latest_serial = iot_read_u64(data + 36),
    };
    return true;
}

void iot_encode_ack(unsigned char *data, const struct iot_ack *ack)
{
    memcpy(data, "IOT1", 4);
    data[4] = IOT_ACK;
    data[5] = ack->status;
    data[6] = ack->on_time;
    data[7] = 0;
    write_u32(data + 8, ack->id);
    write_u64(data + 12, ack->stream_id);
    write_u64(data + 20, ack->received);
    write_u64(data + 28, ack->latest_generated);
    write_u64(data + 36, ack->latest_serial);
}
