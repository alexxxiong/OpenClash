/* Read-only readiness probe for the exact MU5250 CN B27 UI build.
 * Opens proc files O_RDONLY; never attaches, writes memory or calls UI code.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "The verified target ABI is little endian"
#endif

#define TIMER_HANDLER UINT64_C(0x47a840)
#define NETWORK_VTABLE UINT64_C(0xe64908)
#define TIMER_STATE UINT64_C(0x227153c)
#define TIMER_LIST UINT64_C(0x2279c78)
#define MAX_MAPS 4096

struct mapping { uint64_t lo, hi; char perm[5]; };
struct snapshot { uint32_t tick; };
static struct mapping maps[MAX_MAPS];
static size_t map_count;
static bool display;
static const char *reason = "waiting-for-ui";
static char root[PATH_MAX] = "/proc";
static long pid;

static int emit(int code) {
    printf("{\"ready\":%s,\"reason\":\"%s\"}\n", code == 0 ? "true" : "false", reason);
    return code;
}
static int path(char *out, size_t n, const char *leaf) {
    int count = snprintf(out, n, "%s/%ld/%s", root, pid, leaf);
    return count < 0 || (size_t)count >= n ? -1 : 0;
}
static int read_text(const char *leaf, char *out, size_t n) {
    char file[PATH_MAX];
    if (path(file, sizeof(file), leaf)) return -1;
    int fd = open(file, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    ssize_t size = read(fd, out, n - 1);
    close(fd);
    if (size < 0 || (size_t)size == n - 1) return -1;
    out[size] = 0;
    return 0;
}
static uint64_t identity(void) {
    char file[PATH_MAX], text[8192], exe[PATH_MAX];
    if (path(file, sizeof(file), "exe")) return 0;
    ssize_t n = readlink(file, exe, sizeof(exe) - 1);
    if (n < 0) return 0;
    exe[n] = 0;
    if (strcmp(exe, "/usr/bin/zte_topsw_devui")) return 0;
    if (read_text("status", text, sizeof(text))) return 0;
    bool uid = false, group = false, tracer = false;
    char *save = NULL;
    for (char *p = strtok_r(text, "\n", &save); p; p = strtok_r(NULL, "\n", &save)) {
        unsigned a, b, c, d;
        long v;
        if (sscanf(p, "Uid: %u %u %u %u", &a, &b, &c, &d) == 4) uid = !(a | b | c | d);
        if (sscanf(p, "Tgid: %ld", &v) == 1) group = v == pid;
        if (sscanf(p, "TracerPid: %ld", &v) == 1) tracer = v == 0;
    }
    if (!uid || !group || !tracer || read_text("stat", text, sizeof(text))) return 0;
    char *p = strrchr(text, ')');
    if (!p || p[1] != ' ') return 0;
    save = NULL;
    p = strtok_r(p + 2, " \t\r\n", &save);
    for (int field = 3; p; field++, p = strtok_r(NULL, " \t\r\n", &save)) {
        if (field == 22) {
            char *end;
            errno = 0;
            uint64_t v = strtoull(p, &end, 10);
            return !errno && !*end ? v : 0;
        }
    }
    return 0;
}
static bool mapped(uint64_t a, size_t n, char required) {
    for (size_t i = 0; i < map_count; i++) {
        if (a >= maps[i].lo && a < maps[i].hi && n <= maps[i].hi - a &&
            strchr(maps[i].perm, required)) return true;
    }
    return false;
}
static int load_maps(void) {
    char file[PATH_MAX], line[PATH_MAX + 128];
    if (path(file, sizeof(file), "maps")) return -1;
    FILE *f = fopen(file, "r");
    if (!f) return -1;
    map_count = 0;
    display = false;
    while (fgets(line, sizeof(line), f)) {
        uint64_t a, b;
        char perm[5];
        if (sscanf(line, "%" SCNx64 "-%" SCNx64 " %4s", &a, &b, perm) != 3 || a >= b) continue;
        if (map_count == MAX_MAPS) { fclose(f); return -1; }
        maps[map_count].lo = a;
        maps[map_count].hi = b;
        memcpy(maps[map_count++].perm, perm, sizeof(perm));
        if (!strcmp(perm, "rw-s") && b - a == 320 * 480 * 2 && strstr(line, "/dev/dri/card0")) display = true;
    }
    int error = ferror(f);
    fclose(f);
    return error ? -1 : 0;
}
static bool memory(int fd, uint64_t at, void *out, size_t size) {
    return mapped(at, size, 'r') && at <= INT64_MAX &&
        pread(fd, out, size, (off_t)at) == (ssize_t)size;
}
static bool timer_node(int fd, uint64_t at) {
    uint64_t callback;
    return at && !(at & 7) && mapped(at, 32, 'w') &&
        memory(fd, at + 8, &callback, sizeof(callback)) && !(callback & 3) && mapped(callback, 4, 'x');
}
static int sample(int fd, struct snapshot *s) {
    if (load_maps()) { reason = "waiting-for-maps"; return 75; }
    uint32_t instruction;
    uint64_t vtable[2], list[3];
    uint8_t state[16];
    if (!memory(fd, TIMER_HANDLER, &instruction, sizeof(instruction)) ||
        !memory(fd, NETWORK_VTABLE, vtable, sizeof(vtable))) {
        reason = "waiting-for-code"; return 75;
    }
    if (instruction != 0xa9b97bfd || vtable[0] != 0x4f76ac || vtable[1] != 0x4f77d4) {
        reason = "unsupported-ui"; return 69;
    }
    if (!display) { reason = "waiting-for-display"; return 75; }
    if (!memory(fd, TIMER_STATE, state, sizeof(state)) || state[3] != 1 ||
        !memory(fd, TIMER_LIST, list, sizeof(list)) || list[0] != 32 ||
        !timer_node(fd, list[1]) || !timer_node(fd, list[2])) {
        reason = "waiting-for-timers"; return 75;
    }
    memcpy(&s->tick, state + 12, sizeof(s->tick));
    return 0;
}
static uint64_t now_ms(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return 0;
    return (uint64_t)t.tv_sec * 1000 + (uint64_t)t.tv_nsec / 1000000;
}
static bool number(const char *s, long *out, long low, long high) {
    if (!*s || *s == '+' || *s == '-') return false;
    char *end;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno || *end || v < low || v > high) return false;
    *out = v;
    return true;
}
int main(int argc, char **argv) {
    long wait_ms = 1200;
    bool have_pid = false;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--pid") && i + 1 < argc) {
            if (!number(argv[++i], &pid, 2, INT_MAX) || have_pid) return emit(64);
            have_pid = true;
        } else if (!strcmp(argv[i], "--proc-root") && i + 1 < argc) {
            const char *v = argv[++i];
            if (v[0] != '/' || strlen(v) >= sizeof(root)) return emit(64);
            strcpy(root, v);
        } else if (!strcmp(argv[i], "--wait-ms") && i + 1 < argc) {
            if (!number(argv[++i], &wait_ms, 0, 5000)) return emit(64);
        } else return emit(64);
    }
    if (!have_pid) return emit(64);
    uint64_t start = identity();
    if (!start) { reason = "ui-identity-unavailable"; return emit(69); }
    char file[PATH_MAX];
    if (path(file, sizeof(file), "mem")) return emit(64);
    int fd = open(file, O_RDONLY | O_CLOEXEC);
    if (fd < 0) { reason = "waiting-for-memory"; return emit(75); }
    struct snapshot first, next;
    int code = sample(fd, &first);
    if (code) { close(fd); return emit(code); }
    uint64_t deadline = now_ms() + (uint64_t)wait_ms;
    reason = "waiting-for-heartbeat";
    while (now_ms() < deadline) {
        struct timespec delay = {0, 50000000};
        nanosleep(&delay, NULL);
        if (identity() != start) { code = 69; reason = "ui-process-changed"; break; }
        code = sample(fd, &next);
        if (code) break;
        uint32_t elapsed = next.tick - first.tick;
        if (elapsed && elapsed <= 5000) { reason = "ready"; close(fd); return emit(0); }
        code = 75;
        reason = "waiting-for-heartbeat";
    }
    close(fd);
    return emit(code ? code : 75);
}
