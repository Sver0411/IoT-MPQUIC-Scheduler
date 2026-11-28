#undef NDEBUG
#include <assert.h>
#include <string.h>

#include "scheduler/scheduler.h"

static struct iot_workload workload;
static struct iot_scheduler scheduler = {.tuning = IOT_DEFAULT_TUNING};

static void add(uint8_t kind, uint32_t source, uint64_t generated, uint64_t deadline,
                uint64_t maximum_age, uint8_t priority)
{
    uint32_t id = (uint32_t)workload.count + 1;
    workload.entries[workload.count++] = (struct iot_entry){
        .record = {.id = id, .kind = kind, .source = source, .generated = generated,
                   .deadline = deadline, .maximum_age = maximum_age, .priority = priority,
                   .serial = id},
    };
}

static void admit(uint32_t expected_drop)
{
    uint32_t dropped;
    size_t index;
    assert(iot_admit(&workload, 0, iot_policy_replaces(scheduler.policy), &index, &dropped));
    assert(dropped == expected_drop);
}

static void pick(uint32_t expected)
{
    struct iot_entry *entry = iot_select(&scheduler, &workload);
    assert(entry && entry->record.id == expected);
    // Peeking twice must leave round-robin and pending state unchanged.
    assert(iot_select(&scheduler, &workload) == entry);
    entry->state = IOT_SUBMITTED;
    iot_schedule_commit(&scheduler, &entry->record);
}

static void test_order(const uint32_t order[4])
{
    add(IOT_STATE, 1, 0, 10, 10, 1);
    add(IOT_EVENT, 1, 0, 20, 0, 3);
    add(IOT_STATE, 1, 0, 5, 10, 3);
    add(IOT_EVENT, 1, 0, 5, 0, 2);
    workload.entries[3].release = 1;
    for (int i = 0; i < 3; i++)
        admit(0);
    size_t index;
    uint32_t dropped;
    assert(!iot_admit(&workload, 0, false, &index, &dropped));
    assert(iot_admit(&workload, 1, false, &index, &dropped));
    for (int i = 0; i < 4; i++)
        pick(order[i]);
}

static void test_aoi(void)
{
    add(IOT_EVENT, 1, 0, 1, 0, 9);
    add(IOT_STATE, 1, 90, 100, 30, 1);
    add(IOT_STATE, 2, 90, 100, 10, 1);
    add(IOT_STATE, 3, 90, 100, 30, 1);
    add(IOT_STATE, 4, 25, 100, 30, 1);
    add(IOT_STATE, 5, 95, 100, 100, 1);
    add(IOT_STATE, 6, 90, 100, 20, 1);
    for (int i = 0; i < 7; i++)
        admit(0);
    iot_schedule_feedback(&scheduler, 1, 30, 1); // 60/30
    iot_schedule_feedback(&scheduler, 2, 60, 1); // 30/10
    iot_schedule_feedback(&scheduler, 2, 90, 2); // New ACK makes candidate obsolete.
    iot_schedule_feedback(&scheduler, 2, 10, 1); // An older ACK cannot regress the estimate.
    iot_schedule_feedback(&scheduler, 4, 30, 1);
    iot_schedule_feedback(&scheduler, 5, 55, 1); // 40/100
    iot_schedule_feedback(&scheduler, 6, 80, 1); // 10/20 ranks above 40/100.
    pick(2);
    pick(4); // Unseen sensor scores one.
    pick(7);
    pick(6);
    pick(1); // Event and stale state score zero, then FIFO.
    pick(3);
    pick(5);
}

static void test_round_robin(void)
{
    add(IOT_STATE, 9, 0, 10, 10, 1);
    add(IOT_STATE, 3, 0, 10, 10, 1);
    add(IOT_STATE, 3, 0, 10, 10, 1);
    add(IOT_EVENT, 3, 0, 10, 0, 9);
    add(IOT_EVENT, 3, 0, 10, 0, 1);
    add(IOT_STATE, 3, 1, 10, 10, 1);
    add(IOT_STATE, 1, 0, 10, 10, 1);
    add(IOT_STATE, 3, 0, 10, 10, 1);
    admit(0);
    admit(0);
    admit(2);
    admit(0);
    admit(0);
    pick(3);
    admit(0);
    admit(0);
    admit(8); // Older input must not replace the pending newer state.
    pick(4);
    pick(1);
    pick(5);
    pick(7); // Wrap the sensor cursor.
    pick(6);
    assert(workload.entries[2].state == IOT_SUBMITTED);
}

