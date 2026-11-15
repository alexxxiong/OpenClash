/* Exercise the real tick/uninstall paths; only the native widget ABI is fake.
 * Linux: clang -std=c11 -O2 -Wall -Wextra -Werror test-screen-layout.c -o /tmp/test-screen-layout
 */
static char status_path[] = "/tmp/mu5250-layout-XXXXXX";
#define MU_SCREEN_NATIVE_TEST 1
#define STATUS_FILE status_path
#define UI_LOG "/dev/null"
#define CONTROL "/dev/null"
#include "screen-network.c"
/* Zig's optimized C builds define NDEBUG; fixture checks must still execute. */
#undef NDEBUG
#include <assert.h>

enum { STOCK_COUNT = 4 };
struct FakeStock {
    int height, y, visible, selected;
};
struct FakeParent;
struct FakeRow {
    uintptr_t *vtable;
    struct FakeParent *parent;
    int width, height, y, visible, checked, enabled;
    void (*callback)(void *);
    void *callback_arg;
    char title[96];
};
struct FakeParent {
    uintptr_t vtable;
    struct FakeStock stock[STOCK_COUNT];
    struct FakeRow *extra;
    int height, layout_calls, constructor_height;
};
struct Fixture {
    struct FakeParent *parent;
    void *network_object, *timer_object;
    int allocations, constructors, deletions, text_calls, checked_calls;
};
static struct Fixture fixture;
static uintptr_t fake_vtable[12];

_Static_assert(sizeof(uintptr_t) == 8, "the native object layout requires 64 bits");
_Static_assert(sizeof(struct FakeRow) <= 0xa8, "fake row must fit native allocation");
_Static_assert(sizeof(struct FakeParent) >= 80, "native parent guard reads 80 bytes");

static int stock_height(const struct FakeParent *parent) {
    int height = 0;
    for (int i = 0; i < STOCK_COUNT; ++i)
        if (parent->stock[i].visible) height += parent->stock[i].height;
    return height;
}

static void *fake_new(size_t size) {
    assert(size == 0xa8);
    ++fixture.allocations;
    void *object = calloc(1, size);
    assert(object);
    return object;
}

static void fake_string_ctor(struct cpp_string *value, const char *text) {
    memset(value, 0, sizeof(*value));
    value->words[0] = (uintptr_t)text;
}

static void fake_string_dtor(struct cpp_string *value) {
    assert(value->words[0]);
    memset(value, 0, sizeof(*value));
}

static void fake_switch_ctor(void *object, void *container,
                             const struct cpp_string *title) {
    struct FakeParent *parent = container;
    struct FakeRow *widget = object;
    assert(parent == fixture.parent && !parent->extra);
    assert(parent->layout_calls == 1);
    assert(parent->height == stock_height(parent) + 16);
    parent->constructor_height = parent->height;
    widget->vtable = fake_vtable;
    widget->parent = parent;
    widget->width = 300;
    widget->height = 52;
    widget->y = parent->height;
    snprintf(widget->title, sizeof(widget->title), "%s",
             (const char *)title->words[0]);
    parent->extra = widget;
    ++fixture.constructors;
    /* Registering a child does not resize the parent's clipping rectangle. */
}

static void fake_callback(void *object, void (*callback)(void *), void *arg) {
    struct FakeRow *widget = object;
    assert(callback == clicked && !arg);
    widget->callback = callback;
    widget->callback_arg = arg;
    /* Register only: no test invokes clicked() or forks a control helper. */
}

static void fake_size(void *object, int width, int height) {
    struct FakeRow *widget = object;
    widget->width = width;
    widget->height = height;
}

static void fake_visible(void *object, int visible) {
    ((struct FakeRow *)object)->visible = visible;
}

static void fake_checked(void *object, int checked) {
    assert(updating);
    ((struct FakeRow *)object)->checked = checked;
    ++fixture.checked_calls;
}

static void fake_enable(void *object, int enabled) {
    assert(updating);
    ((struct FakeRow *)object)->enabled = enabled;
}

static void fake_text(void *object, const struct cpp_string *text) {
    struct FakeRow *widget = object;
    assert(updating);
    snprintf(widget->title, sizeof(widget->title), "%s",
             (const char *)text->words[0]);
    ++fixture.text_calls;
}

