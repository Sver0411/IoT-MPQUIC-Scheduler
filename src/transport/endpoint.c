#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#include <xquic/xquic.h>
#include <xquic/xqc_errno.h>

#include "iot.h"
#include "path_affinity.h"
#include "scheduler/scheduler.h"

#define ALPN "iot-stage1"
#define IOT_ALPN "iot-stage2"
enum {
    PATHS = 2, PHASES = 2, RECORDS = 64, RECORD_BYTES = 16384, TX_WINDOW = 16,
    IOT_WINDOW = 8, IOT_CREDIT = 4, IOT_SEND_PACKETS = 24,
    IOT_RETRY_MIN_US = 5000, IOT_RETRY_MAX_US = 50000,
};

struct iot_run {
    union {
        struct iot_workload workload;
        struct iot_receiver receiver;
    };
    uint64_t start, retry_at, retry_delay;
    bool window_blocked;
    struct iot_scheduler scheduler;
    struct iot_path_control paths;
};

struct link {
    int fd;
    struct sockaddr_in local, peer;
    uint64_t id, tx, rx, tx_bytes, rx_bytes;
    bool blocked;
};

struct endpoint {
    xqc_engine_t *engine;
    xqc_connection_t *conn;
    xqc_cid_t cid;
    struct link links[PATHS];
    uint64_t timer;
    bool server, handshake, create_path, validated, closing, quic_closing, closed, failed;
    unsigned phase, sent, active_tx, acked, ack_sent;
    unsigned received[PHASES + 1];
    bool seen[PHASES * RECORDS];
    struct iot_run *iot;
    FILE *trace;
};

struct record_stream {
    struct endpoint *ep;
    size_t used, size;
    unsigned id;
    bool sending, writing, fin;
    unsigned char data[IOT_HEADER + IOT_MAX_PAYLOAD + 1];
};

static volatile sig_atomic_t advance_phase;

static void on_signal(int signal_number)
{
    (void)signal_number;
    advance_phase = 1;
}

static uint64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

static void event(const char *name, const char *format, ...)
{
    printf("t_us=%" PRIu64 " event=%s", now_us(), name);
    if (*format) {
        putchar(' ');
        va_list args;
        va_start(args, format);
        vprintf(format, args);
        va_end(args);
    }
    putchar('\n');
}

static void set_timer(xqc_usec_t delay, void *user)
{
    struct endpoint *ep = user;
    ep->timer = now_us() + delay;
}

static void trace_log(xqc_log_level_t level, const void *buf, size_t size, void *user)
{
    (void)level;
    struct endpoint *ep = user;
    fwrite(buf, 1, size, ep->trace);
    fputc('\n', ep->trace);
}

static bool same_address(const struct sockaddr_in *a, const struct sockaddr *b, socklen_t len)
{
    if (len != sizeof(*a) || b->sa_family != AF_INET)
        return false;
    const struct sockaddr_in *v4 = (const struct sockaddr_in *)b;
    return a->sin_addr.s_addr == v4->sin_addr.s_addr && a->sin_port == v4->sin_port;
}

static void address_text(const struct sockaddr_in *addr, char text[22])
{
    char ip[INET_ADDRSTRLEN];
    inet_ntop(AF_INET, &addr->sin_addr, ip, sizeof(ip));
    snprintf(text, 22, "%s:%u", ip, ntohs(addr->sin_port));
}

static void map_link(struct link *link, int index, uint64_t id)
{
    char local[22], peer[22];
    address_text(&link->local, local);
    address_text(&link->peer, peer);
    link->id = id;
    event("path_map", "link=%c path_id=%" PRIu64 " local=%s peer=%s",
          'A' + index, id, local, peer);
}

static ssize_t write_path(uint64_t id, const unsigned char *buf, size_t size,
                          const struct sockaddr *peer, socklen_t len, void *user)
{
    struct endpoint *ep = user;
    for (int i = 0; i < PATHS; i++) {
        struct link *link = &ep->links[i];
        if (link->id != id)
            continue;
        // The pinned client initializes new paths with path A's address.
        // Its connected socket supplies the configured destination for B.
        if ((ep->server || i == 0) && len && !same_address(&link->peer, peer, len)) {
            event("peer_mismatch", "link=%c path_id=%" PRIu64, 'A' + i, id);
            ep->failed = true;
            return XQC_SOCKET_ERROR;
        }
        ssize_t written;
        do {
            written = send(link->fd, buf, size, 0);
        } while (written < 0 && errno == EINTR);
        if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            link->blocked = true;
            return XQC_SOCKET_EAGAIN;
        }
        if (written < 0 && errno == ECONNREFUSED && ep->quic_closing)
            return XQC_SOCKET_ERROR;
        if (written != (ssize_t)size) {
            perror("UDP send");
            ep->failed = true;
            return XQC_SOCKET_ERROR;
        }
        link->tx++;
        link->tx_bytes += size;
        event("udp_tx", "link=%c path_id=%" PRIu64 " bytes=%zu", 'A' + i, id, size);
        return written;
    }
    event("unknown_tx_path", "path_id=%" PRIu64, id);
    ep->failed = true;
    return XQC_SOCKET_ERROR;
}