static void test_proposed(void)
{
    const uint64_t now = 100000;
    scheduler.recovery_k = 2;
    add(IOT_STATE, 1, 90000, 300000, 200000, 1);
    add(IOT_STATE, 2, 0, 50000, 50000, 2);
    add(IOT_EVENT, 4, 90000, 300000, 0, 9);
    add(IOT_STATE, 3, 80000, 300000, 200000, 1);
    workload.entries[2].record.severity = 9;
    for (int i = 0; i < 4; i++)
        admit(0);
    struct iot_path_sample paths[] = {
        {.id = 0, .rtt_us = 4000, .rttvar_us = 1000, .rate_Bps = 100000, .loss = 0.02},
        {.id = 1, .rtt_us = 20000, .rttvar_us = 2000, .rate_Bps = 100000, .loss = 0.02},
    };
    assert(!iot_plan_record(&scheduler, &workload, now, paths, 0).entry);
    struct iot_plan plan = iot_plan_record(&scheduler, &workload, now, paths, 2);
    assert(plan.category == IOT_URGENT && plan.entry->record.id == 3 && plan.path_id == 0);
    scheduler.ablation = IOT_NO_EVENT;
    assert(iot_plan_record(&scheduler, &workload, now, paths, 2).category == IOT_NORMAL);
    scheduler.ablation = IOT_FULL;
    paths[0].pto = 1;
    paths[0].queued_bytes = 1000000;
    plan = iot_plan_record(&scheduler, &workload, now, paths, 2);
    assert(plan.path_id == 1 && plan.estimate.probability > 0);
    scheduler.ablation = IOT_FIXED_PATH;
    plan = iot_plan_record(&scheduler, &workload, now, paths, 2);
    assert(plan.path_id == 0 && plan.estimate.probability == 0);
    scheduler.ablation = IOT_FULL;
    for (unsigned i = 0; i < IOT_EVENT_BURST; i++)
        iot_commit_plan(&scheduler, IOT_URGENT);
    plan = iot_plan_record(&scheduler, &workload, now, paths, 2);
    assert(plan.category == IOT_NORMAL);
    double with_age = plan.score;
    scheduler.ablation = IOT_NO_AOI;
    plan = iot_plan_record(&scheduler, &workload, now, paths, 2);
    assert(plan.category == IOT_NORMAL && plan.score < with_age);
    scheduler.ablation = IOT_FULL;
    plan.entry->state = IOT_SUBMITTED;
    iot_commit_plan(&scheduler, plan.category);
    iot_commit_plan(&scheduler, IOT_NORMAL);
    plan = iot_plan_record(&scheduler, &workload, now, paths, 2);
    assert(plan.category == IOT_RECOVERY && plan.entry->record.id == 2 && plan.estimate.probability == 0);
    scheduler.ablation = IOT_NO_RECOVERY;
    assert(iot_plan_record(&scheduler, &workload, now, paths, 2).category != IOT_RECOVERY);
    scheduler.ablation = IOT_FULL;
    iot_commit_plan(&scheduler, plan.category);
    assert(scheduler.normal_since_recovery == 0);
    iot_schedule_feedback(&scheduler, 2, 0, 2);
    iot_schedule_feedback(&scheduler, 2, 0, 1);
    assert(iot_obsolete(&scheduler, &workload.entries[1].record));
    assert(!iot_obsolete(&scheduler, &workload.entries[2].record));
    assert(iot_plan_record(&scheduler, &workload, now, paths, 2).category == IOT_URGENT);
}

