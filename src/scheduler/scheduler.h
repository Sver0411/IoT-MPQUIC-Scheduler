#ifndef IOT_SCHEDULER_H
#define IOT_SCHEDULER_H

#include "iot.h"

enum iot_policy {
    IOT_FIFO_REPLACE,
    IOT_FIFO,
    IOT_PRIORITY,
    IOT_EDF,
    IOT_AOI,
    IOT_LATEST_RR,
    IOT_PROPOSED_STRICT,
    IOT_PROPOSED_CROSS,
};

enum iot_ablation {
    IOT_FULL, IOT_NO_REPLACE, IOT_NO_EVENT, IOT_NO_AOI, IOT_FIXED_PATH, IOT_NO_RECOVERY, IOT_NO_CROSS,
};

struct iot_feedback {
    uint32_t sensor;
    uint64_t generated, serial;
};

struct iot_scheduler {
    enum iot_policy policy;
    enum iot_ablation ablation;
    uint32_t last_sensor;
    bool prefer_event;
    struct iot_feedback feedback[IOT_MAX_RECORDS];
    size_t feedback_count;
    unsigned recovery_k, normal_since_recovery, event_streak;
};

enum iot_class { IOT_NO_RECORD, IOT_NORMAL, IOT_RECOVERY, IOT_URGENT };

struct iot_path_sample {
    uint64_t id, rtt_us, rttvar_us, rate_Bps, queued_bytes;
    double loss;
    unsigned pto;
};

struct iot_estimate {
    double duration_us, probability;
};

struct iot_plan {
    struct iot_entry *entry;
    enum iot_class category;
    uint64_t path_id;
    struct iot_estimate estimate;
    double score;
};

enum { IOT_EVENT_BURST = 3, IOT_DEFAULT_RECOVERY_K = 2, IOT_MAX_RECOVERY_K = 64 };

const char *iot_policy_name(enum iot_policy policy);
bool iot_parse_policy(const char *name, enum iot_policy *policy);
const char *iot_ablation_name(enum iot_ablation ablation);
bool iot_parse_ablation(const char *name, enum iot_ablation *ablation);
bool iot_policy_replaces(enum iot_policy policy);
bool iot_policy_proposed(enum iot_policy policy);
void iot_schedule_feedback(struct iot_scheduler *scheduler, uint32_t sensor, uint64_t generated,
                           uint64_t serial);
const struct iot_feedback *iot_latest_feedback(const struct iot_scheduler *scheduler, uint32_t sensor);
struct iot_entry *iot_select(const struct iot_scheduler *scheduler, struct iot_workload *workload);
void iot_schedule_commit(struct iot_scheduler *scheduler, const struct iot_record *record);
bool iot_obsolete(const struct iot_scheduler *scheduler, const struct iot_record *record);
struct iot_estimate iot_estimate_path(const struct iot_path_sample *path, size_t bytes, double slack_us);
struct iot_plan iot_plan_record(const struct iot_scheduler *scheduler, struct iot_workload *workload,
                                uint64_t elapsed, const struct iot_path_sample *paths, size_t count);
void iot_commit_plan(struct iot_scheduler *scheduler, enum iot_class category);

#endif
