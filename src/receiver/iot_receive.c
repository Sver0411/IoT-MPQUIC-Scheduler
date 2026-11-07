#include "iot.h"

#include <stdlib.h>
#include <string.h>

const struct iot_ack *iot_receive(struct iot_receiver *receiver, const struct iot_record *record,
                                  uint64_t stream_id, uint64_t received)
{
    if (!record->id || record->id > IOT_MAX_RECORDS || receiver->record_ids[record->id] ||
        receiver->count == IOT_MAX_RECORDS || received < record->generated)
        return NULL;

    struct iot_ack ack = {
        .id = record->id, .stream_id = stream_id, .received = received,
        .on_time = received <= record->deadline,
    };
    if (record->kind == IOT_STATE) {
        size_t i = 0;
        while (i < receiver->sensors_count && receiver->sensors[i].id != record->source)
            i++;
        bool first = i == receiver->sensors_count;
        struct iot_sensor *sensor = &receiver->sensors[i];
        ack.status = IOT_STALE;
        if (first || record->generated > sensor->generated ||
            (record->generated == sensor->generated && record->serial > sensor->serial)) {
            unsigned char *payload = NULL;
            if (record->payload_len) {
                payload = malloc(record->payload_len);
                if (!payload)
                    return NULL;
                memcpy(payload, record->payload, record->payload_len);
            }
            free(sensor->payload);
            sensor->payload = payload;
            sensor->payload_len = record->payload_len;
            sensor->generated = record->generated;
            sensor->serial = record->serial;
            if (first) {
                sensor->id = record->source;
                receiver->sensors_count++;
            }
            ack.status = IOT_UPDATED;
        }
        ack.latest_generated = sensor->generated;
        ack.latest_serial = sensor->serial;
    } else {
        size_t i = 0;
        while (i < receiver->events_count && (receiver->event_ids[i].source != record->source ||
                                               receiver->event_ids[i].serial != record->serial))
            i++;
        ack.status = IOT_DUPLICATE;
        if (i == receiver->events_count) {
            receiver->event_ids[receiver->events_count++] = (struct iot_event_id){record->source, record->serial};
            ack.status = IOT_ACCEPTED;
        }
    }
    receiver->record_ids[record->id] = true;
    receiver->acks[receiver->count] = ack;
    return &receiver->acks[receiver->count++];
}

void iot_free_receiver(struct iot_receiver *receiver)
{
    for (size_t i = 0; i < receiver->sensors_count; i++)
        free(receiver->sensors[i].payload);
}