static ssize_t write_initial(const unsigned char *buf, size_t size,
                             const struct sockaddr *peer, socklen_t len, void *user)
{
    return write_path(0, buf, size, peer, len, user);
}

// The pinned client calls these unconditionally when NEW_TOKEN and TLS tickets arrive.
static void discard_token(const unsigned char *token, uint32_t len, void *user)
{
    (void)token;
    (void)len;
    (void)user;
}

static void discard_ticket(const char *ticket, size_t len, void *user)
{
    (void)ticket;
    (void)len;
    (void)user;
}

static void snapshot(struct endpoint *ep, unsigned phase)
{
    xqc_conn_stats_t stats = xqc_conn_get_stats(ep->engine, &ep->cid);
    event("quic_stats", "phase=%u mp=%d mp_state=%d available=%d lost=%u sent=%u spurious=%u",
          phase, stats.enable_multipath, stats.mp_state,
          xqc_conn_available_paths(ep->engine, &ep->cid), stats.lost_count,
          stats.send_count, stats.spurious_loss_count);
    for (int i = 0; i < XQC_MAX_PATHS_COUNT; i++) {
        const xqc_path_metrics_t *p = &stats.paths_info[i];
        if (p->path_id == UINT64_MAX)
            continue;
        event("quic_path", "phase=%u path_id=%" PRIu64 " tx_packets=%" PRIu64
              " rx_packets=%" PRIu64 " data_tx_bytes=%" PRIu64 " data_rx_bytes=%" PRIu64,
              phase, p->path_id, p->path_pkt_send_count, p->path_pkt_recv_count,
              p->path_send_bytes, p->path_recv_bytes);
    }
}

static xqc_int_t conn_open(xqc_connection_t *conn, const xqc_cid_t *cid, void *user, void *proto)
{
    (void)proto;
    struct endpoint *ep = user;
    if (ep->conn && ep->conn != conn)
        return -1;
    ep->conn = conn;
    ep->cid = *cid;
    if (ep->iot && iot_policy_proposed(ep->iot->scheduler.policy))
        xqc_conn_set_alp_user_data(conn, &ep->iot->paths);
    event("conn_open", "");
    return 0;
}

static xqc_int_t accept_connection(xqc_engine_t *engine, xqc_connection_t *conn,
                                   const xqc_cid_t *cid, void *user)
{
    (void)engine;
    (void)cid;
    struct endpoint *ep = user;
    struct sockaddr_storage local;
    socklen_t len;
    if (ep->conn ||
        xqc_conn_get_local_addr(conn, (struct sockaddr *)&local, sizeof(local), &len) ||
        !same_address(&ep->links[0].local, (struct sockaddr *)&local, len))
        return -1;
    return 0;
}

static xqc_int_t conn_closed(xqc_connection_t *conn, const xqc_cid_t *cid, void *user, void *proto)
{
    (void)cid;
    (void)proto;
    struct endpoint *ep = user;
    if (ep->conn == conn) {
        int error = xqc_conn_get_errno(conn);
        event("conn_closed", "error=%d", error);
        ep->failed |= error != 0;
        ep->conn = NULL;
        ep->closed = true;
    }
    return 0;
}

static xqc_int_t conn_closing(xqc_connection_t *conn, const xqc_cid_t *cid, xqc_int_t error, void *user)
{
    (void)cid;
    struct endpoint *ep = user;
    if (ep->conn == conn) {
        ep->quic_closing = true;
        ep->failed |= error != 0;
    }
    return 0;
}

static void handshake_finished(xqc_connection_t *conn, void *user, void *proto)
{
    (void)conn;
    (void)proto;
    struct endpoint *ep = user;
    ep->handshake = true;
    event("handshake", "");
    snapshot(ep, 0);
}

static void cid_updated(xqc_connection_t *conn, const xqc_cid_t *old_cid,
                        const xqc_cid_t *new_cid, void *user)
{
    (void)conn;
    (void)old_cid;
    struct endpoint *ep = user;
    ep->cid = *new_cid;
}

static void path_ready(const xqc_cid_t *cid, void *user)
{
    (void)cid;
    struct endpoint *ep = user;
    ep->create_path = true;
}

static int path_created(xqc_connection_t *conn, const xqc_cid_t *cid, uint64_t id, void *user)
{
    (void)cid;
    struct endpoint *ep = user;
    struct sockaddr_storage local, peer;
    socklen_t local_len, peer_len;
    struct link *b = &ep->links[1];
    if (!ep->server || (b->id != UINT64_MAX && b->id != id) ||
        xqc_path_get_local_addr(conn, id, (struct sockaddr *)&local, sizeof(local), &local_len) ||
        xqc_path_get_peer_addr(conn, id, (struct sockaddr *)&peer, sizeof(peer), &peer_len) ||
        !same_address(&b->local, (struct sockaddr *)&local, local_len) ||
        !same_address(&b->peer, (struct sockaddr *)&peer, peer_len)) {
        event("invalid_path", "path_id=%" PRIu64, id);
        ep->failed = true;
        return -1;
    }
    map_link(b, 1, id);
    return 0;
}

static void path_removed(const xqc_cid_t *cid, uint64_t id, void *user)
{
    (void)cid;
    (void)user;
    event("path_removed", "path_id=%" PRIu64, id);
}

