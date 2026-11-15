/* Exact-build MU5250 B27 UI extension. Loaded only after the launcher verifies
 * SHA256 571a201d18ba5ae80ce9f2b92948e63e9f73f2ead22b1920d3f58a6716f07028.
 * Adds a stock TUSettingSwitchButton child; no code/vtable or firmware writes.
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>

#define NETWORK_VTABLE ((uintptr_t)0xe64908)
#define TIMER_CREATE ((uintptr_t)0x47a6dc)
#define SWITCH_CTOR ((uintptr_t)0x54875c)
#ifndef STATUS_FILE
#define STATUS_FILE "/data/openclash/run/screen-status.json"
#endif
#ifndef CONTROL
#define CONTROL "/data/openclash/bin/screen-control.sh"
#endif
#ifndef UI_LOG
#define UI_LOG "/data/openclash/run/screen-ui.log"
#endif

struct cpp_string { uintptr_t words[4]; };
typedef void (*timer_cb)(void *);
typedef void *(*timer_create_fn)(timer_cb,uint32_t,void *);
typedef void *(*new_fn)(size_t);
typedef void (*string_ctor_fn)(struct cpp_string *, const char *);
typedef void (*string_dtor_fn)(struct cpp_string *);
typedef void (*switch_ctor_fn)(void *,void *,const struct cpp_string *);
typedef void (*set_size_fn)(void *,int,int);
typedef void (*layout_fn)(void *);
typedef int (*height_fn)(void *);
typedef void (*set_bool_fn)(void *,int);
typedef void (*set_callback_fn)(void *,void (*)(void *),void *);

static void *timer, *network, *row;
static pid_t child;
static int installed, updating, desired, busy, usable, last_enabled=-1;
static unsigned long pending_until;
static unsigned long sample_uptime, pending_after, error_until;
static int pending_desired=-1;
static char last_text[96];

#ifdef MU_SCREEN_NATIVE_TEST
/* Compile-time fixtures exercise the real tick with a fake native widget ABI.
 * No test dispatch is included in the device library. */
