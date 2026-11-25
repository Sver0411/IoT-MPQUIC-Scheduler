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
                  (path->queued_bytes + (double)path->in_flight_bytes + bytes) * 1000000 / rate +
                  path->delay_correction_us;
    double spread = fmax(path->rttvar_us, 1000);
    double arrival = slack_us > 0 ? fmin(1, fmax(0, (slack_us - time + spread) / (2 * spread))) : 0;
    double clean = pow(1 - path->loss, ceil((double)bytes / 1200));
    return (struct iot_estimate){time, arrival * clean};
}

/* CWND-blocking-aware delivery estimate used by FAP-MPQUIC (fresh-aware / fresh-aware-noprobe). */
struct iot_estimate iot_estimate_path_cwnd(const struct iot_path_sample *path, size_t bytes, double slack_us)
{
    double rate = fmax(path->rate_Bps, 15000);
    double load = (double)path->queued_bytes + (double)path->in_flight_bytes + (double)bytes;
    double cwnd_penalty = 0;
    /* Apply CWND-blocking round penalty only when actual in-flight bytes already saturate CWND
     * (avoids penalizing a clean slow-start backup path during sudden primary blackout). */
    if (path->cwnd_bytes > 0 && path->in_flight_bytes > path->cwnd_bytes) {
        double cwnd_denom = fmax((double)path->cwnd_bytes, 1200.0);
        cwnd_penalty = (double)path->rtt_us * (((double)path->in_flight_bytes - (double)path->cwnd_bytes) / cwnd_denom);
    }
    double time = path->rtt_us * (path->pto ? 2.5 : 0.5) +
                  load * 1000000 / rate +
                  cwnd_penalty +
                  path->delay_correction_us;
    double spread = fmax(path->rttvar_us, 1000);
    double arrival = slack_us > 0 ? fmin(1, fmax(0, (slack_us - time + spread) / (2 * spread))) : 0;
    double clean = pow(1 - path->loss, ceil((double)bytes / 1200));
    return (struct iot_estimate){time, arrival * clean};
}

void iot_update_path_feedback(struct iot_path_feedback *feedback, double residual_us)
{
    feedback->bias_us = 0.25 * residual_us + 0.75 * feedback->bias_us;
    feedback->samples++;
}