static xqc_int_t stream_open(xqc_stream_t *stream, void *user)
{
    if (!user) {
        struct endpoint *ep = xqc_get_conn_user_data_by_stream(stream);
        if (xqc_stream_get_direction(stream) != XQC_STREAM_UNI) {
            ep->failed = true;
            return -1;
        }
        struct record_stream *record = calloc(1, sizeof(*record));
        if (!record) {
            ep->failed = true;
            return -1;
        }
        record->ep = ep;
        if (ep->iot)
            record->size = ep->server ? IOT_HEADER + IOT_MAX_PAYLOAD : IOT_ACK_BYTES;
        else
            record->size = ep->server ? RECORD_BYTES : sizeof(uint32_t);
        xqc_stream_set_user_data(stream, record);
    }
    event("stream_open", "stream_id=%" PRIu64 " direction=%s",
          xqc_stream_id(stream), user ? "tx" : "rx");
    return 0;
}

static xqc_int_t stream_write(xqc_stream_t *stream, void *user)
{
    struct record_stream *record = user;
    if (!record || !record->sending || record->writing || record->fin)
        return 0;

    // Raw xqc_stream_send can run connection logic before returning.
    record->writing = true;
    size_t remaining = record->size - record->used;
    ssize_t n = xqc_stream_send(stream, record->data + record->used, remaining, 1);
    record->writing = false;
    if (record->ep->iot && (n == -XQC_EAGAIN || (n >= 0 && (size_t)n < remaining)))
        event("iot_write_backpressure", "stream_id=%" PRIu64 " id=%u offset=%zu offered=%zu result=%zd",
              xqc_stream_id(stream), record->id, record->used, remaining, n);
    if (n == -XQC_EAGAIN)
        return 0;
    if (n < 0) {
        event("stream_write_error", "code=%zd", n);
        record->ep->failed = true;
        return -1;
    }
    record->used += (size_t)n;
    if (record->used == record->size) {
        record->fin = true;
        event("stream_fin_tx", "stream_id=%" PRIu64 " id=%u bytes=%zu",
              xqc_stream_id(stream), record->id, record->size);
    }
    return 0;
}

static unsigned char payload_byte(unsigned id, size_t offset)
{
    return (unsigned char)((id + offset) % 251);
}

static int finish_iot_record(xqc_stream_t *stream, struct record_stream *buffer)
{
    struct endpoint *ep = buffer->ep;
    uint64_t received = now_us();
    uint64_t stream_id = xqc_stream_id(stream);
    if (ep->server) {
        struct iot_record record;
        if (!iot_decode_record(buffer->data, buffer->used, &record))
            return -1;
        const struct iot_ack *ack = iot_receive(&ep->iot->receiver, &record, stream_id, received);
        if (!ack)
            return -1;
        buffer->id = record.id;
        event("iot_rx", "id=%u stream_id=%" PRIu64 " kind=%u source=%u serial=%" PRIu64
              " priority=%u severity=%u payload_len=%u hash=%" PRIu64
              " generated=%" PRIu64 " deadline=%" PRIu64 " maximum_age=%" PRIu64
              " received=%" PRIu64 " status=%u on_time=%d age_exceeded=%d"
              " latest_generated=%" PRIu64 " latest_serial=%" PRIu64,
              record.id, stream_id, record.kind, record.source, record.serial, record.priority,
              record.severity, record.payload_len, iot_payload_hash(&record), record.generated,
              record.deadline, record.maximum_age, ack->received, ack->status, ack->on_time,
              record.kind == IOT_STATE && received - record.generated > record.maximum_age,
              ack->latest_generated, ack->latest_serial);
        return 0;
    }

    struct iot_ack ack;
    struct iot_workload *workload = &ep->iot->workload;
    if (!iot_decode_ack(buffer->data, buffer->used, &ack) || !ack.id || ack.id > workload->count)
        return -1;
    struct iot_entry *entry = &workload->entries[ack.id - 1];
    bool state = entry->record.kind == IOT_STATE;
    if (entry->state != IOT_SUBMITTED || ack.stream_id != entry->stream_id ||
        ack.received < entry->submitted || ack.received > received ||
        ack.on_time != (ack.received <= ep->iot->start + entry->record.deadline) ||
        (ack.status <= IOT_STALE) != state ||
        (state && (!ack.latest_serial || ack.latest_generated < ep->iot->start ||
                   ack.latest_generated > ack.received)) ||
        (!state && (ack.latest_serial || ack.latest_generated)))
        return -1;
    buffer->id = ack.id;
    if (state)
        iot_schedule_feedback(&ep->iot->scheduler, entry->record.source,
                              ack.latest_generated - ep->iot->start, ack.latest_serial);
    entry->state = IOT_ACKED;
    workload->outstanding--;
    event("iot_ack", "id=%u stream_id=%" PRIu64 " ack_stream_id=%" PRIu64
          " submitted=%" PRIu64 " server_received=%" PRIu64 " client_received=%" PRIu64
          " status=%u on_time=%d latest_generated=%" PRIu64 " latest_serial=%" PRIu64,
          ack.id, ack.stream_id, stream_id, entry->submitted, ack.received, received, ack.status,
          ack.on_time, ack.latest_generated, ack.latest_serial);
    return 0;
}

