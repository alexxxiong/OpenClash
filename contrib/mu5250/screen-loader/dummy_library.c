#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void write_marker(const char *kind,uint64_t arg,int create) {
    const char *path=getenv("INJECTOR_DUMMY_MARKER");if(!path||path[0]!='/')return;
    int fd=open(path,O_WRONLY|O_CLOEXEC|O_NOFOLLOW|(create?O_CREAT|O_EXCL:O_APPEND),0600);if(fd<0)return;
    char text[160];int len=snprintf(text,sizeof(text),"INJECTOR_DUMMY_%s_v1 pid=%ld arg0=0x%016"PRIx64"\n",kind,(long)getpid(),arg);
    if(len>0&&(size_t)len<sizeof(text)) {
        size_t at=0;while(at<(size_t)len){ssize_t n=write(fd,text+at,(size_t)len-at);if(n<0&&errno==EINTR)continue;if(n<=0)break;at+=(size_t)n;}
    }close(fd);
}
__attribute__((constructor)) static void dummy_constructor(void) {
    write_marker("CTOR",0,1);
    const char *hang=getenv("INJECTOR_DUMMY_HANG");
    if(hang&&!strcmp(hang,"1"))for(;;)__asm__ volatile("yield" ::: "memory");
    /* Deliberately clobber caller-saved SIMD registers: restoration must handle them. */
    __asm__ volatile("movi v0.16b, #0x55\n\tmovi v31.16b, #0xaa" ::: "v0","v31");
}
__attribute__((visibility("default"))) uint64_t dummy_entry(uint64_t arg) {
    write_marker("ENTRY",arg,0);return arg^UINT64_C(0x0123456789abcdef);
}
__attribute__((visibility("default"))) uint64_t dummy_uninstall(uint64_t arg) {
    write_marker("UNINSTALL",arg,0);return 0;
}
