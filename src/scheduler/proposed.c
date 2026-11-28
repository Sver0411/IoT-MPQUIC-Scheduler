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

/* Pillar 2: Online Adaptive Conformal Residual Quantile (OCRQ) timely delivery estimator. */
struct iot_estimate iot_estimate_path_conformal(const struct iot_path_sample *path, size_t bytes, double slack_us)
{
    double rate = fmax(path->rate_Bps, 15000);
    double load = (double)path->queued_bytes + (double)path->in_flight_bytes + (double)bytes;
    double cwnd_penalty = 0;
    if (path->cwnd_bytes > 0 && path->in_flight_bytes > path->cwnd_bytes) {
        double cwnd_denom = fmax((double)path->cwnd_bytes, 1200.0);
        cwnd_penalty = (double)path->rtt_us * (((double)path->in_flight_bytes - (double)path->cwnd_bytes) / cwnd_denom);
    }
    double pto_delay = 0.0;
    if (path->pto > 0) {
        unsigned shift = path->pto > 3 ? 3 : path->pto;
        pto_delay = (double)path->rtt_us * (2.5 * (double)(1u << shift)) + 50000.0 * (double)path->pto;
    }
    double base_time = (path->pto ? pto_delay : 0.5 * (double)path->rtt_us) +
                       load * 1000000.0 / rate +
                       cwnd_penalty;
    double q_adj = path->conformal_q_us > 0 ? path->conformal_q_us : 0.0;
    double time = base_time + q_adj;
    double spread = fmax((double)path->rttvar_us, 2000.0);
    double arrival = 0.0;
    if (slack_us > 0) {
        double z = (slack_us - time) / spread;
        if (z > 10.0)
            arrival = 1.0;
        else if (z < -10.0)
            arrival = 0.0;
        else
            arrival = 1.0 / (1.0 + exp(-2.0 * z));
    }
    double clean = pow(1.0 - path->loss, ceil((double)bytes / 1200.0));
    if (path->pto > 0)
        clean *= pow(0.35, (double)path->pto);
    return (struct iot_estimate){time, arrival * clean};
}

void iot_update_path_feedback(struct iot_path_feedback *feedback, double residual_us)
{
    feedback->bias_us = 0.25 * residual_us + 0.75 * feedback->bias_us;
    feedback->samples++;
}

void iot_update_conformal_feedback(struct iot_path_feedback *feedback, double residual_us, double rttvar_us)
{
    /* Online Adaptive Conformal Inference (ACI) at target miscoverage alpha = 0.20.
     * Tracks positive one-way queueing/retransmission tail residuals without over-inflating
     * during transient startup bursts. */
    const double alpha = 0.20;
    const double eta = 0.25;
    double step_scale = fmax(fmin(rttvar_us, 40000.0), 10000.0);
    double miss = residual_us > feedback->conformal_q_us ? 1.0 : 0.0;
    if (miss > 0.5)
        feedback->conformal_misses++;
    feedback->conformal_q_us += eta * (miss - alpha) * step_scale;
    if (feedback->conformal_q_us < -15000.0)
        feedback->conformal_q_us = -15000.0;
    if (feedback->conformal_q_us > 250000.0)
        feedback->conformal_q_us = 250000.0;
    feedback->conformal_samples++;
}