double iot_path_correction(const struct iot_path_feedback *feedback)
{
    if (feedback->samples < 3)
        return 0;
    return fmax(0, feedback->bias_us) * fmin(1, (feedback->samples - 2) / 3.0);
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

size_t iot_choose_initial_path(const struct iot_record *record, double slack,
                              const struct iot_path_sample *paths, size_t count,
                              enum iot_path_rule rule, uint32_t seed)
{
    if (rule == IOT_RANDOM_PATH) {
        unsigned time_bin = slack <= 0 ? 0 : slack <= 100000 ? 1 : slack <= 400000 ? 2 : 3;
        unsigned size_bin = record->payload_len < 1200 ? 0 : record->payload_len < 3000 ? 1 : 2;
        uint32_t draw = seed ^ record->id * UINT32_C(0x9e3779b9) ^
                        (time_bin * 3 + size_bin + 1) * UINT32_C(0x85ebca6b);
        draw = (draw ^ (draw >> 16)) * UINT32_C(0x7feb352d);
        draw = (draw ^ (draw >> 15)) * UINT32_C(0x846ca68b);
        return (draw ^ (draw >> 16)) % count;
    }
    size_t bytes = IOT_HEADER + record->payload_len;
    if (rule == IOT_BLEST_PATH) {
        /* BLEST (Ferlin et al., IFIP Networking 2016): pick lowest-RTT path if it has CWND space;
         * otherwise spill to alternate path iff HoL-blocking estimate X <= cwnd_fast. */
        size_t fast = 0;
        for (size_t i = 1; i < count; i++)
            if (paths[i].rtt_us < paths[fast].rtt_us)
                fast = i;
        double fast_load = (double)paths[fast].queued_bytes + (double)paths[fast].in_flight_bytes + (double)bytes;
        if (paths[fast].cwnd_bytes == 0 || fast_load <= (double)paths[fast].cwnd_bytes)
            return fast;
        double rtt_fast = fmax((double)paths[fast].rtt_us, 1000.0);
        size_t best_alt = fast;
        double best_alt_rtt = INFINITY;
        for (size_t i = 0; i < count; i++) {
            if (i == fast)
                continue;
            double alt_load = (double)paths[i].queued_bytes + (double)paths[i].in_flight_bytes + (double)bytes;
            if (paths[i].cwnd_bytes > 0 && alt_load > (double)paths[i].cwnd_bytes)
                continue;
            double rtt_slow = fmax((double)paths[i].rtt_us, rtt_fast);
            double x_bytes = (double)bytes * (rtt_slow / rtt_fast) + (double)paths[fast].in_flight_bytes;
            if (x_bytes <= (double)paths[fast].cwnd_bytes * 1.25 && rtt_slow < best_alt_rtt) {
                best_alt = i;
                best_alt_rtt = rtt_slow;
            }
        }
        return best_alt;
    }
    if (rule == IOT_FRESH_AWARE || rule == IOT_FRESH_NOPROBE) {
        static const uint64_t stale_horizon_us = 150000; /* 150 ms freshness horizon */
        size_t best = 0;
        struct iot_estimate chosen = iot_estimate_path_cwnd(&paths[0], bytes, slack);
        for (size_t i = 1; i < count; i++) {
            struct iot_estimate cand = iot_estimate_path_cwnd(&paths[i], bytes, slack);
            bool better = cand.probability > chosen.probability ||
                          (cand.probability == chosen.probability && cand.duration_us < chosen.duration_us);
            if (better) {
                best = i;
                chosen = cand;
            }
        }
        if (rule == IOT_FRESH_AWARE && count > 1) {
            const struct iot_path_sample *pri = &paths[best];
            double pri_load = (double)pri->queued_bytes + (double)pri->in_flight_bytes + (double)bytes;
            bool pri_stressed = pri->pto > 0 ||
                                (pri->cwnd_bytes > 0 && pri_load > (double)pri->cwnd_bytes) ||
                                (slack > 0 && chosen.probability < 0.25);
            if (record->kind == IOT_STATE || pri_stressed) {
                size_t probe = best;
                uint64_t oldest_stale = 0;
                for (size_t i = 0; i < count; i++) {
                    /* Health-gated freshness probe: never probe a path with PTO, recent loss,
                     * or unacknowledged in-flight bytes (in-flight bytes already probe it, and
                     * blacked-out paths keep in-flight > 0 until recovery). */
                    if (i == best || paths[i].pto > 0 || paths[i].recent_lost > 0 || paths[i].in_flight_bytes > 0)
                        continue;
                    uint64_t now_s = paths[i].sampled_us;
                    uint64_t rtt_age = now_s > paths[i].rtt_updated_us ? (now_s - paths[i].rtt_updated_us) : 0;
                    uint64_t probe_age = now_s > paths[i].last_probe_us ? (now_s - paths[i].last_probe_us) : UINT64_MAX;
                    if (rtt_age >= stale_horizon_us && probe_age >= stale_horizon_us && rtt_age > oldest_stale) {
                        oldest_stale = rtt_age;
                        probe = i;
                    }
                }
                return probe;
            }
        }
        return best;
    }
    size_t best = 0;
    struct iot_estimate chosen = iot_estimate_path(&paths[0], bytes, slack);
    for (size_t i = 1; i < count; i++) {
        struct iot_estimate candidate = iot_estimate_path(&paths[i], bytes, slack);
        bool better = candidate.duration_us < chosen.duration_us;
        if (rule == IOT_FIXED_MINRTT)
            better = paths[i].rtt_us < paths[best].rtt_us;
        if ((rule == IOT_PROBABILITY_PATH || rule == IOT_ACK_ADAPTIVE) &&
            candidate.probability != chosen.probability)
            better = candidate.probability > chosen.probability;
        if (better) {
            best = i;
            chosen = candidate;
        }
    }
    return best;
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