static int finish_record(xqc_stream_t *stream, struct record_stream *record)
{
    struct endpoint *ep = record->ep;
    if (ep->iot)
        return finish_iot_record(stream, record);
    if (record->used != record->size)
        return -1;
    uint32_t wire_id;
    memcpy(&wire_id, record->data, sizeof(wire_id));
    unsigned id = ntohl(wire_id);
    record->id = id;

    if (ep->server) {
        if (id >= PHASES * RECORDS || ep->seen[id])
            return -1;
        for (size_t i = sizeof(wire_id); i < record->size; i++) {
            if (record->data[i] != payload_byte(id, i))
                return -1;
        }
        unsigned phase = id / RECORDS + 1;
        if (phase != ep->ack_sent + 1)
            return -1;
        ep->seen[id] = true;
        event("record_rx", "phase=%u id=%u stream_id=%" PRIu64 " bytes=%zu",
              phase, id, xqc_stream_id(stream), record->size);
        if (++ep->received[phase] == RECORDS)
            event("phase_complete", "phase=%u records=%u", phase, RECORDS);
    } else {
        if (id != ep->phase || id != ep->acked + 1 || ep->sent != RECORDS)
            return -1;
        ep->acked = id;
        event("phase_ack_rx", "phase=%u stream_id=%" PRIu64, id, xqc_stream_id(stream));
    }
    return 0;
}

static xqc_int_t stream_read(xqc_stream_t *stream, void *user)
{
    struct record_stream *record = user;
    if (!record || record->sending)
        return -1;
    while (!record->fin) {
        uint8_t fin = 0;
        ssize_t n = xqc_stream_recv(stream, record->data + record->used,
                                    record->size + 1 - record->used, &fin);
        if (n == -XQC_EAGAIN)
            return 0;
        if (n < 0)
            break;
        record->used += (size_t)n;
        if (record->used > record->size)
            break;
        if (fin) {
            if (finish_record(stream, record))
                break;
            record->fin = true;
            return 0;
        }
        if (n == 0)
            return 0;
    }
    if (record->fin)
        return 0;
    event("invalid_stream", "stream_id=%" PRIu64 " bytes=%zu",
          xqc_stream_id(stream), record->used);
    record->ep->failed = true;
    return -1;
}

static xqc_int_t stream_closed(xqc_stream_t *stream, void *user)
{
    struct record_stream *record = user;
    if (record) {
        record->ep->failed |= !record->fin;
        if (record->sending)
            record->ep->active_tx--;
        event("stream_closed", "stream_id=%" PRIu64 " id=%u fin=%d direction=%s active_tx=%u",
              xqc_stream_id(stream), record->id, record->fin, record->sending ? "tx" : "rx",
              record->ep->active_tx);
        free(record);
    }
    return 0;
}

static struct record_stream *new_tx(struct endpoint *ep, unsigned id)
{
    struct record_stream *record = calloc(1, sizeof(*record));
    if (!record) {
        perror("allocating stream");
        ep->failed = true;
        return NULL;
    }
    record->ep = ep;
    record->id = id;
    record->sending = true;
    return record;
}

static xqc_stream_t *open_tx(struct endpoint *ep, struct record_stream *record)
{
    xqc_stream_t *stream = xqc_stream_create_with_direction(ep->conn, XQC_STREAM_UNI, record);
    if (!stream) {
        // The public API also returns NULL when cumulative peer credit is exhausted.
        free(record);
        return NULL;
    }
    ep->active_tx++;
    return stream;
}

static bool queue_stream(struct endpoint *ep, unsigned id, size_t size)
{
    struct record_stream *record = new_tx(ep, id);
    if (!record)
        return false;
    record->size = size;
    uint32_t wire_id = htonl(id);
    memcpy(record->data, &wire_id, sizeof(wire_id));
    for (size_t i = sizeof(wire_id); i < size; i++)
        record->data[i] = payload_byte(id, i);

    xqc_stream_t *stream = open_tx(ep, record);
    if (!stream)
        return false;
    stream_write(stream, record);
    return true;
}

static bool iot_tx_slot(struct endpoint *ep)
{
    bool blocked = ep->active_tx >= IOT_WINDOW;
    if (ep->iot->window_blocked != blocked)
        event("iot_window", "blocked=%d active_tx=%u limit=%d", blocked, ep->active_tx, IOT_WINDOW);
    ep->iot->window_blocked = blocked;
    return !blocked;
}

static xqc_stream_t *open_iot_tx(struct endpoint *ep, struct record_stream *record)
{
    struct iot_run *iot = ep->iot;
    unsigned id = record->id;
    xqc_stream_t *stream = open_tx(ep, record);
    if (!stream) {
        if (!iot->retry_delay)
            event("iot_stream_create", "blocked=1 id=%u active_tx=%u", id, ep->active_tx);
        // Each failed credit check in the pinned XQUIC can enqueue STREAMS_BLOCKED.
        uint64_t delay = iot->retry_delay ? iot->retry_delay * 2 : IOT_RETRY_MIN_US;
        iot->retry_delay = delay < IOT_RETRY_MAX_US ? delay : IOT_RETRY_MAX_US;
        iot->retry_at = now_us() + iot->retry_delay;
    } else if (iot->retry_delay) {
        event("iot_stream_create", "blocked=0 id=%u active_tx=%u", id, ep->active_tx);
        iot->retry_delay = iot->retry_at = 0;
    }
    return stream;
}

