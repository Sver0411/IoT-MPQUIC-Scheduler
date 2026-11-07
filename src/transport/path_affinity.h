#ifndef IOT_PATH_AFFINITY_H
#define IOT_PATH_AFFINITY_H

#include <xquic/xquic.h>
#include "scheduler/scheduler.h"

struct iot_binding {
    uint64_t home, due_us;
    uint32_t id;
};

struct iot_path_control {
    bool cross;
    struct iot_binding bindings[IOT_MAX_RECORDS];
};

size_t iot_path_samples(xqc_connection_t *conn, struct iot_path_sample *samples, size_t capacity,
                        double loss_scale);
extern const xqc_scheduler_callback_t iot_affinity_callback;

#endif
