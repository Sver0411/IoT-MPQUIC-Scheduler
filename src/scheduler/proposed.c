#include "scheduler.h"

#include <math.h>

static const double wait_scale_us = 100000;

static double saturate(double value)
{
    return value / (1 + value);
}

bool iot_obsolete(const struct iot_scheduler *scheduler, const struct iot_record *record)
{
    if (record->kind != IOT_STATE)
        return false;
    const struct iot_feedback *latest = iot_latest_feedback(scheduler, record->source);
    return latest && (record->generated < latest->generated ||
                      (record->generated == latest->generated && record->serial <= latest->serial));
}

struct iot_estimate iot_estimate_path(const struct iot_path_sample *path, size_t bytes, double slack_us)
{
    double rate = fmax(path->rate_Bps, 15000);
    double time = path->rtt_us * (path->pto ? 2.5 : 0.5) +
                  (path->queued_bytes + (double)bytes) * 1000000 / rate;
    double spread = fmax(path->rttvar_us, 1000);
    double arrival = slack_us > 0 ? fmin(1, fmax(0, (slack_us - time + spread) / (2 * spread))) : 0;
    double clean = pow(1 - path->loss, ceil((double)bytes / 1200));
    return (struct iot_estimate){time, arrival * clean};
}

static struct iot_plan plan_entry(const struct iot_scheduler *scheduler, struct iot_entry *entry,
                                   uint64_t now, const struct iot_path_sample *paths, size_t count)
{
    const struct iot_record *record = &entry->record;
    double waiting = (double)(now - entry->release);
    double priority = saturate(record->priority);
    double gain = 0, excess = 0;
    double slack = (double)record->deadline - now;
    enum iot_class category = IOT_URGENT;
    if (record->kind == IOT_STATE) {
        const struct iot_feedback *latest = iot_latest_feedback(scheduler, record->source);
        double scale = record->maximum_age;
        gain = latest ? saturate((record->generated - latest->generated) / scale) : 1;
        excess = latest ? saturate(fmax(0, (now - latest->generated) / scale - 1)) : 1;
        slack = fmin(slack, scale - (double)(now - record->generated));
        category = slack > 0 || scheduler->ablation == IOT_NO_RECOVERY ? IOT_NORMAL : IOT_RECOVERY;
    }
    if (scheduler->ablation == IOT_NO_AOI)
        gain = excess = 0;
    if (record->kind == IOT_EVENT && scheduler->ablation == IOT_NO_EVENT)
        priority = 0;
    struct iot_plan best = {.entry = entry, .category = category};
    for (size_t i = 0; i < count; i++) {
        struct iot_estimate estimate = iot_estimate_path(&paths[i], IOT_HEADER + record->payload_len, slack);
        if (i == 0 || (scheduler->ablation != IOT_FIXED_PATH && (estimate.probability > best.estimate.probability ||
            (estimate.probability == best.estimate.probability && estimate.duration_us < best.estimate.duration_us)))) {
            best.path_id = paths[i].id;
            best.estimate = estimate;
        }
    }
    double wait_score = saturate(waiting / wait_scale_us);
    if (category == IOT_RECOVERY) {
        best.score = priority + gain + excess + wait_score;
    } else {
        double severity = record->kind == IOT_EVENT && scheduler->ablation != IOT_NO_EVENT
                        ? saturate(record->severity) : 0;
        double urgency = wait_scale_us / (wait_scale_us + fmax(0, slack));
        best.score = priority + 2 * severity + gain + wait_score +
                     best.estimate.probability * (1 + urgency);
    }
    return best;
}

struct iot_plan iot_plan_record(const struct iot_scheduler *scheduler, struct iot_workload *workload,
                                uint64_t elapsed, const struct iot_path_sample *paths, size_t count)
{
    struct iot_plan best[IOT_URGENT + 1] = {0};
    if (!count)
        return best[IOT_NO_RECORD];
    for (size_t i = 0; i < workload->next; i++) {
        struct iot_entry *entry = &workload->entries[i];
        if (entry->state != IOT_PENDING || iot_obsolete(scheduler, &entry->record))
            continue;
        struct iot_plan candidate = plan_entry(scheduler, entry, elapsed, paths, count);
        struct iot_plan *current = &best[candidate.category];
        if (!current->entry || candidate.score > current->score)
            *current = candidate;
    }
    struct iot_plan normal = best[IOT_NORMAL], recovery = best[IOT_RECOVERY], event = best[IOT_URGENT];
    if (recovery.entry && scheduler->normal_since_recovery >= scheduler->recovery_k)
        return recovery;
    if (scheduler->event_streak >= IOT_EVENT_BURST && (normal.entry || recovery.entry))
        return normal.entry ? normal : recovery;
    if (event.entry && (!normal.entry || event.score > normal.score ||
        (event.score == normal.score && event.entry->record.id < normal.entry->record.id)))
        return event;
    return normal.entry ? normal : recovery;
}

void iot_commit_plan(struct iot_scheduler *scheduler, enum iot_class category)
{
    if (category == IOT_URGENT) {
        if (scheduler->event_streak < IOT_EVENT_BURST)
            scheduler->event_streak++;
        return;
    }
    scheduler->event_streak = 0;
    if (category == IOT_RECOVERY)
        scheduler->normal_since_recovery = 0;
    else if (category == IOT_NORMAL && scheduler->normal_since_recovery < scheduler->recovery_k)
        scheduler->normal_since_recovery++;
}