static void fake_delete(void *object) {
    struct FakeRow *widget = object;
    assert(widget->parent->extra == widget);
    widget->parent->extra = NULL;
    ++fixture.deletions;
    free(widget);
}

static void fake_native_layout(void *container) {
    struct FakeParent *parent = container;
    assert(parent == fixture.parent);
    assert(parent->vtable == 0xe67388);
    int bottom = 0;
    for (int i = 0; i < STOCK_COUNT; ++i) {
        struct FakeStock *stock = &parent->stock[i];
        if (!stock->visible) continue;
        stock->y = bottom;
        bottom += stock->height;
    }
    if (parent->extra) {
        struct FakeRow *widget = parent->extra;
        /* Post-layout must follow geometry, state and text updates. */
        assert(!updating && widget->visible == 1);
        assert(widget->width == 284 && widget->height == 52);
        assert(widget->callback == clicked && widget->title[0]);
        widget->y = bottom;
        bottom += widget->height;
    }
    parent->height = bottom + 16;
    ++parent->layout_calls;
}

static int fake_height(void *container) {
    assert(container == fixture.parent);
    return fixture.parent->height;
}

uintptr_t mu_screen_test_got(uintptr_t slot) {
    switch (slot) {
    case 0xe69fb8: return (uintptr_t)fake_new;
    case 0xe682b0: return (uintptr_t)fake_string_dtor;
    case 0xe68898: return (uintptr_t)fake_callback;
    case 0xe6cf18: return (uintptr_t)fake_size;
    case 0xe6c920: return (uintptr_t)fake_checked;
    case 0xe6b280: return (uintptr_t)fake_enable;
    case 0xe6fae8: return (uintptr_t)fake_native_layout;
    case 0xe68470: return (uintptr_t)fake_height;
    default:
        fprintf(stderr, "Unexpected native GOT slot: %#lx\n", (unsigned long)slot);
        abort();
    }
}

uintptr_t mu_screen_test_native(uintptr_t address) {
    switch (address) {
    case 0x4f7a50: return (uintptr_t)fake_string_ctor;
    case SWITCH_CTOR: return (uintptr_t)fake_switch_ctor;
    default:
        fprintf(stderr, "Unexpected native address: %#lx\n", (unsigned long)address);
        abort();
    }
}

static void write_status(const char *state, int enabled) {
    struct timespec now;
    assert(clock_gettime(CLOCK_BOOTTIME, &now) == 0);
    FILE *file = fopen(status_path, "w");
    assert(file);
    assert(fprintf(file, "{\"uptime\":%lu,\"state\":\"%s\","
                         "\"enabled\":%s,\"busy\":false}\n",
                   (unsigned long)now.tv_sec, state, enabled ? "true" : "false") > 0);
    assert(fclose(file) == 0);
}

static void setup(void) {
    assert(!row && !network && !timer);
    memset(&fixture, 0, sizeof(fixture));
    fixture.parent = calloc(1, sizeof(*fixture.parent));
    fixture.network_object = calloc(1, 176);
    fixture.timer_object = calloc(1, 32);
    assert(fixture.parent && fixture.network_object && fixture.timer_object);
    fixture.parent->vtable = 0xe67388;
    const int heights[STOCK_COUNT] = {52, 52, 104, 88};
    for (int i = 0; i < STOCK_COUNT; ++i) {
        fixture.parent->stock[i] = (struct FakeStock){heights[i], 0, 1, i % 2};
    }
    fixture.parent->height = 312;
    fake_vtable[1] = (uintptr_t)fake_delete;
    fake_vtable[6] = (uintptr_t)fake_visible;
    fake_vtable[11] = (uintptr_t)fake_text;
    *(uintptr_t *)fixture.network_object = NETWORK_VTABLE;
    *(void **)((char *)fixture.network_object + 0x58) = fixture.parent;
    *(int32_t *)((char *)fixture.timer_object + 0x18) = 1;
    network = fixture.network_object;
    timer = fixture.timer_object;
    installed = 1;
    child = 0;
    updating = desired = busy = usable = 0;
    last_enabled = pending_desired = -1;
    pending_until = sample_uptime = pending_after = error_until = 0;
    last_text[0] = 0;
    write_status("running", 1);
}

static void assert_stock_untouched(void) {
    for (int i = 0; i < STOCK_COUNT; ++i)
        assert(fixture.parent->stock[i].selected == i % 2);
    assert(child == 0 && pending_desired == -1 && error_until == 0);
}

