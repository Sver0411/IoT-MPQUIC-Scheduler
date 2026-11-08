#ifndef IOT_PATH_AFFINITY_H
#define IOT_PATH_AFFINITY_H

#include <xquic/xquic.h>
#include "scheduler/scheduler.h"

struct iot_binding {
    uint64_t home, due_us;
    uint32_t id;
    double base_estimate_us;
};

struct iot_path_control {
    bool cross, integrity, profile;
    uint64_t decisions;
    struct iot_binding bindings[IOT_MAX_RECORDS];
};

void iot_trace_event(const char *name, const char *format, ...);
uint64_t iot_profile_clock(void);
size_t iot_path_samples(xqc_connection_t *conn, struct iot_path_sample *samples, size_t capacity,
                        double loss_scale, bool features);
extern const xqc_scheduler_callback_t iot_affinity_callback;

#endif
