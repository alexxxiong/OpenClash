#define _GNU_SOURCE
#if !defined(__aarch64__) || !defined(__linux__)
#error "This fixture is an AArch64 Linux executable, never run on Windows"
#endif
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* Fixed-register loop keeps canaries across each blocked syscall. No loader or heap calls. */
extern int dummy_blocked_loop(volatile unsigned char *control);
int main(int argc,char **argv) {
    if(argc<3||argc>4||argv[1][0]!='/'||argv[2][0]!='/') {
        fprintf(stderr,"dummy-target /absolute/marker /absolute/new-control-file [--hang-constructor]\n");return 2;
    }
    if(argc==4&&strcmp(argv[3],"--hang-constructor"))return 2;
    if(setenv("INJECTOR_DUMMY_MARKER",argv[1],1)||setenv("INJECTOR_DUMMY_HANG",argc==4?"1":"0",1))return 1;
    int fd=open(argv[2],O_CREAT|O_EXCL|O_RDWR|O_CLOEXEC|O_NOFOLLOW,0600);
    if(fd<0||ftruncate(fd,4096)){perror("control file");return 1;}
    volatile unsigned char *control=mmap(NULL,4096,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);
    if(control==MAP_FAILED){perror("control mmap");return 1;}
    printf("DUMMY_READY pid=%ld control[0]=Q quits; control[1]=canary-failure; u64@8=heartbeat\n",(long)getpid());fflush(stdout);
    int result=dummy_blocked_loop(control);
    printf("DUMMY_EXIT result=%d\n",result);fflush(stdout);munmap((void *)control,4096);return result;
}