static void test_independent_factors(void)
{
    struct iot_path_feedback feedback[2] = {0};
    struct iot_path_sample candidates[] = {
        {.id = 0, .rtt_us = 20000, .rate_Bps = 1000000},
        {.id = 1, .rtt_us = 24000, .rate_Bps = 1000000},
    };
    struct iot_record probe = {0};
    assert(iot_choose_initial_path(&probe, 11000, candidates, 2, IOT_ACK_ADAPTIVE, 0) == 0);
    iot_update_path_feedback(&feedback[0], 20000);
    iot_update_path_feedback(&feedback[0], 20000);
    assert(iot_path_correction(&feedback[0]) == 0);
    iot_update_path_feedback(&feedback[0], 20000);
    assert(feedback[0].bias_us == 11562.5 && iot_path_correction(&feedback[0]) > 3800);
    assert(feedback[1].samples == 0 && iot_path_correction(&feedback[1]) == 0);
    candidates[0].delay_correction_us = iot_path_correction(&feedback[0]);
    assert(iot_choose_initial_path(&probe, 11000, candidates, 2, IOT_ACK_ADAPTIVE, 0) == 1);
    iot_update_path_feedback(&feedback[0], -40000);
    assert(iot_path_correction(&feedback[0]) == 0);

    /* Stage 10 unit checks: BLEST + CWND-aware FAP-MPQUIC + stale-path probing */
    struct iot_path_sample s10[] = {
        {.id = 0, .rtt_us = 10000, .rttvar_us = 1000, .rate_Bps = 2000000,
         .in_flight_bytes = 20000, .cwnd_bytes = 14000, .sampled_us = 800000,
         .rtt_updated_us = 795000, .last_probe_us = 795000},
        {.id = 1, .rtt_us = 30000, .rttvar_us = 2000, .rate_Bps = 1000000,
         .in_flight_bytes = 0, .cwnd_bytes = 24000, .sampled_us = 800000,
         .rtt_updated_us = 500000, .last_probe_us = 500000},
    };
    struct iot_record ev_rec = {.id = 99, .kind = IOT_EVENT, .payload_len = 1200};
    struct iot_record st_rec = {.id = 100, .kind = IOT_STATE, .payload_len = 256};
    /* CWND-blocked primary path 0 should trigger spill in fresh-aware-noprobe / fresh-aware */
    assert(iot_estimate_path_cwnd(&s10[0], 1252, 40000).duration_us > iot_estimate_path(&s10[0], 1252, 40000).duration_us);
    assert(iot_choose_initial_path(&ev_rec, 25000, s10, 2, IOT_FRESH_NOPROBE, 0) == 1);
    assert(iot_choose_initial_path(&ev_rec, 25000, s10, 2, IOT_FRESH_AWARE, 0) == 1);
    /* Non-congested primary path 0 + stale alternate path 1 (age 300ms >= 150ms):
     * state record probes path 1, while urgent event stays on healthy fast path 0. */
    s10[0].in_flight_bytes = 0;
    assert(iot_choose_initial_path(&ev_rec, 50000, s10, 2, IOT_FRESH_AWARE, 0) == 0);
    assert(iot_choose_initial_path(&st_rec, 200000, s10, 2, IOT_FRESH_AWARE, 0) == 1);
    assert(iot_choose_initial_path(&st_rec, 200000, s10, 2, IOT_FRESH_NOPROBE, 0) == 0);
    /* BLEST picks fast unblocked path 0, and spills if fast path is CWND-blocked with bounded X */
    assert(iot_choose_initial_path(&ev_rec, 50000, s10, 2, IOT_BLEST_PATH, 0) == 0);
    s10[0].in_flight_bytes = 13500;
    s10[0].cwnd_bytes = 14000;
    s10[1].rtt_us = 18000;
    assert(iot_choose_initial_path(&ev_rec, 50000, s10, 2, IOT_BLEST_PATH, 0) == 1);

    /* Stage 11 unit checks: FGCA shedding, OCRQ conformal quantiles, JBM-SP joint matching,
     * Peekaboo-UCB, and DaMPS-EDF. */
    struct iot_path_feedback conf_fb = {0};
    /* Positive residuals above q push conformal_q_us upward; negative residuals decay it smoothly. */
    iot_update_conformal_feedback(&conf_fb, 80000.0, 20000.0);
    assert(conf_fb.conformal_q_us > 0.0 && conf_fb.conformal_misses == 1 && conf_fb.conformal_samples == 1);
    double q_after_miss = conf_fb.conformal_q_us;
    iot_update_conformal_feedback(&conf_fb, -5000.0, 20000.0);
    assert(conf_fb.conformal_q_us < q_after_miss && conf_fb.conformal_samples == 2);

    struct iot_path_sample acsp_paths[] = {
        {.id = 0, .rtt_us = 20000, .rttvar_us = 4000, .rate_Bps = 1000000, .cwnd_bytes = 24000,
         .sampled_us = 1000000, .rtt_updated_us = 995000, .last_probe_us = 995000, .conformal_q_us = 90000.0},
        {.id = 1, .rtt_us = 35000, .rttvar_us = 4000, .rate_Bps = 1000000, .cwnd_bytes = 24000,
         .sampled_us = 1000000, .rtt_updated_us = 700000, .last_probe_us = 700000, .conformal_q_us = 0.0},
    };
    /* OCRQ should steer tight-slack event away from path 0 (high conformal tail q=90ms) to path 1. */
    assert(iot_estimate_path_conformal(&acsp_paths[1], 1252, 60000.0).probability >
           iot_estimate_path_conformal(&acsp_paths[0], 1252, 60000.0).probability);
    assert(iot_choose_initial_path(&ev_rec, 60000.0, acsp_paths, 2, IOT_ACSP_CONFORMAL, 0) == 1);

    /* FGCA sheds expired alarms and contention-infeasible alarms, never sheds saveable alarms or states. */
    double best_p = 0.0;
    assert(iot_should_shed_event(&ev_rec, -1000.0, acsp_paths, 2, IOT_ACSP_FULL, false, &best_p));
    assert(iot_should_shed_event(&ev_rec, 1000.0, acsp_paths, 2, IOT_ACSP_FULL, true, &best_p) && best_p < 0.05);
    assert(!iot_should_shed_event(&ev_rec, 100000.0, acsp_paths, 2, IOT_ACSP_FULL, true, &best_p) && best_p > 0.5);
    assert(!iot_should_shed_event(&st_rec, -1000.0, acsp_paths, 2, IOT_ACSP_FULL, true, &best_p));

    /* Peekaboo-UCB explores untried path first, then exploits higher reward path; avoids PTO. */
    struct iot_path_sample ucb_paths[] = {
        {.id = 0, .rtt_us = 10000, .ucb_pulls = 10, .ucb_reward_sum = 2.0, .ucb_total_pulls = 20},
        {.id = 1, .rtt_us = 15000, .ucb_pulls = 10, .ucb_reward_sum = 9.0, .ucb_total_pulls = 20},
    };
    assert(iot_choose_initial_path(&ev_rec, 50000.0, ucb_paths, 2, IOT_PEEKABOO_UCB, 0) == 1);
    ucb_paths[1].pto = 1;
    assert(iot_choose_initial_path(&ev_rec, 50000.0, ucb_paths, 2, IOT_PEEKABOO_UCB, 0) == 0);

    /* DaMPS-EDF minimizes loss-adjusted expected completion time. */
    struct iot_path_sample damps_paths[] = {
        {.id = 0, .rtt_us = 20000, .rate_Bps = 1000000, .loss = 0.50},
        {.id = 1, .rtt_us = 24000, .rate_Bps = 1000000, .loss = 0.01},
    };
    assert(iot_choose_initial_path(&ev_rec, 50000.0, damps_paths, 2, IOT_DAMPS_EDF, 0) == 1);

    /* JBM-SP joint bipartite matching selects saveable urgent alarm on highest-value conformal path. */
    workload.count = workload.next = 0;
    add(IOT_STATE, 1, 0, 200000, 200000, 1);
    add(IOT_EVENT, 2, 0, 80000, 0, 5);
    workload.entries[1].record.severity = 5;
    admit(0);
    admit(0);
    size_t joint_k = 99;
    struct iot_plan jp = iot_plan_joint_acsp(&scheduler, &workload, 10000, acsp_paths, 2,
                                             IOT_ACSP_FULL, 2, 4, &joint_k);
    assert(jp.entry == &workload.entries[1] && joint_k == 1 && jp.path_id == 1);
    workload.count = workload.next = 0;

    add(IOT_STATE, 1, 0, 100000, 100000, 1);
    add(IOT_EVENT, 1, 0, 100000, 0, 4);
    workload.entries[1].record.severity = 4;
    admit(0);
    admit(0);
    struct iot_plan app = iot_plan_record(&scheduler, &workload, 1, NULL, 0);
    assert(app.entry == &workload.entries[1]);
    struct iot_path_sample paths[] = {
        {.id = 0, .rtt_us = 300000, .rate_Bps = 100000},
        {.id = 1, .rtt_us = 20000, .rate_Bps = 100000},
    };
    struct iot_plan with_samples = iot_plan_record(&scheduler, &workload, 1, paths, 2);
    assert(with_samples.entry == app.entry && with_samples.score == app.score);
    iot_plan_path(&app, iot_record_slack(&app.entry->record, 1), paths, 2, false);
    assert(app.path_id == 1 && app.estimate.probability > 0);
    paths[1].in_flight_bytes = 1000000;
    iot_plan_path(&app, iot_record_slack(&app.entry->record, 1), paths, 2, false);
    assert(app.path_id == 0 && app.estimate.probability == 0);
    scheduler.policy = IOT_FIFO;
    assert(iot_select(&scheduler, &workload) == &workload.entries[0]);

    scheduler.policy = IOT_PROPOSED_APP;
    workload.count = workload.next = 0;
    for (size_t i = 0; i < IOT_MAX_RECORDS; i++) {
        add(IOT_EVENT, 1, 0, 100000, 0, 1);
        admit(0);
    }
    workload.entries[IOT_MAX_RECORDS - 1].record.severity = 5;
    assert(iot_plan_record(&scheduler, &workload, 1, NULL, 0).entry ==
           &workload.entries[IOT_MAX_RECORDS - 1]);
}

