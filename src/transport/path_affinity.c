#include "path_affinity.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>

#include "src/common/xqc_time.h"
#include "src/transport/scheduler/xqc_scheduler_common.h"
#include "src/transport/xqc_conn.h"
#include "src/transport/xqc_multipath.h"
#include "src/transport/xqc_packet_out.h"
#include "src/transport/xqc_send_ctl.h"

static bool path_available(const xqc_path_ctx_t *path)
{
    return path->path_state == XQC_PATH_STATE_ACTIVE &&
           path->app_path_status != XQC_APP_PATH_STATUS_FROZEN;
}

static struct iot_path_sample path_sample(xqc_path_ctx_t *path, double loss_scale)
{
    xqc_send_ctl_t *ctl = path->path_send_ctl;
    uint64_t sent = (uint64_t)ctl->ctl_recent_send_count[0] + ctl->ctl_recent_send_count[1];
    uint64_t lost = (uint64_t)ctl->ctl_recent_lost_count[0] + ctl->ctl_recent_lost_count[1];
    double loss = fmin(0.95, (lost + 1.0) / (sent + 20.0));
    return (struct iot_path_sample){
        .id = path->path_id,
        .rtt_us = xqc_send_ctl_get_srtt(ctl),
        .rttvar_us = ctl->ctl_rttvar,
        .rate_Bps = xqc_send_ctl_get_est_bw(ctl),
        .queued_bytes = path->path_schedule_bytes,
        .in_flight_bytes = ctl->ctl_bytes_in_flight,
        .recent_sent = sent,
        .recent_lost = lost,
        .loss = fmin(0.95, loss * loss_scale),
        .observed_loss = loss,
        .pto = ctl->ctl_pto_count,
    };
}

size_t iot_path_samples(xqc_connection_t *conn, struct iot_path_sample *samples, size_t capacity,
                        double loss_scale)
{
    size_t count = 0;
    xqc_list_head_t *pos, *next;
    xqc_list_for_each_safe(pos, next, &conn->conn_paths_list) {
        xqc_path_ctx_t *path = xqc_list_entry(pos, xqc_path_ctx_t, path_list);
        if (path_available(path) && count < capacity)
            samples[count++] = path_sample(path, loss_scale);
    }
    return count;
}

static xqc_path_ctx_t *choose_path(void *state, xqc_connection_t *conn, xqc_packet_out_t *packet,
                                  int check_cwnd, int reinject, xqc_bool_t *blocked)
{
    struct iot_path_control *control = conn->proto_data;
    if (!control || !(packet->po_frame_types & XQC_FRAME_BIT_STREAM))
        return xqc_minrtt_scheduler_cb.xqc_scheduler_get_path(state, conn, packet, check_cwnd, reinject, blocked);

    // The pinned packetizer coalesces STREAM frames only for the same stream.
    uint64_t sid = packet->po_stream_id;
    if (blocked)
        *blocked = XQC_FALSE;
    if (sid % 4 != 2 || sid / 4 >= IOT_MAX_RECORDS || !control->bindings[sid / 4].id)
        return NULL;
    const struct iot_binding *binding = &control->bindings[sid / 4];
    uint64_t now = xqc_monotonic_timestamp();
    bool repair = packet->po_origin != NULL;
    double slack = (double)binding->due_us - now;
    xqc_path_ctx_t *home = xqc_conn_find_path_by_path_id(conn, binding->home);
    bool home_available = home && path_available(home);
    bool home_ready = home_available && xqc_scheduler_check_path_can_send(home, packet, check_cwnd);
    xqc_path_ctx_t *chosen = home_ready ? home : NULL;
    const char *cause = !home_available ? "unavailable" : !home_ready ? "cwnd" : "none";
    unsigned home_pto = 0;
    double home_time = 0;

    if (repair && control->cross) {
        struct iot_path_sample original = {0};
        struct iot_estimate home_est = {0};
        if (home_available) {
            original = path_sample(home, 1);
            home_est = iot_estimate_path(&original, packet->po_used_size, slack);
            home_pto = original.pto;
            home_time = home_est.duration_us;
            if (home_ready)
                cause = home_pto ? "pto" : home_time >= slack ? "budget" : "none";
        }
        if (!home_ready || original.pto || home_est.duration_us >= slack) {
            double best_time = home_ready ? home_est.duration_us : INFINITY;
            double best_probability = home_ready ? home_est.probability : -1;
            xqc_list_head_t *pos, *next;
            xqc_list_for_each_safe(pos, next, &conn->conn_paths_list) {
                xqc_path_ctx_t *path = xqc_list_entry(pos, xqc_path_ctx_t, path_list);
                // A PTO probe can bypass its own path's cwnd, not the alternate path's.
                if (path == home || !path_available(path) ||
                    !xqc_scheduler_check_path_can_send(path, packet, 1))
                    continue;
                struct iot_path_sample alt = path_sample(path, 1);
                struct iot_estimate estimate = iot_estimate_path(&alt, packet->po_used_size, slack);
                bool healthier = home_ready && original.pto > 0 && alt.pto == 0;
                if (healthier || estimate.probability > best_probability ||
                    (estimate.probability == best_probability && estimate.duration_us < best_time)) {
                    chosen = path;
                    best_time = estimate.duration_us;
                    best_probability = estimate.probability;
                }
            }
        }
    }
    if (blocked)
        *blocked = chosen == NULL && home_available;
    if (control->integrity) {
        packet->po_iot_decision = chosen ? ++control->decisions : 0;
        if (chosen)
            iot_trace_event("iot91_choice", "choice=%" PRIu64 " id=%u stream=%" PRIu64
                            " home=%" PRIu64 " path=%" PRIu64 " repair=%d cause=%s home_ready=%d"
                            " home_pto=%u home_time=%.17g slack=%.0f",
                            packet->po_iot_decision, binding->id, sid, binding->home, chosen->path_id,
                            repair, cause, home_ready, home_pto, home_time, slack);
    }
    if (chosen)
        iot_trace_event("iot_packet_path", "id=%u stream_id=%" PRIu64 " offset=%" PRIu64
                        " home=%" PRIu64 " chosen=%" PRIu64 " repair=%d",
                        binding->id, sid, packet->po_stream_offset, binding->home, chosen->path_id, repair);
    return chosen;
}

static size_t scheduler_size(void)
{
    return 0;
}

static void scheduler_init(void *state, xqc_log_t *log, xqc_scheduler_params_t *params)
{
    (void)state;
    (void)log;
    (void)params;
}

const xqc_scheduler_callback_t iot_affinity_callback = {
    .xqc_scheduler_size = scheduler_size,
    .xqc_scheduler_init = scheduler_init,
    .xqc_scheduler_get_path = choose_path,
    .xqc_scheduler_schedule_stream_pto = 1,
};