static void progress_iot(struct endpoint *ep)
{
    if (ep->server) {
        if (!ep->validated && xqc_conn_available_paths(ep->engine, &ep->cid) == PATHS) {
            ep->validated = true;
            event("paths_validated", "count=%d", PATHS);
        }
        struct iot_receiver *receiver = &ep->iot->receiver;
        while (!ep->failed && receiver->next < receiver->count && iot_tx_slot(ep) &&
               now_us() >= ep->iot->retry_at) {
            const struct iot_ack *ack = &receiver->acks[receiver->next];
            struct record_stream *buffer = new_tx(ep, ack->id);
            if (!buffer)
                return;
            buffer->size = IOT_ACK_BYTES;
            iot_encode_ack(buffer->data, ack);
            xqc_stream_t *stream = open_iot_tx(ep, buffer);
            if (!stream)
                return;
            receiver->next++;
            stream_write(stream, buffer);
        }
        return;
    }

    struct iot_workload *workload = &ep->iot->workload;
    struct iot_scheduler *scheduler = &ep->iot->scheduler;
    bool proposed = iot_policy_proposed(scheduler->policy);
    if (!ep->iot->start) {
        ep->iot->start = now_us();
        event("iot_start", "origin=%" PRIu64 " records=%zu tx_window=%d initial_uni_credit=%d send_packets=%d policy=%s",
              ep->iot->start, workload->count, IOT_WINDOW, IOT_CREDIT, IOT_SEND_PACKETS,
              iot_policy_name(scheduler->policy));
        if (proposed)
            event("iot_recovery_config", "k=%u event_burst=%u", scheduler->recovery_k, IOT_EVENT_BURST);
        if (proposed)
            event("iot_ablation", "name=%s", iot_ablation_name(scheduler->ablation));
    }
    uint64_t elapsed = now_us() - ep->iot->start;
    size_t index;
    uint32_t superseded;
    bool replace = iot_policy_replaces(scheduler->policy) && scheduler->ablation != IOT_NO_REPLACE;
    while (iot_admit(workload, elapsed, replace, &index, &superseded)) {
        const struct iot_record *record = &workload->entries[index].record;
        event("iot_input", "id=%u kind=%u source=%u serial=%" PRIu64 " release=%" PRIu64,
              record->id, record->kind, record->source, record->serial, workload->entries[index].release);
        if (superseded)
            event("iot_replace", "input=%u dropped=%u source=%u", record->id, superseded, record->source);
    }
    if (proposed) {
        for (size_t i = 0; i < workload->next; i++) {
            struct iot_entry *entry = &workload->entries[i];
            if (entry->state == IOT_PENDING && iot_obsolete(scheduler, &entry->record)) {
                entry->state = IOT_OBSOLETE;
                event("iot_obsolete", "id=%u source=%u", entry->record.id, entry->record.source);
            }
        }
    }
    while (!ep->failed && iot_pending(workload) && iot_tx_slot(ep) &&
           now_us() >= ep->iot->retry_at) {
        struct iot_plan plan = {0};
        struct iot_entry *entry;
        struct iot_path_sample paths[PATHS];
        size_t count = 0;
        uint64_t decision_us = now_us() - ep->iot->start;
        if (proposed) {
            count = iot_path_samples(ep->conn, paths, PATHS);
            plan = iot_plan_record(scheduler, workload, decision_us, paths, count);
            entry = plan.entry;
        } else {
            entry = iot_select(scheduler, workload);
        }
        if (!entry)
            break;
        struct iot_record record = entry->record;
        record.generated += ep->iot->start;
        record.deadline += ep->iot->start;
        struct record_stream *buffer = new_tx(ep, record.id);
        if (!buffer)
            return;
        buffer->size = iot_encode_record(buffer->data, &record);
        xqc_stream_t *stream = open_iot_tx(ep, buffer);
        if (!stream)
            return;
        // Freeze the queue entry only after stream creation succeeds, before the first write.
        entry->stream_id = xqc_stream_id(stream);
        entry->submitted = now_us();
        entry->state = IOT_SUBMITTED;
        workload->outstanding++;
        if (proposed) {
            uint64_t due = record.deadline;
            if (record.kind == IOT_STATE && record.maximum_age < due - record.generated)
                due = record.generated + record.maximum_age;
            ep->iot->paths.bindings[entry->stream_id / 4] =
                (struct iot_binding){plan.path_id, due, record.id};
            for (size_t i = 0; i < count; i++) {
                const struct iot_path_sample *path = &paths[i];
                event("iot_path_sample", "id=%u path_id=%" PRIu64 " rtt_us=%" PRIu64
                      " rttvar_us=%" PRIu64 " rate_Bps=%" PRIu64 " queued_bytes=%" PRIu64 " loss=%.17g pto=%u",
                      record.id, path->id, path->rtt_us, path->rttvar_us, path->rate_Bps,
                      path->queued_bytes, path->loss, path->pto);
            }
            event("iot_plan", "id=%u stream_id=%" PRIu64 " class=%d home=%" PRIu64
                  " decision_us=%" PRIu64 " estimate_us=%.6f probability=%.9f score=%.9f normal=%u events=%u",
                  record.id, entry->stream_id, plan.category, plan.path_id, decision_us,
                  plan.estimate.duration_us, plan.estimate.probability, plan.score,
                  scheduler->normal_since_recovery, scheduler->event_streak);
            iot_commit_plan(scheduler, plan.category);
        }
        iot_schedule_commit(scheduler, &entry->record);
        event("iot_submit", "id=%u stream_id=%" PRIu64 " submitted=%" PRIu64
              " expired=%d active_tx=%u", record.id, entry->stream_id, entry->submitted,
              entry->submitted > record.deadline, ep->active_tx);
        stream_write(stream, buffer);
    }
    if (workload->next == workload->count && !iot_pending(workload) &&
        !workload->outstanding && !ep->active_tx && !ep->failed) {
        snapshot(ep, 1);
        event("iot_complete", "records=%zu", workload->count);
        ep->closing = true;
        ep->failed |= xqc_conn_close(ep->engine, &ep->cid) != 0;
    }
}