void iot_update_ucb_feedback(struct iot_path_feedback *feedback, double reward)
{
    feedback->ucb_pulls++;
    feedback->ucb_reward_sum += reward;
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
    if (rule == IOT_PEEKABOO_UCB) {
        /* Peekaboo-style online UCB1 multi-armed bandit over paths (Wu et al., JSAC 2020). */
        size_t best_k = 0;
        double best_ucb = -INFINITY;
        unsigned total_pulls = 1;
        for (size_t i = 0; i < count; i++)
            total_pulls += paths[i].ucb_pulls;
        for (size_t i = 0; i < count; i++) {
            double score;
            if (paths[i].pto > 0) {
                score = -10.0 - (double)paths[i].pto;
            } else if (paths[i].ucb_pulls == 0) {
                score = 100.0 - (double)paths[i].rtt_us * 1e-6;
            } else {
                double mean_r = paths[i].ucb_reward_sum / (double)paths[i].ucb_pulls;
                double bonus = 0.5 * sqrt(log((double)total_pulls + 1.0) / (double)paths[i].ucb_pulls);
                score = mean_r + bonus;
            }
            if (score > best_ucb) {
                best_ucb = score;
                best_k = i;
            }
        }
        return best_k;
    }
    if (rule == IOT_DAMPS_EDF) {
        /* DaMPS-style deadline-aware minimum expected completion time (Chen et al., MASS 2024). */
        size_t best_k = 0;
        double best_t = INFINITY;
        for (size_t i = 0; i < count; i++) {
            double rate = fmax((double)paths[i].rate_Bps, 15000.0);
            double load = (double)paths[i].queued_bytes + (double)paths[i].in_flight_bytes + (double)bytes;
            double loss_pen = (paths[i].loss / fmax(1.0 - paths[i].loss, 0.05)) * (double)paths[i].rtt_us;
            double exp_t = 0.5 * (double)paths[i].rtt_us + load * 1e6 / rate + loss_pen;
            if (paths[i].pto > 0)
                exp_t += 2.0 * (double)paths[i].rtt_us;
            if (exp_t < best_t) {
                best_t = exp_t;
                best_k = i;
            }
        }
        return best_k;
    }
    if (rule == IOT_ACSP_FGCA || rule == IOT_ACSP_CONFORMAL ||
        rule == IOT_ACSP_JOINT || rule == IOT_ACSP_FULL) {
        bool use_conf = rule != IOT_ACSP_FGCA;
        bool any_fresh_healthy = false;
        for (size_t i = 0; i < count; i++) {
            uint64_t rtt_age = paths[i].sampled_us > paths[i].rtt_updated_us
                                   ? (paths[i].sampled_us - paths[i].rtt_updated_us) : 0;
            if (paths[i].pto == 0 && rtt_age < 250000)
                any_fresh_healthy = true;
        }
        size_t best_k = 0;
        struct iot_estimate chosen = use_conf
            ? iot_estimate_path_conformal(&paths[0], bytes, slack)
            : iot_estimate_path_cwnd(&paths[0], bytes, slack);
        double best_eff_p = chosen.probability;
        if (use_conf && record->kind == IOT_EVENT && any_fresh_healthy) {
            uint64_t age0 = paths[0].sampled_us > paths[0].rtt_updated_us
                                ? (paths[0].sampled_us - paths[0].rtt_updated_us) : 0;
            if (age0 >= 250000)
                best_eff_p *= 0.90;
        }
        for (size_t i = 1; i < count; i++) {
            struct iot_estimate cand = use_conf
                ? iot_estimate_path_conformal(&paths[i], bytes, slack)
                : iot_estimate_path_cwnd(&paths[i], bytes, slack);
            double cand_p = cand.probability;
            if (use_conf && record->kind == IOT_EVENT && any_fresh_healthy) {
                uint64_t age_i = paths[i].sampled_us > paths[i].rtt_updated_us
                                     ? (paths[i].sampled_us - paths[i].rtt_updated_us) : 0;
                if (age_i >= 250000)
                    cand_p *= 0.90;
            }
            bool better = cand_p > best_eff_p ||
                          (cand_p == best_eff_p && cand.duration_us < chosen.duration_us);
            if (better) {
                best_k = i;
                chosen = cand;
                best_eff_p = cand_p;
            }
        }
        if (count > 1) {
            const struct iot_path_sample *pri = &paths[best_k];
            uint64_t pri_rtt = pri->rtt_us > 0 ? pri->rtt_us : 10000;
            double pri_load = (double)pri->queued_bytes + (double)pri->in_flight_bytes + (double)bytes;
            bool pri_stressed = pri->pto > 0 ||
                                (pri->cwnd_bytes > 0 && pri_load > (double)pri->cwnd_bytes) ||
                                (slack > 0 && chosen.probability < 0.25);
            if (record->kind == IOT_STATE || pri_stressed) {
                size_t probe = best_k;
                uint64_t oldest = 0;
                for (size_t i = 0; i < count; i++) {
                    if (i == best_k || paths[i].pto > 0 || paths[i].recent_lost > 0 || paths[i].in_flight_bytes > 0)
                        continue;
                    bool much_slower = paths[i].rtt_us > pri_rtt * 2 + pri_rtt / 2 ||
                                       paths[i].conformal_q_us > 40000.0;
                    uint64_t adapt_stale = much_slower ? 400000 : 120000;
                    uint64_t now_s = paths[i].sampled_us;
                    uint64_t rtt_age = now_s > paths[i].rtt_updated_us ? (now_s - paths[i].rtt_updated_us) : 0;
                    uint64_t probe_age = now_s > paths[i].last_probe_us ? (now_s - paths[i].last_probe_us) : UINT64_MAX;
                    if (rtt_age >= adapt_stale && probe_age >= adapt_stale && rtt_age > oldest) {
                        oldest = rtt_age;
                        probe = i;
                    }
                }
                return probe;
            }
        }
        return best_k;
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

/* Pillar 1: Feasibility-Gated Credit Admission (FGCA) — shed provably doomed/expired alarms
 * before they lock scarce MAX_STREAMS credits and trigger deadline-inversion cascades.
 * Sheds expired alarms (slack <= 0) unconditionally, and contention-infeasible alarms
 * (under_pressure && best physical delivery probability < 0.05) without using queue-inflated SRTT
 * cutoffs that could falsely shed saveable alarms. */
bool iot_should_shed_event(const struct iot_record *record, double slack,
                           const struct iot_path_sample *paths, size_t count,
                           enum iot_path_rule rule, bool under_pressure, double *best_prob_out)
{
    (void)rule;
    if (best_prob_out)
        *best_prob_out = 0.0;
    if (record->kind != IOT_EVENT)
        return false;
    if (slack <= 0.0)
        return true;
    if (!paths || !count)
        return false;
    size_t bytes = IOT_HEADER + record->payload_len;
    double best_p = 0.0;
    for (size_t i = 0; i < count; i++) {
        struct iot_estimate est = iot_estimate_path_cwnd(&paths[i], bytes, slack);
        if (est.probability > best_p)
            best_p = est.probability;
    }
    if (best_prob_out)
        *best_prob_out = best_p;
    return under_pressure && best_p < 0.05;
}

/* Pillar 3: Joint Record-Path Bipartite Matching with Dynamic Credit Shadow Price (JBM-SP),
 * Moore-Hodgson Shortest-Credit-Occupancy Alarm Ordering, and Backoff-Gated Stale Probing.
 * Strictly preserves optimal conformal path selection per record (never trading higher path
 * delivery probability for stale low-RTT paths). Complexity: strictly O(|Q| * K). */
struct iot_plan iot_plan_joint_acsp(const struct iot_scheduler *scheduler, struct iot_workload *workload,
                                    uint64_t elapsed, const struct iot_path_sample *paths, size_t count,
                                    enum iot_path_rule rule, size_t outstanding, size_t max_credits,
                                    size_t *chosen_path_index)
{
    struct iot_plan best[IOT_URGENT + 1] = {0};
    size_t best_idx[IOT_URGENT + 1] = {0};
    if (!paths || !count)
        return best[IOT_NO_RECORD];

    double lambda = max_credits > 0 ? fmin(1.5, (double)outstanding / (double)max_credits) : 0.0;

    /* Identify fastest healthy path and track whether any path has fresh causal telemetry. */
    size_t fast_k = 0;
    bool any_fresh_healthy = false;
    for (size_t k = 0; k < count; k++) {
        if ((paths[k].pto == 0 && paths[fast_k].pto > 0) ||
            (paths[k].pto == paths[fast_k].pto && paths[k].rtt_us < paths[fast_k].rtt_us))
            fast_k = k;
        uint64_t rtt_age = paths[k].sampled_us > paths[k].rtt_updated_us
                               ? (paths[k].sampled_us - paths[k].rtt_updated_us) : 0;
        if (paths[k].pto == 0 && rtt_age < 250000)
            any_fresh_healthy = true;
    }

    /* Count pending saveable alarms so state probing never steals scarce credits mid-burst. */
    size_t pending_events = 0;
    for (size_t i = 0; i < workload->next; i++)
        if (workload->entries[i].state == IOT_PENDING && workload->entries[i].record.kind == IOT_EVENT)
            pending_events++;

    /* Backoff-gated stale-path probe eligibility (clean, idle alternate paths only). */
    bool probe_ready[4] = {false, false, false, false};
    if (pending_events == 0) {
        uint64_t fast_rtt = paths[fast_k].rtt_us > 0 ? paths[fast_k].rtt_us : 10000;
        for (size_t k = 0; k < count && k < 4; k++) {
            if (k == fast_k || paths[k].pto > 0 || paths[k].recent_lost > 0 || paths[k].in_flight_bytes > 0)
                continue;
            bool much_slower = paths[k].rtt_us > fast_rtt * 2 + fast_rtt / 2 ||
                               paths[k].conformal_q_us > 40000.0;
            uint64_t stale_thresh = much_slower ? 400000 : 120000;
            uint64_t now_s = paths[k].sampled_us;
            uint64_t rtt_age = now_s > paths[k].rtt_updated_us ? (now_s - paths[k].rtt_updated_us) : 0;
            uint64_t probe_age = now_s > paths[k].last_probe_us ? (now_s - paths[k].last_probe_us) : UINT64_MAX;
            if (rtt_age >= stale_thresh && probe_age >= stale_thresh)
                probe_ready[k] = true;
        }
    }

    for (size_t i = 0; i < workload->next; i++) {
        struct iot_entry *entry = &workload->entries[i];
        if (entry->state != IOT_PENDING || iot_obsolete(scheduler, &entry->record))
            continue;
        const struct iot_record *rec = &entry->record;
        double slack = iot_record_slack(rec, elapsed);
        size_t bytes = IOT_HEADER + rec->payload_len;
        double waiting = (double)(elapsed - entry->release);
        double priority = saturate(rec->priority);
        double wait_score = scheduler->tuning.waiting_weight * saturate(waiting / wait_scale_us);
        enum iot_class category = IOT_URGENT;
        double gain = 0.0, excess = 0.0;
        if (rec->kind == IOT_STATE) {
            const struct iot_feedback *latest = iot_latest_feedback(scheduler, rec->source);
            double scale = rec->maximum_age;
            gain = latest ? saturate((rec->generated - latest->generated) / scale) : 1.0;
            excess = latest ? saturate(fmax(0.0, (elapsed - latest->generated) / scale - 1.0)) : 1.0;
            category = slack > 0 ? IOT_NORMAL : IOT_RECOVERY;
        }
        double severity = rec->kind == IOT_EVENT ? saturate(rec->severity) : 0.0;
        double urgency = wait_scale_us / (wait_scale_us + fmax(0.0, slack));

        /* Step 1: Choose strictly optimal conformal path k*(i) for record i. */
        size_t opt_k = 0;
        struct iot_estimate opt_est = {0};
        double opt_rank = -INFINITY;
        for (size_t k = 0; k < count; k++) {
            struct iot_estimate est = iot_estimate_path_conformal(&paths[k], bytes, slack);
            double eff_prob = est.probability;
            if (category == IOT_URGENT && any_fresh_healthy) {
                uint64_t rtt_age = paths[k].sampled_us > paths[k].rtt_updated_us
                                       ? (paths[k].sampled_us - paths[k].rtt_updated_us) : 0;
                if (rtt_age >= 250000)
                    eff_prob *= 0.90; /* Discount unverified stale path for hard-deadline alarms. */
            }
            /* Rank paths primarily by timely delivery probability (or inverse duration for recovery). */
            double rank;
            if (category == IOT_RECOVERY) {
                /* Recovery states have slack <= 0: always pick fastest non-PTO path! */
                rank = (paths[k].pto > 0 ? -1000.0 * paths[k].pto : 0.0) - est.duration_us * 1e-6;
            } else {
                rank = eff_prob * 100.0 - est.duration_us * 1e-6;
            }
            if (k == 0 || rank > opt_rank) {
                opt_rank = rank;
                opt_k = k;
                opt_est = est;
            }
        }

        /* Optional zero-overhead stale-path freshness probe for loose-slack normal states. */
        if (category == IOT_NORMAL && slack > 120000.0) {
            for (size_t k = 0; k < count && k < 4; k++) {
                if (probe_ready[k]) {
                    opt_k = k;
                    opt_est = iot_estimate_path_conformal(&paths[k], bytes, slack);
                    break;
                }
            }
        }

        /* Step 2: Score record i on its optimal path k*(i) using credit-shadow-priced efficiency. */
        double hold_cost = fmin(3.0, opt_est.duration_us / 40000.0);
        double score;
        if (category == IOT_URGENT) {
            /* Moore-Hodgson credit-efficiency ordering: among saveable alarms, prefer tight slack
             * and short credit-holding duration so jumbo payloads never HoL-block fast alarms. */
            double feasible = opt_est.probability >= 0.20 ? 1.0 : opt_est.probability / 0.20;
            double eff_bonus = 0.85 / (1.0 + 0.45 * lambda * hold_cost);
            score = feasible * (3.0 * urgency + eff_bonus + 0.45 * scheduler->tuning.severity_weight * severity +
                                0.30 * priority + 0.20 * wait_score) +
                    0.50 * opt_est.probability;
        } else if (category == IOT_NORMAL) {
            score = priority + 1.4 * gain + wait_score + 0.40 * opt_est.probability -
                    0.12 * lambda * fmin(2.0, opt_est.duration_us / wait_scale_us);
        } else {
            score = priority + gain + excess + wait_score;
        }

        struct iot_plan *cur = &best[category];
        if (!cur->entry || score > cur->score ||
            (score == cur->score && opt_est.duration_us < cur->estimate.duration_us)) {
            *cur = (struct iot_plan){
                .entry = entry,
                .category = category,
                .path_id = paths[opt_k].id,
                .estimate = opt_est,
                .score = score,
            };
            best_idx[category] = opt_k;
        }
    }

    (void)rule;
    enum iot_class pick = IOT_NO_RECORD;
    bool urgent_saveable = best[IOT_URGENT].entry && best[IOT_URGENT].estimate.probability >= 0.20;
    unsigned burst_cap = scheduler->tuning.event_burst;
    if (urgent_saveable && iot_record_slack(&best[IOT_URGENT].entry->record, elapsed) < 150000.0)
        burst_cap = burst_cap + 2; /* Protect tight-deadline alarm bursts from mid-burst interruption. */

    if (best[IOT_RECOVERY].entry && !urgent_saveable &&
        scheduler->normal_since_recovery >= scheduler->recovery_k) {
        pick = IOT_RECOVERY;
    } else if (scheduler->event_streak >= burst_cap &&
               (best[IOT_NORMAL].entry || best[IOT_RECOVERY].entry)) {
        pick = best[IOT_NORMAL].entry ? IOT_NORMAL : IOT_RECOVERY;
    } else if (best[IOT_URGENT].entry &&
               (!best[IOT_NORMAL].entry || urgent_saveable ||
                best[IOT_URGENT].score >= best[IOT_NORMAL].score)) {
        pick = IOT_URGENT;
    } else if (best[IOT_NORMAL].entry) {
        pick = IOT_NORMAL;
    } else if (best[IOT_RECOVERY].entry) {
        pick = IOT_RECOVERY;
    }
    if (chosen_path_index)
        *chosen_path_index = best_idx[pick];
    return best[pick];
}