static void test_receiver(void)
{
    static struct iot_receiver receiver;
    struct iot_record event = {.kind = IOT_EVENT, .source = 1, .serial = 8, .id = 1, .deadline = 3};
    const struct iot_ack *ack = iot_receive(&receiver, &event, 2, 2);
    assert(ack && ack->status == IOT_ACCEPTED && ack->on_time);
    event.source = 2;
    event.id = 2;
    ack = iot_receive(&receiver, &event, 6, 4);
    assert(ack && ack->status == IOT_ACCEPTED && !ack->on_time);
    event.id = 3;
    ack = iot_receive(&receiver, &event, 10, 4);
    assert(ack && ack->status == IOT_DUPLICATE);
    assert(!iot_receive(&receiver, &event, 14, 4));
    struct iot_record state = {.kind = IOT_STATE, .source = 1, .id = 4};
    ack = iot_receive(&receiver, &state, 14, 1);
    assert(ack && ack->status == IOT_UPDATED);
    state.id = 5;
    ack = iot_receive(&receiver, &state, 18, 2);
    assert(ack && ack->status == IOT_STALE);
    state.id = 6;
    state.source = 2;
    ack = iot_receive(&receiver, &state, 22, 2);
    assert(ack && ack->status == IOT_UPDATED);
    iot_free_receiver(&receiver);
}

int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "receiver")) {
        test_receiver();
        return 0;
    }
    assert(argc == 2 && iot_parse_policy(argv[1], &scheduler.policy));
    assert(!strcmp(iot_policy_name(scheduler.policy), argv[1]));
    enum iot_policy invalid;
    assert(!iot_parse_policy("unknown", &invalid));
    switch (scheduler.policy) {
    case IOT_FIFO: test_order((uint32_t[]){1, 2, 3, 4}); break;
    case IOT_PRIORITY: test_order((uint32_t[]){2, 3, 4, 1}); break;
    case IOT_EDF: test_order((uint32_t[]){3, 4, 1, 2}); break;
    case IOT_AOI: test_aoi(); break;
    case IOT_LATEST_RR: test_round_robin(); break;
    case IOT_PROPOSED_APP: test_independent_factors(); return 0;
    case IOT_PROPOSED_STRICT:
    case IOT_PROPOSED_CROSS:
        test_proposed();
        return 0;
    default: return 1;
    }
    assert(!iot_select(&scheduler, &workload));
    return 0;
}