static void progress(struct endpoint *ep)
{
    if (!ep->conn || !ep->handshake || ep->closing || ep->quic_closing)
        return;
    if (ep->server) {
        if (ep->iot) {
            progress_iot(ep);
            return;
        }
        unsigned phase = ep->ack_sent + 1;
        if (phase <= PHASES && ep->received[phase] == RECORDS) {
            if (queue_stream(ep, phase, sizeof(uint32_t))) {
                snapshot(ep, phase);
                ep->ack_sent++;
            }
        }
        return;
    }

    if (ep->create_path && ep->links[1].id == UINT64_MAX) {
        uint64_t id;
        ep->create_path = false;
        int rc = xqc_conn_create_path(ep->engine, &ep->cid, &id, 0);
        if (rc == -XQC_EMP_NO_AVAIL_PATH_ID)
            return; // The next CID notification re-enables the attempt.
        if (rc != 0) {
            event("path_create_error", "code=%d", rc);
            ep->failed = true;
            return;
        }
        map_link(&ep->links[1], 1, id);
    }
    if (!ep->validated) {
        if (xqc_conn_available_paths(ep->engine, &ep->cid) != PATHS)
            return;
        ep->validated = true;
        event("paths_validated", "count=%d", PATHS);
        snapshot(ep, 0);
    }
    if (ep->iot) {
        progress_iot(ep);
        return;
    }
    if (ep->phase && ep->acked == ep->phase) {
        if (ep->sent == RECORDS) {
            snapshot(ep, ep->phase);
            event("phase_acked", "phase=%u records=%u", ep->phase, RECORDS);
            ep->sent = 0;
        }
        if (ep->acked == PHASES) {
            ep->closing = true;
            ep->failed |= xqc_conn_close(ep->engine, &ep->cid) != 0;
            return;
        }
        if (!advance_phase)
            return;
    }
    if (!ep->phase || ep->phase == ep->acked) {
        ep->phase++;
        event("phase_start", "phase=%u records=%u record_bytes=%u", ep->phase, RECORDS, RECORD_BYTES);
    }
    while (ep->conn && !ep->failed && ep->sent < RECORDS && ep->active_tx < TX_WINDOW) {
        unsigned id = (ep->phase - 1) * RECORDS + ep->sent;
        if (!queue_stream(ep, id, RECORD_BYTES))
            break;
        ep->sent++;
    }
}

static int open_link(struct link *link, bool server, int index)
{
    const char *client_ips[] = {"10.47.1.1", "10.47.2.1"};
    const char *server_ips[] = {"10.47.1.2", "10.47.2.2"};
    link->local.sin_family = link->peer.sin_family = AF_INET;
    link->local.sin_port = htons(server ? 4433 : 4434);
    link->peer.sin_port = htons(server ? 4434 : 4433);
    inet_pton(AF_INET, server ? server_ips[index] : client_ips[index], &link->local.sin_addr);
    inet_pton(AF_INET, server ? client_ips[index] : server_ips[index], &link->peer.sin_addr);
    link->fd = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (link->fd < 0 ||
        bind(link->fd, (struct sockaddr *)&link->local, sizeof(link->local)) < 0 ||
        connect(link->fd, (struct sockaddr *)&link->peer, sizeof(link->peer)) < 0) {
        char local[22];
        address_text(&link->local, local);
        fprintf(stderr, "UDP setup for link %c (%s): %s\n", 'A' + index, local, strerror(errno));
        return -1;
    }
    return 0;
}