static void assert_row_fits(void) {
    struct FakeParent *parent = fixture.parent;
    struct FakeRow *widget = row;
    assert(widget && widget == parent->extra);
    assert(widget->visible == 1 && widget->width == 284 && widget->height == 52);
    assert(widget->y == stock_height(parent));
    assert(widget->y >= 0 && widget->y + widget->height <= parent->height);
    assert(parent->height == stock_height(parent) + 52 + 16);
    assert_stock_untouched();
}

static void teardown(void) {
    if (installed) assert(mu_screen_uninstall() == 0);
    assert(!fixture.parent->extra && !row && !network && !timer);
    assert(fixture.allocations == fixture.deletions);
    free(fixture.timer_object);
    free(fixture.network_object);
    free(fixture.parent);
    memset(&fixture, 0, sizeof(fixture));
}

static void first_frame(void) {
    setup();
    tick(timer);
    assert_row_fits();
    assert(fixture.parent->constructor_height == 312);
    assert(fixture.parent->height == 364 && fixture.parent->layout_calls == 2);
    assert(fixture.allocations == 1 && fixture.constructors == 1);
    assert(usable == 1 && desired == 1 && !busy);
    assert(((struct FakeRow *)row)->checked == 1 && ((struct FakeRow *)row)->enabled == 1);
    assert(!strcmp(((struct FakeRow *)row)->title, "OpenClash · 运行中"));
    teardown();
}

static void hidden_stock(void) {
    setup();
    fixture.parent->stock[2].visible = 0;
    fixture.parent->stock[2].y = 4096;
    tick(timer);
    assert_row_fits();
    assert(fixture.parent->constructor_height == 208);
    assert(((struct FakeRow *)row)->y == 192 && fixture.parent->height == 260);
    assert(fixture.parent->stock[2].y == 4096);
    teardown();
}

static void stale_height(void) {
    setup();
    tick(timer);
    void *original = row;
    int layouts = fixture.parent->layout_calls;
    fixture.parent->height = 312;
    ((struct FakeRow *)row)->y = 999;
    write_status("running", 1);
    tick(timer);
    assert(row == original && fixture.constructors == 1);
    assert(fixture.parent->layout_calls == layouts + 1);
    assert(fixture.text_calls == 1 && fixture.checked_calls == 1);
    assert_row_fits();
    teardown();
}

static void status_refresh(void) {
    setup();
    tick(timer);
    void *original = row;
    fixture.parent->stock[3].visible = 0;
    write_status("disabled", 0);
    tick(timer);
    assert_row_fits();
    assert(row == original && fixture.allocations == 1 && fixture.constructors == 1);
    assert(usable == 1 && desired == 0 && !busy);
    assert(((struct FakeRow *)row)->checked == 0 && ((struct FakeRow *)row)->enabled == 1);
    assert(!strcmp(((struct FakeRow *)row)->title, "OpenClash · 已关闭"));
    assert(fixture.text_calls == 2 && fixture.checked_calls == 2);
    teardown();
}

static void uninstall_restores_stock(void) {
    setup();
    fixture.parent->stock[2].visible = 0;
    tick(timer);
    assert_row_fits();
    assert(mu_screen_uninstall() == 0);
    assert(!installed && !timer && !row && !network && !fixture.parent->extra);
    assert(fixture.deletions == 1 && fixture.parent->layout_calls == 3);
    assert(fixture.parent->height == stock_height(fixture.parent) + 16);
    assert(fixture.parent->height == 208);
    assert(*(int32_t *)((char *)fixture.timer_object + 0x18) == 0);
    assert(last_text[0] == 0 && last_enabled == -1 && desired == 1);
    assert_stock_untouched();
    tick(fixture.timer_object);
    assert(mu_screen_uninstall() == 0);
    assert(fixture.deletions == 1 && fixture.parent->layout_calls == 3);
    teardown();
}

int main(void) {
    int fd = mkstemp(status_path);
    assert(fd >= 0 && close(fd) == 0);
    first_frame();
    hidden_stock();
    stale_height();
    status_refresh();
    uninstall_restores_stock();
    assert(unlink(status_path) == 0);
    puts("5 layout cases passed (first frame, hidden stock, stale height, status refresh, uninstall)");
    return 0;
}
