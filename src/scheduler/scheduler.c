#include "scheduler.h"

#include <string.h>

static const char *const policy_names[] = {
    [IOT_FIFO_REPLACE] = "fifo-replace",
    [IOT_FIFO] = "fifo",
    [IOT_PRIORITY] = "strict-priority",
    [IOT_EDF] = "edf",
    [IOT_AOI] = "aoi",
    [IOT_LATEST_RR] = "latest-rr",
    [IOT_PROPOSED_STRICT] = "proposed-strict",
    [IOT_PROPOSED_CROSS] = "proposed-cross",
    [IOT_PROPOSED_APP] = "proposed-app",
};

static const char *const ablation_names[] = {
    [IOT_FULL] = "full",
    [IOT_NO_REPLACE] = "no-replace",
    [IOT_NO_EVENT] = "no-event",
    [IOT_NO_AOI] = "no-aoi",
    [IOT_FIXED_PATH] = "fixed-path",
    [IOT_NO_RECOVERY] = "no-recovery",
    [IOT_NO_CROSS] = "no-cross",
    [IOT_NO_PROBABILITY] = "no-probability",
};

const char *iot_ablation_name(enum iot_ablation ablation)
{
    return ablation_names[ablation];
}

bool iot_parse_ablation(const char *name, enum iot_ablation *ablation)
{
    for (size_t i = 0; i < sizeof(ablation_names) / sizeof(*ablation_names); i++) {
        if (!strcmp(name, ablation_names[i])) {
            *ablation = (enum iot_ablation)i;
            return true;
        }
    }
    return false;
}

const char *iot_policy_name(enum iot_policy policy)
{
    return policy_names[policy];
}

bool iot_parse_policy(const char *name, enum iot_policy *policy)
{
    for (size_t i = 0; i < sizeof(policy_names) / sizeof(*policy_names); i++) {
        if (!strcmp(name, policy_names[i])) {
            *policy = (enum iot_policy)i;
            return true;
        }
    }
    return false;
}

bool iot_policy_replaces(enum iot_policy policy)
{
    return policy == IOT_FIFO_REPLACE || policy == IOT_LATEST_RR || iot_policy_proposed(policy);
}

bool iot_policy_proposed(enum iot_policy policy)
{
    return iot_policy_joint(policy) || policy == IOT_PROPOSED_APP;
}

bool iot_policy_joint(enum iot_policy policy)
{
    return policy == IOT_PROPOSED_STRICT || policy == IOT_PROPOSED_CROSS;
}

static size_t feedback_index(const struct iot_scheduler *scheduler, uint32_t sensor)
{
    size_t i = 0;
    while (i < scheduler->feedback_count && scheduler->feedback[i].sensor != sensor)
        i++;
    return i;
}

const struct iot_feedback *iot_latest_feedback(const struct iot_scheduler *scheduler, uint32_t sensor)
{
    size_t i = feedback_index(scheduler, sensor);
    return i < scheduler->feedback_count ? &scheduler->feedback[i] : NULL;
}

void iot_schedule_feedback(struct iot_scheduler *scheduler, uint32_t sensor, uint64_t generated,
                           uint64_t serial)
{
    if (scheduler->policy != IOT_AOI && !iot_policy_proposed(scheduler->policy))
        return;
    size_t i = feedback_index(scheduler, sensor);
    if (i == scheduler->feedback_count) {
        if (i == IOT_MAX_RECORDS)
            return;
        scheduler->feedback[i] = (struct iot_feedback){sensor, generated, serial};
        scheduler->feedback_count++;
    } else if (generated > scheduler->feedback[i].generated ||
               (generated == scheduler->feedback[i].generated && serial > scheduler->feedback[i].serial)) {
        scheduler->feedback[i].generated = generated;
        scheduler->feedback[i].serial = serial;
    }
}

struct aoi_score {
    uint64_t gain, scale;
};

static struct aoi_score aoi_score(const struct iot_scheduler *scheduler, const struct iot_record *record)
{
    if (record->kind == IOT_EVENT)
        return (struct aoi_score){0, 1};
    size_t i = feedback_index(scheduler, record->source);
    if (i == scheduler->feedback_count)
        return (struct aoi_score){1, 1};
    uint64_t last = scheduler->feedback[i].generated;
    uint64_t gain = record->generated > last ? record->generated - last : 0;
    return (struct aoi_score){gain, record->maximum_age};
}

static bool precedes(const struct iot_scheduler *scheduler, const struct iot_record *candidate,
                     const struct iot_record *best)
{
    switch (scheduler->policy) {
    case IOT_PRIORITY:
        return candidate->priority > best->priority;
    case IOT_EDF:
        return candidate->deadline < best->deadline;
    case IOT_AOI: {
        struct aoi_score a = aoi_score(scheduler, candidate);
        struct aoi_score b = aoi_score(scheduler, best);
        return (__uint128_t)a.gain * b.scale > (__uint128_t)b.gain * a.scale;
    }
    case IOT_LATEST_RR: {
        if (candidate->kind != best->kind)
            return (candidate->kind == IOT_EVENT) == scheduler->prefer_event;
        if (candidate->kind == IOT_EVENT)
            return false;
        bool candidate_ahead = candidate->source > scheduler->last_sensor;
        bool best_ahead = best->source > scheduler->last_sensor;
        return candidate_ahead == best_ahead ? candidate->source < best->source : candidate_ahead;
    }
    default:
        return false;
    }
}

struct iot_entry *iot_select(const struct iot_scheduler *scheduler, struct iot_workload *workload)
{
    struct iot_entry *best = NULL;
    for (size_t i = 0; i < workload->next; i++) {
        struct iot_entry *entry = &workload->entries[i];
        if (entry->state == IOT_PENDING &&
            (!best || precedes(scheduler, &entry->record, &best->record)))
            best = entry;
    }
    return best;
}

void iot_schedule_commit(struct iot_scheduler *scheduler, const struct iot_record *record)
{
    if (scheduler->policy != IOT_LATEST_RR)
        return;
    scheduler->prefer_event = record->kind == IOT_STATE;
    if (record->kind == IOT_STATE)
        scheduler->last_sensor = record->source;
}