static void receive_packets(struct endpoint *ep, int index)
{
    struct link *link = &ep->links[index];
    unsigned char buf[65536];
    for (int n = 0; n < 64; n++) {
        ssize_t bytes = recv(link->fd, buf, sizeof(buf), 0);
        if (bytes < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            break;
        if (bytes < 0 && errno == ECONNREFUSED && ep->quic_closing)
            break;
        if (bytes < 0) {
            perror("UDP recv");
            ep->failed = true;
            break;
        }
        uint64_t received_at = now_us();
        link->rx++;
        link->rx_bytes += (uint64_t)bytes;
        int rc = xqc_engine_packet_process(ep->engine, buf, (size_t)bytes,
                   (struct sockaddr *)&link->local, sizeof(link->local),
                   (struct sockaddr *)&link->peer, sizeof(link->peer), received_at, ep);
        event("udp_rx", "link=%c path_id=%" PRIu64 " bytes=%zd recv_us=%" PRIu64 " rc=%d",
              'A' + index, link->id, bytes, received_at, rc);
    }
    xqc_engine_finish_recv(ep->engine);
}

static void run(struct endpoint *ep)
{
    const uint64_t deadline = now_us() + 60000000;
    while (!ep->closed && !ep->failed && now_us() < deadline) {
        if (now_us() >= ep->timer) {
            ep->timer = UINT64_MAX;
            xqc_engine_main_logic(ep->engine);
        }
        progress(ep);
        if (ep->closed || ep->failed)
            break;
        struct pollfd fds[PATHS];
        for (int i = 0; i < PATHS; i++) {
            fds[i].fd = ep->links[i].fd;
            fds[i].events = POLLIN | (ep->links[i].blocked ? POLLOUT : 0);
        }
        uint64_t now = now_us();
        uint64_t delay = ep->timer > now ? ep->timer - now : 0;
        int timeout_ms = delay < 5000 ? (int)((delay + 999) / 1000) : 5;
        if (poll(fds, PATHS, timeout_ms) < 0) {
            if (errno == EINTR)
                continue;
            perror("poll");
            ep->failed = true;
            break;
        }
        for (int i = 0; i < PATHS; i++) {
            if (fds[i].revents & (POLLIN | POLLERR))
                receive_packets(ep, i);
            if (fds[i].revents & POLLOUT) {
                ep->links[i].blocked = false;
                if (ep->conn)
                    xqc_conn_continue_send_by_conn(ep->conn);
            }
        }
    }
    if (!ep->closed && !ep->failed)
        event("timeout", "phase=%u acked=%u ack_sent=%u", ep->phase, ep->acked, ep->ack_sent);
    ep->failed |= !ep->closed;
    if (ep->iot) {
        if (ep->server) {
            const struct iot_receiver *receiver = &ep->iot->receiver;
            ep->failed |= !ep->validated || !receiver->count || receiver->count != receiver->next;
        } else {
            ep->failed |= !ep->closing;
        }
    } else {
        ep->failed |= ep->server ? ep->ack_sent != PHASES : ep->acked != PHASES;
    }
    for (int i = 0; i < PATHS; i++) {
        const struct link *link = &ep->links[i];
        event("udp_summary", "link=%c path_id=%" PRIu64 " tx=%" PRIu64 " rx=%" PRIu64
              " tx_bytes=%" PRIu64 " rx_bytes=%" PRIu64,
              'A' + i, link->id, link->tx, link->rx, link->tx_bytes, link->rx_bytes);
    }
}

int main(int argc, char **argv)
{
    uint64_t process_start = now_us();
    const char *role = argc > 1 ? argv[1] : "";
    bool iot = !strncmp(role, "iot-", 4);
    if (iot)
        role += 4;
    bool server = !strcmp(role, "server");
    if ((server && argc != 5) ||
        (!server && (strcmp(role, "client") || (iot ? (argc < 4 || argc > 7) : argc != 3)))) {
        fprintf(stderr, "Usage: %s server TRACE CERT KEY | client TRACE | "
                        "iot-server TRACE CERT KEY | iot-client TRACE WORKLOAD [POLICY [RECOVERY_K [ABLATION]]]\n", argv[0]);
        return 2;
    }
    enum iot_policy policy = IOT_FIFO_REPLACE;
    if (iot && !server && argc >= 5 && !iot_parse_policy(argv[4], &policy)) {
        fprintf(stderr, "Unknown IoT scheduler: %s\n", argv[4]);
        return 2;
    }
    unsigned recovery_k = IOT_DEFAULT_RECOVERY_K;
    if (iot && !server && argc >= 6) {
        char *end;
        unsigned long value = strtoul(argv[5], &end, 10);
        if (!iot_policy_proposed(policy) || *end || value < 1 || value > IOT_MAX_RECOVERY_K) {
            fprintf(stderr, "RECOVERY_K requires a proposed policy and an integer from 1 to %d\n",
                    IOT_MAX_RECOVERY_K);
            return 2;
        }
        recovery_k = (unsigned)value;
    }
    enum iot_ablation ablation = IOT_FULL;
    if (argc == 7 && (policy != IOT_PROPOSED_CROSS || !iot_parse_ablation(argv[6], &ablation))) {
        fprintf(stderr, "ABLATION requires proposed-cross and one documented variant\n");
        return 2;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    struct sigaction action = {.sa_handler = on_signal};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGUSR1, &action, NULL) < 0) {
        perror("sigaction");
        return 1;
    }

    struct endpoint ep = {.server = server, .timer = UINT64_MAX};
    const char *alpn = iot ? IOT_ALPN : ALPN;
    for (int i = 0; i < PATHS; i++)
        ep.links[i].fd = -1;
    if (iot) {
        ep.iot = calloc(1, sizeof(*ep.iot));
        if (!ep.iot) {
            perror("allocating IoT application state");
            goto fail;
        }
        ep.iot->scheduler.policy = policy;
        ep.iot->scheduler.ablation = ablation;
        ep.iot->scheduler.recovery_k = recovery_k;
        ep.iot->paths.cross = policy == IOT_PROPOSED_CROSS && ablation != IOT_NO_CROSS;
        if (!server && !iot_load_workload(&ep.iot->workload, argv[3]))
            goto fail;
    }
    ep.trace = fopen(argv[2], "w");
    if (!ep.trace) {
        perror("opening XQUIC trace");
        goto fail;
    }
    for (int i = 0; i < PATHS; i++) {
        ep.links[i].id = UINT64_MAX;
        if (open_link(&ep.links[i], server, i) < 0)
            goto fail;
    }
    map_link(&ep.links[0], 0, 0);

    xqc_engine_type_t type = server ? XQC_ENGINE_SERVER : XQC_ENGINE_CLIENT;
    xqc_config_t config;
    if (xqc_engine_get_default_config(&config, type) != 0) {
        fprintf(stderr, "Cannot read XQUIC engine defaults\n");
        goto fail;
    }
    config.cfg_log_level = XQC_LOG_INFO;
    xqc_engine_ssl_config_t tls = {0};
    if (server) {
        tls.cert_file = argv[3];
        tls.private_key_file = argv[4];
    }
    xqc_engine_callback_t callbacks = {
        .set_event_timer = set_timer,
        .monotonic_ts = now_us,
        .log_callbacks = {.xqc_log_write_err = trace_log, .xqc_log_write_stat = trace_log},
    };
    xqc_transport_callbacks_t transport = {
        .server_accept = server ? accept_connection : NULL,
        .write_socket = write_initial,
        .write_socket_ex = write_path,
        .save_token = discard_token,
        .save_session_cb = discard_ticket,
        .conn_update_cid_notify = cid_updated,
        .conn_closing = conn_closing,
        .ready_to_create_path_notify = path_ready,
        .path_created_notify = server ? path_created : NULL,
        .path_removed_notify = path_removed,
    };
    xqc_app_proto_callbacks_t app = {
        .conn_cbs = {
            .conn_create_notify = conn_open,
            .conn_close_notify = conn_closed,
            .conn_handshake_finished = handshake_finished,
        },
        .stream_cbs = {
            .stream_create_notify = stream_open,
            .stream_read_notify = stream_read,
            .stream_write_notify = stream_write,
            .stream_close_notify = stream_closed,
        },
    };
    xqc_conn_settings_t settings = {
        .proto_version = XQC_VERSION_V1,
        .enable_multipath = 1,
        .multipath_version = XQC_MULTIPATH_10,
        .init_max_path_id = 1,
        .max_streams_uni = iot ? IOT_CREDIT : 32,
        .sndq_packets_used_max = iot ? IOT_SEND_PACKETS : 0,
        .scheduler_callback = iot_policy_proposed(policy) ? iot_affinity_callback : xqc_minrtt_scheduler_cb,
        .cong_ctrl_callback = xqc_cubic_cb,
        .pacing_on = 1,
    };
    ep.engine = xqc_engine_create(type, &config, &tls, &callbacks, &transport, &ep);
    if (!ep.engine || xqc_engine_register_alpn(ep.engine, alpn, strlen(alpn), &app, NULL)) {
        fprintf(stderr, "Cannot initialize XQUIC engine or register ALPN\n");
        goto fail;
    }
    event("ready", "role=%s", server ? "server" : "client");
    if (server) {
        xqc_server_set_conn_settings(ep.engine, &settings);
    } else {
        // Isolated lab uses a throwaway self-signed certificate and does not authenticate its peer.
        xqc_conn_ssl_config_t conn_tls = {0};
        const xqc_cid_t *cid = xqc_connect(ep.engine, &settings, NULL, 0, alpn, 0, &conn_tls,
                                          (struct sockaddr *)&ep.links[0].peer,
                                          sizeof(ep.links[0].peer), alpn, &ep);
        if (!cid) {
            fprintf(stderr, "Cannot create XQUIC connection\n");
            goto fail;
        }
        ep.cid = *cid;
    }

    run(&ep);
    goto cleanup;

fail:
    ep.failed = true;
cleanup:
    if (ep.engine)
        xqc_engine_destroy(ep.engine);
    for (int i = 0; i < PATHS; i++)
        if (ep.links[i].fd >= 0)
            close(ep.links[i].fd);
    if (ep.trace && fclose(ep.trace) != 0) {
        perror("writing XQUIC trace");
        ep.failed = true;
    }
    if (ep.iot) {
        if (server)
            iot_free_receiver(&ep.iot->receiver);
        else
            iot_free_workload(&ep.iot->workload);
        free(ep.iot);
    }
    struct rusage cpu;
    if (getrusage(RUSAGE_SELF, &cpu) == 0) {
        uint64_t user_us = (uint64_t)cpu.ru_utime.tv_sec * 1000000 + cpu.ru_utime.tv_usec;
        uint64_t sys_us = (uint64_t)cpu.ru_stime.tv_sec * 1000000 + cpu.ru_stime.tv_usec;
        event("process_cpu", "user_us=%" PRIu64 " sys_us=%" PRIu64 " wall_us=%" PRIu64,
              user_us, sys_us, now_us() - process_start);
    }
    event("exit", "ok=%d", !ep.failed);
    return ep.failed ? 1 : 0;
}
