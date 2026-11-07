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
                  (path->queued_bytes + (double)path->in_flight_bytes + bytes) * 1000000 / rate;
    double spread = fmax(path->rttvar_us, 1000);
    double arrival = slack_us > 0 ? fmin(1, fmax(0, (slack_us - time + spread) / (2 * spread))) : 0;
    double clean = pow(1 - path->loss, ceil((double)bytes / 1200));
    return (struct iot_estimate){time, arrival * clean};
}

double iot_record_slack(const struct iot_record *record, uint64_t elapsed)
{
    double slack = (double)record->deadline - elapsed;
    if (record->kind == IOT_STATE)
        slack = fmin(slack, (double)record->maximum_age - ((double)elapsed - record->generated));
    return slack;
}

void iot_plan_path(struct iot_plan *plan, double slack, const struct iot_path_sample *paths,
                   size_t count, bool fixed)
{
    size_t bytes = IOT_HEADER + plan->entry->record.payload_len;
    for (size_t i = 0; i < count; i++) {
        struct iot_estimate estimate = iot_estimate_path(&paths[i], bytes, slack);
        if (i == 0 || (!fixed && (estimate.probability > plan->estimate.probability ||
            (estimate.probability == plan->estimate.probability && estimate.duration_us < plan->estimate.duration_us)))) {
            plan->path_id = paths[i].id;
            plan->estimate = estimate;
        }
    }
}

static struct iot_plan plan_entry(const struct iot_scheduler *scheduler, struct iot_entry *entry,
                                   uint64_t now, const struct iot_path_sample *paths, size_t count)
{
    const struct iot_record *record = &entry->record;
    double waiting = (double)(now - entry->release);
    double priority = saturate(record->priority);
    double gain = 0, excess = 0;
    double slack = iot_record_slack(record, now);
    enum iot_class category = IOT_URGENT;
    if (record->kind == IOT_STATE) {
        const struct iot_feedback *latest = iot_latest_feedback(scheduler, record->source);
        double scale = record->maximum_age;
        gain = latest ? saturate((record->generated - latest->generated) / scale) : 1;
        excess = latest ? saturate(fmax(0, (now - latest->generated) / scale - 1)) : 1;
        category = slack > 0 || scheduler->ablation == IOT_NO_RECOVERY ? IOT_NORMAL : IOT_RECOVERY;
    }
    if (scheduler->ablation == IOT_NO_AOI)
        gain = excess = 0;
    if (record->kind == IOT_EVENT && scheduler->ablation == IOT_NO_EVENT)
        priority = 0;
    struct iot_plan best = {.entry = entry, .category = category};
    if (iot_policy_joint(scheduler->policy))
        iot_plan_path(&best, slack - scheduler->tuning.slack_margin_us, paths, count,
                      scheduler->ablation == IOT_FIXED_PATH);
    double wait_score = scheduler->tuning.waiting_weight * saturate(waiting / wait_scale_us);
    if (category == IOT_RECOVERY) {
        best.score = priority + gain + excess + wait_score;
    } else {
        double severity = record->kind == IOT_EVENT && scheduler->ablation != IOT_NO_EVENT
                        ? saturate(record->severity) : 0;
        double urgency = wait_scale_us / (wait_scale_us + fmax(0, slack));
        best.score = priority + scheduler->tuning.severity_weight * severity + gain + wait_score;
        if (scheduler->policy == IOT_PROPOSED_APP)
            best.score += urgency;
        else if (scheduler->ablation != IOT_NO_PROBABILITY)
            best.score += best.estimate.probability * (1 + urgency);
    }
    return best;
}

struct iot_plan iot_plan_record(const struct iot_scheduler *scheduler, struct iot_workload *workload,
                                uint64_t elapsed, const struct iot_path_sample *paths, size_t count)
{
    struct iot_plan best[IOT_URGENT + 1] = {0};
    if (!count && iot_policy_joint(scheduler->policy))
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
    if (scheduler->event_streak >= scheduler->tuning.event_burst && (normal.entry || recovery.entry))
        return normal.entry ? normal : recovery;
    if (event.entry && (!normal.entry || event.score > normal.score ||
        (event.score == normal.score && event.entry->record.id < normal.entry->record.id)))
        return event;
    return normal.entry ? normal : recovery;
}

void iot_commit_plan(struct iot_scheduler *scheduler, enum iot_class category)
{
    if (category == IOT_URGENT) {
        if (scheduler->event_streak < scheduler->tuning.event_burst)
            scheduler->event_streak++;
        return;
    }
    scheduler->event_streak = 0;
    if (category == IOT_RECOVERY)
        scheduler->normal_since_recovery = 0;
    else if (category == IOT_NORMAL && scheduler->normal_since_recovery < scheduler->recovery_k)
        scheduler->normal_since_recovery++;
}