extern uintptr_t mu_screen_test_got(uintptr_t);
extern uintptr_t mu_screen_test_native(uintptr_t);
static uintptr_t got(uintptr_t a) { return mu_screen_test_got(a); }
static uintptr_t native(uintptr_t a) { return mu_screen_test_native(a); }
#else
static uintptr_t got(uintptr_t a) { return *(const uintptr_t *)a; }
static uintptr_t native(uintptr_t a) { return a; }
#endif
static void note(const char *text) {
    FILE *f=fopen(UI_LOG,"a");
    if(f){struct timespec t={0};clock_gettime(CLOCK_BOOTTIME,&t);fprintf(f,"[%ld.%03ld] %s\n",t.tv_sec,t.tv_nsec/1000000,text);fclose(f);}
}
static unsigned long uptime_sec(void) {
    /* /proc/uptime includes suspended time. MONOTONIC does not on this panel. */
    struct timespec t; if(clock_gettime(CLOCK_BOOTTIME,&t))return 0;
    return (unsigned long)t.tv_sec;
}
static int mapped(uintptr_t address,size_t length) {
    FILE *f=fopen("/proc/self/maps","r");char line[512],perm[8];unsigned long a,b;int ok=0;
    if(!f)return 0;
    while(fgets(line,sizeof(line),f))if(sscanf(line,"%lx-%lx %7s",&a,&b,perm)==3)
        if(perm[0]=='r'&&address>=a&&length<=b-address&&address<b){ok=1;break;}
    fclose(f);return ok;
}
static int compatible(void) {
    char exe[256];ssize_t n=readlink("/proc/self/exe",exe,sizeof(exe)-1);
    if(n<0)return 0;exe[n]=0;
    if(strcmp(exe,"/usr/bin/zte_topsw_devui")||geteuid()!=0)return 0;
    if(!mapped(NETWORK_VTABLE,21*8)||!mapped(TIMER_CREATE,16)||!mapped(SWITCH_CTOR,16))return 0;
    const uintptr_t *v=(const uintptr_t*)NETWORK_VTABLE;
    return v[0]==0x4f76ac&&v[1]==0x4f77d4&&v[11]==0x4f90e8&&v[19]==0x4f7f58
        &&*(const uint32_t*)TIMER_CREATE==0xa9bd7bfd
        &&*(const uint32_t*)SWITCH_CTOR==0xa9bc7bfd
        &&got(0xe6fae8)==0x548e68&&*(const uint32_t*)0x548e68==0xa9b97bfd;
}
static void *find_network(void) {
    FILE *f=fopen("/proc/self/maps","r");char line[512],perm[8];unsigned long a,b,offset,inode;char dev[32];
    void *found=NULL;size_t scanned=0;
    if(!f)return NULL;
    while(fgets(line,sizeof(line),f)){
        int consumed=0;
        if(sscanf(line,"%lx-%lx %7s %lx %31s %lu %n",&a,&b,perm,&offset,dev,&inode,&consumed)!=6)continue;
        if(strncmp(perm,"rw",2)||strchr(line+consumed,'/')||b-a>64*1024*1024)continue;
        if(strchr(line+consumed,'[')&&!strstr(line+consumed,"[heap]"))continue;
        if(scanned+b-a>64*1024*1024)break;scanned+=b-a;
        for(uintptr_t p=(a+7)&~(uintptr_t)7;p+176<=b;p+=8){
            if(*(uintptr_t*)p!=NETWORK_VTABLE)continue;
            uintptr_t parent=*(uintptr_t*)(p+0x58);
            if(!parent||!mapped(parent,16))continue;
            /* Saved registers on the loader's scratch stack can also contain
             * the class address. Require the real stock child-object graph. */
            uintptr_t stock_switch=*(uintptr_t*)(p+0x60);
            uintptr_t stock_button=*(uintptr_t*)(p+0x68);
            if(!mapped(stock_switch,8)||!mapped(stock_button,8)||
               *(uintptr_t*)stock_switch!=0xe672f8||*(uintptr_t*)stock_button!=0xe671e8)continue;
            if(found){found=NULL;goto done;}found=(void*)p;
        }
    }
done:fclose(f);return found;
}
static void set_text(const char *text) {
    if(!strcmp(last_text,text))return;
    struct cpp_string s;
    ((string_ctor_fn)native(0x4f7a50))(&s,text);
    uintptr_t *v=*(uintptr_t**)row;
    ((void(*)(void*,const struct cpp_string*))v[11])(row,&s);
    ((string_dtor_fn)got(0xe682b0))(&s);
    snprintf(last_text,sizeof(last_text),"%s",text);
}
static void clicked(void *unused) {
    (void)unused;
    if(updating||!usable||busy||child>0)return;
    last_enabled=-1;
    const char *action=desired?"off":"on";
    pid_t p=fork();
    if(p==0){
        int sink=open("/dev/null",O_RDWR);
        if(sink>=0){dup2(sink,0);dup2(sink,1);dup2(sink,2);if(sink>2)close(sink);}
        execl("/bin/sh","sh",CONTROL,action,(char*)NULL);_exit(127);
    }
    if(p>0){child=p;busy=1;error_until=0;pending_desired=!desired;pending_after=sample_uptime;pending_until=uptime_sec()+20;note(desired?"Screen requested OpenClash off":"Screen requested OpenClash on");set_text(desired?"OpenClash · 停止中":"OpenClash · 启动中");}
    else error_until=uptime_sec()+5;
}
static int read_status(char *state,size_t state_size) {
    char data[1024];FILE *f=fopen(STATUS_FILE,"r");
    if(!f)return 0;size_t n=fread(data,1,sizeof(data)-1,f);fclose(f);data[n]=0;
    char *stamp=strstr(data,"\"uptime\":");if(!stamp)return 0;
    char *end;unsigned long then=strtoul(stamp+9,&end,10),now=uptime_sec();
    if(end==stamp+9||then>now||now-then>20)return 0;
    char *s=strstr(data,"\"state\":\"");if(!s)return 0;s+=9;
    char *e=strchr(s,'"');if(!e||(size_t)(e-s)>=state_size)return 0;
    memcpy(state,s,(size_t)(e-s));state[e-s]=0;
    if(strcmp(state,"running")&&strcmp(state,"disabled")&&strcmp(state,"starting")&&strcmp(state,"stopping")&&strcmp(state,"failed"))return 0;
    if(strstr(data,"\"enabled\":true"))desired=1;
    else if(strstr(data,"\"enabled\":false"))desired=0;
    else return 0;
    busy=strstr(data,"\"busy\":true")!=NULL;
    sample_uptime=then;
    return 1;
}
static void reap_child(void) {
    if(child>0){
        int status;pid_t p=waitpid(child,&status,WNOHANG);
        if(p==child){child=0;if(!WIFEXITED(status)||WEXITSTATUS(status)){pending_desired=-1;last_enabled=-1;error_until=uptime_sec()+5;}}
        /* Stock UI reaps subprocesses itself. ECHILD is completion too;
         * cached service state remains the authority for operation success. */
        else if(p<0&&errno==ECHILD){child=0;note("Stock UI reaped control helper; following service status");}
    }
}
static void tick(void *called_timer) {
    if(!installed||called_timer!=timer)return;
    reap_child();
    if(!network){network=find_network();if(!network)return;}
    if(!mapped((uintptr_t)network,176)||*(uintptr_t*)network!=NETWORK_VTABLE){network=NULL;row=NULL;last_text[0]=0;last_enabled=-1;return;}
    void *parent=*(void**)((char*)network+0x58);
    if(!mapped((uintptr_t)parent,80)||*(uintptr_t*)parent!=0xe67388)return;
    int added=0,initial_height=0;
    if(!row){
        /* The stock page lays out once before our asynchronous child exists.
         * Normalize that stock layout, then include the new child below. */
        ((layout_fn)got(0xe6fae8))(parent);
        initial_height=((height_fn)got(0xe68470))(parent);
        struct cpp_string empty;((string_ctor_fn)native(0x4f7a50))(&empty,"");
        row=((new_fn)got(0xe69fb8))(0xa8);
        if(!row){((string_dtor_fn)got(0xe682b0))(&empty);return;}
        ((switch_ctor_fn)native(SWITCH_CTOR))(row,parent,&empty);
        ((string_dtor_fn)got(0xe682b0))(&empty);
        ((set_callback_fn)got(0xe68898))(row,clicked,NULL);
        /* Match the factory mobile-data switch's 284x52 content geometry. */
        ((set_size_fn)got(0xe6cf18))(row,284,52);
        uintptr_t *v=*(uintptr_t**)row;
        ((set_bool_fn)v[6])(row,1);
        last_enabled=-1;
        added=1;
        note("Added OpenClash switch to the stock Network page");
    }
    char state[24];usable=read_status(state,sizeof(state));
    const char *text="OpenClash · 状态未知";
    if(usable){
        if(!strcmp(state,"running"))text="OpenClash · 运行中";
        else if(!strcmp(state,"disabled"))text="OpenClash · 已关闭";
        else if(!strcmp(state,"starting"))text="OpenClash · 启动中";
        else if(!strcmp(state,"stopping"))text="OpenClash · 停止中";
        else text="OpenClash · 启动失败";
    }
    if(pending_desired>=0){
        int newer=usable&&sample_uptime>pending_after;
        int settled=newer&&((desired==pending_desired&&(!strcmp(state,"running")||!strcmp(state,"disabled")))||!strcmp(state,"failed"));
        if(settled){pending_desired=-1;error_until=0;}
        else if(newer&&!busy&&child<=0&&pending_until<=uptime_sec()){
            pending_desired=-1;last_enabled=-1;error_until=uptime_sec()+5;text="OpenClash · 操作失败";
        }
        else if(!newer&&pending_until<=uptime_sec()){text="OpenClash · 状态待更新";usable=0;busy=1;}
        else {text=pending_desired?"OpenClash · 启动中":"OpenClash · 停止中";busy=1;}
    }
    if(error_until>uptime_sec()&&pending_desired<0)text="OpenClash · 操作失败";
    updating=1;
    /* A pre-request cache sample must not visually undo the user's click. */
    int shown_desired=pending_desired>=0?pending_desired:desired;
    if(last_enabled!=shown_desired){((set_bool_fn)got(0xe6c920))(row,shown_desired);last_enabled=shown_desired;}
    ((set_bool_fn)got(0xe6b280))(row,usable&&!busy&&child<=0);
    set_text(text);updating=0;
    /* This is the same native container layout used on page entry. It grows
     * the clipping rectangle and ignores hidden stock rows on the first view. */
    ((layout_fn)got(0xe6fae8))(parent);
    if(added){
        char message[112];
        snprintf(message,sizeof(message),"First layout: container %d -> %d; row 284x52",initial_height,((height_fn)got(0xe68470))(parent));
        note(message);
    }
}
__attribute__((visibility("default"))) int mu_screen_install(void) {
    if(installed)return 0;
    if(!compatible()){note("Unsupported stock UI build; extension left inactive");return 69;}
    installed=1;timer=((timer_create_fn)native(TIMER_CREATE))(tick,1000,NULL);
    if(!timer){installed=0;return 70;}
    note("Stock Network extension timer installed");return 0;
}
__attribute__((visibility("default"))) int mu_screen_uninstall(void) {
    installed=0;
    if(timer){*(int32_t*)((char*)timer+0x18)=0;timer=NULL;}
    if(row){uintptr_t *v=*(uintptr_t**)row;((void(*)(void*))v[1])(row);row=NULL;}
    if(network&&mapped((uintptr_t)network,176)&&*(uintptr_t*)network==NETWORK_VTABLE){
        void *parent=*(void**)((char*)network+0x58);
        if(mapped((uintptr_t)parent,80)&&*(uintptr_t*)parent==0xe67388)((layout_fn)got(0xe6fae8))(parent);
    }
    network=NULL;last_text[0]=0;last_enabled=-1;
    note("Stock Network extension removed without restarting the UI");return 0;
}
/* Install is explicit via the injector's exported-entry call; dlopen alone is inert. */
