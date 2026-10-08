/* Small single-threaded ABI around libslirp. Called only on emulator time
 * callbacks; poll never blocks, and guest frames stay in a bounded queue.
 */
#include <libslirp.h>
#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <poll.h>
#endif
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

#ifdef _WIN32
#define ARCS_NET_API __declspec(dllexport)
/* Winsock rejects POLLPRI; out-of-band data uses POLLRDBAND. */
#define NET_POLL_IN POLLRDNORM
#define NET_POLL_PRI POLLRDBAND
#else
#define ARCS_NET_API
#define NET_POLL_IN POLLIN
#define NET_POLL_PRI POLLPRI
#endif

#define FRAME_CAP 2048
#define QUEUE_CAP 256
#define POLL_CAP 256
struct frame { int length; unsigned char bytes[FRAME_CAP]; };
struct bridge;
struct bridge_timer {
    struct bridge_timer *next;
    SlirpTimerCb callback;
    void *opaque;
    int64_t expiry;
};
struct bridge {
    Slirp *slirp;
    int64_t now;
    struct bridge_timer *timers;
    struct frame frames[QUEUE_CAP];
    unsigned head, count;
    uint64_t dropped;
    struct pollfd fds[POLL_CAP];
    unsigned fd_count;
    char error[256];
};
static void fail(struct bridge *b, const char *message) {
    if(!b->error[0]) snprintf(b->error, sizeof b->error, "%s", message);
}
static slirp_ssize_t send_packet(const void *data, size_t length, void *opaque) {
    struct bridge *b = opaque;
    if(length > FRAME_CAP || length < 14) { fail(b,"Invalid Ethernet frame from libslirp"); return -1; }
    if(b->count == QUEUE_CAP) { b->dropped++; return (slirp_ssize_t)length; }
    struct frame *frame = &b->frames[(b->head + b->count++) % QUEUE_CAP];
    frame->length = (int)length; memcpy(frame->bytes, data, length); return (slirp_ssize_t)length;
}
static void guest_error(const char *message, void *opaque) { fail(opaque,message); }
static int64_t clock_ns(void *opaque) { return ((struct bridge *)opaque)->now; }
static void *timer_new(SlirpTimerCb callback, void *cb_opaque, void *opaque) {
    struct bridge *b = opaque;
    struct bridge_timer *timer = calloc(1,sizeof *timer);
    if(!timer) { fail(b,"Cannot allocate libslirp timer"); return NULL; }
    timer->callback=callback; timer->opaque=cb_opaque; timer->expiry=-1;
    timer->next=b->timers; b->timers=timer; return timer;
}
static void timer_free(void *value, void *opaque) {
    struct bridge *b=opaque; struct bridge_timer *timer=value, **at=&b->timers;
    while(*at && *at != timer) at=&(*at)->next;
    if(*at) { *at=timer->next; free(timer); }
}
static void timer_mod(void *value,int64_t expiry,void *opaque) {
    (void)opaque; ((struct bridge_timer *)value)->expiry=expiry;
}
static void register_socket(slirp_os_socket fd,void *opaque) { (void)fd; (void)opaque; }
static void notify(void *opaque) { (void)opaque; } /* periodic nonblocking pump */
static const SlirpCb callbacks = {
    .send_packet=send_packet,.guest_error=guest_error,.clock_get_ns=clock_ns,
    .timer_new=timer_new,.timer_free=timer_free,.timer_mod=timer_mod,.notify=notify,
    .register_poll_socket=register_socket,.unregister_poll_socket=register_socket,
};
ARCS_NET_API void *arcs_net_create(int allow_host_loopback) {
#ifdef _WIN32
    WSADATA data;
    if (WSAStartup(MAKEWORD(2, 2), &data)) { return NULL; }
#endif
    struct bridge *b=calloc(1,sizeof *b);
    if (!b) {
#ifdef _WIN32
        WSACleanup();
#endif
        return NULL;
    }
    SlirpConfig config={0}; config.version=6; config.in_enabled=true;
    inet_pton(AF_INET,"192.0.2.0",&config.vnetwork);
    inet_pton(AF_INET,"255.255.255.0",&config.vnetmask);
    inet_pton(AF_INET,"192.0.2.1",&config.vhost);
    inet_pton(AF_INET,"192.0.2.2",&config.vdhcp_start);
    inet_pton(AF_INET,"192.0.2.3",&config.vnameserver);
    config.if_mtu=1500; config.if_mru=1500;
    config.disable_host_loopback=!allow_host_loopback;
    b->slirp=slirp_new(&config,&callbacks,b);
    if (!b->slirp) {
        free(b);
#ifdef _WIN32
        WSACleanup();
#endif
        return NULL;
    }
    return b;
}
ARCS_NET_API void arcs_net_destroy(void *opaque) {
    struct bridge *b=opaque; if(!b) return;
    slirp_cleanup(b->slirp);
    while(b->timers) timer_free(b->timers,b);
    free(b);
#ifdef _WIN32
    WSACleanup();
#endif
}
static int add_poll(slirp_os_socket fd,int events,void *opaque) {
    struct bridge *b=opaque;
    if(b->fd_count==POLL_CAP) { fail(b,"libslirp socket capacity exceeded"); return -1; }
    int index=(int)b->fd_count++; struct pollfd *p=&b->fds[index]; p->fd=fd; p->events=0; p->revents=0;
    if(events&SLIRP_POLL_IN) p->events|=NET_POLL_IN;
    if(events&SLIRP_POLL_OUT) p->events|=POLLOUT;
    if(events&SLIRP_POLL_PRI) p->events|=NET_POLL_PRI;
    return index;
}
static int get_events(int index,void *opaque) {
    struct bridge *b=opaque;
    if(index<0 || (unsigned)index>=b->fd_count) return SLIRP_POLL_ERR;
    int revents=b->fds[index].revents, result=0;
    if(revents&NET_POLL_IN) result|=SLIRP_POLL_IN;
    if(revents&POLLOUT) result|=SLIRP_POLL_OUT;
    if(revents&NET_POLL_PRI) result|=SLIRP_POLL_PRI;
    if(revents&(POLLERR|POLLNVAL)) result|=SLIRP_POLL_ERR;
    if(revents&POLLHUP) result|=SLIRP_POLL_HUP;
    return result;
}
ARCS_NET_API int arcs_net_pump(void *opaque,int64_t now_ns) {
    struct bridge *b=opaque;
    if(now_ns<b->now) { fail(b,"libslirp clock moved backwards"); return -1; }
    b->now=now_ns;
    /* Restart traversal after callbacks: a callback may modify/free any timer. */
    for(unsigned budget=0;budget<64;budget++) {
        struct bridge_timer *timer=b->timers;
        while(timer && (timer->expiry<0 || timer->expiry>now_ns/1000000)) timer=timer->next;
        if(!timer) break;
        timer->expiry=-1; timer->callback(timer->opaque);
        if(budget==63) fail(b,"libslirp timer iteration limit exceeded");
    }
    b->fd_count=0; uint32_t timeout=0;
    slirp_pollfds_fill_socket(b->slirp,&timeout,add_poll,b);
    if(b->error[0]) return -1;
    int result = 0;
#ifdef _WIN32
    if (b->fd_count) { result = WSAPoll(b->fds, b->fd_count, 0); }
    if (result == SOCKET_ERROR && WSAGetLastError() != WSAEINTR) {
        char message[80];
        snprintf(message, sizeof(message), "Socket poll failed: %d", WSAGetLastError());
        fail(b, message);
    }
#else
    result = poll(b->fds,b->fd_count,0);
    if (result < 0 && errno != EINTR) { fail(b, strerror(errno)); }
#endif
    slirp_pollfds_poll(b->slirp,result<0,get_events,b);
    return b->error[0] ? -1 : 0;
}
ARCS_NET_API int arcs_net_input(void *opaque,const unsigned char *frame,int length) {
    struct bridge *b=opaque;
    if(length<14 || length>1514) { fail(b,"Guest Ethernet frame exceeds MTU"); return -1; }
    slirp_input(b->slirp,frame,length); return b->error[0] ? -1 : 0;
}
ARCS_NET_API int arcs_net_receive(void *opaque,unsigned char *destination,int capacity) {
    struct bridge *b=opaque; if(!b->count) return 0;
    struct frame *frame=&b->frames[b->head];
    if(capacity<frame->length) { fail(b,"Host Ethernet output buffer too small"); return -1; }
    int length=frame->length; memcpy(destination,frame->bytes,length);
    b->head=(b->head+1)%QUEUE_CAP; b->count--; return length;
}
ARCS_NET_API uint64_t arcs_net_dropped(void *opaque) { return ((struct bridge *)opaque)->dropped; }
ARCS_NET_API const char *arcs_net_error(void *opaque) { return ((struct bridge *)opaque)->error; }
ARCS_NET_API const char *arcs_net_version(void) { return slirp_version_string(); }
