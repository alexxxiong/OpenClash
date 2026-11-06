#define _GNU_SOURCE
#if !defined(__linux__) || !defined(__aarch64__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "Only little-endian AArch64 Linux is supported"
#endif
#include "elf_image.h"
#include "sha256.h"
#include "trace_state.h"
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/auxv.h>
#include <sys/mman.h>
#include <sys/ptrace.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysmacros.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef __WALL
#define __WALL 0x40000000
#endif
#define NOTE_TLS 0x401
#define NOTE_SYSCALL 0x404
#define NOTE_SVE 0x405
#define NOTE_SSVE 0x40b
#define NOTE_ZA 0x40c
#define MAX_MAPS 4096
#define SCRATCH_STACK (2U*1024U*1024U)
#define RECOVERY_MS 3000

_Static_assert(SI_USER==0&&SI_QUEUE==-1&&SI_TKILL==-6,"Linux sender code ABI");
struct mapping { uint64_t lo,hi,off,ino; unsigned major,minor; char perm[5],path[PATH_MAX]; };
struct maps { struct mapping *m; size_t n; };
struct config {
    pid_t pid,tid;
    char exe[PATH_MAX],so[PATH_MAX],sha[65],so_sha[65];
    const char *entry;
    uint64_t arg0;
    int timeout_ms,safe_wait_ms;
    bool check,ack,confirm_no_pidns;
};
struct boundary { uint64_t nr,args[6],sp,pc; };
struct trace {
    struct config *c;
    bool attached,stopped,saved,modified,mask_changed,gone,invalid_space;
    bool remote_inflight,unsafe_unwind,interrupted,deliver_external_trap;
    int status,deliver,syscallno;
    const char *phase;
    unsigned stop_sequence,resume_sequence;
    struct gprs requested;
    bool requested_valid;
    struct gprs regs;
    struct fpsimd fp;
    unsigned char tls[32]; size_t tls_len;
    uint64_t mask,starttime,scratch,scratch_len;
    dev_t exe_dev; ino_t exe_ino;
};
static volatile sig_atomic_t cancelled;
static void on_signal(int sig) { cancelled=sig; }
static uint64_t now_ms(void) {
    struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)<0)return 0;
    return (uint64_t)t.tv_sec*1000+(uint64_t)t.tv_nsec/1000000;
}
static void delay(void) { struct timespec t={0,5000000};nanosleep(&t,NULL); }
static void error_msg(const char *msg) { fprintf(stderr,"ERROR: %s (errno=%d: %s)\n",msg,errno,strerror(errno)); }
static int procpath(char *out,size_t cap,pid_t pid,const char *leaf) {
    int n=snprintf(out,cap,"/proc/%ld/%s",(long)pid,leaf);return n>0&&(size_t)n<cap?0:-1;
}
static bool trusted(const struct stat *st) { return S_ISREG(st->st_mode)&&st->st_uid==0&&!(st->st_mode&022); }
static int read_text(const char *path,char *buf,size_t cap) {
    int fd=open(path,O_RDONLY|O_CLOEXEC);if(fd<0)return -1;
    ssize_t n;do{n=read(fd,buf,cap-1);}while(n<0&&errno==EINTR);int err=errno;close(fd);errno=err;
    if(n<0||(size_t)n==cap-1)return -1;buf[n]=0;return 0;
}
static uint64_t starttime(pid_t pid) {
    char path[96],buf[4096];if(procpath(path,sizeof(path),pid,"stat")||read_text(path,buf,sizeof(buf)))return 0;
    char *p=strrchr(buf,')'),*save=NULL;if(!p||p[1]!=' ')return 0;
    char *v=strtok_r(p+2," ",&save);
    for(int field=3;v;field++,v=strtok_r(NULL," ",&save))if(field==22){char *end;errno=0;uint64_t n=strtoull(v,&end,10);return !errno&&!*end?n:0;}
    return 0;
}
static int status_ok(pid_t tid,pid_t tgid,bool attached) {
    char path[96],buf[8192];if(procpath(path,sizeof(path),tid,"status")||read_text(path,buf,sizeof(buf)))return -1;
    bool uid=false,group=false,tracer=false,seccomp=false,state=false;char *save=NULL;
    for(char *p=strtok_r(buf,"\n",&save);p;p=strtok_r(NULL,"\n",&save)) {
        unsigned a,b,c,d;long n;char s;
        if(sscanf(p,"Uid: %u %u %u %u",&a,&b,&c,&d)==4)uid=!(a|b|c|d);
        if(sscanf(p,"Tgid: %ld",&n)==1)group=n==tgid;
        if(sscanf(p,"TracerPid: %ld",&n)==1)tracer=n==(attached?getpid():0);
        if(sscanf(p,"Seccomp: %ld",&n)==1)seccomp=n==0;
        if(sscanf(p,"State: %c",&s)==1)state=attached?(s=='t'):(s!='T'&&s!='t'&&s!='Z'&&s!='X'&&s!='x');
    }
    if(!(uid&&group&&tracer&&seccomp&&state)){errno=EPERM;return -1;}return 0;
}
static int same_ns(pid_t pid,const char *name,bool confirm_no_pidns) {
    char path[96],self[96];struct stat a,b;
    if(snprintf(path,sizeof(path),"/proc/%ld/ns/%s",(long)pid,name)<0||snprintf(self,sizeof(self),"/proc/self/ns/%s",name)<0){errno=EINVAL;return -1;}
    int ar=stat(path,&a),ae=ar?errno:0;
    int br=stat(self,&b),be=br?errno:0;
    if(!ar&&!br) {
        if(a.st_dev==b.st_dev&&a.st_ino==b.st_ino)return 0;
        fprintf(stderr,"namespace %s mismatch: target dev/ino=%ju/%ju self=%ju/%ju\n",name,(uintmax_t)a.st_dev,(uintmax_t)a.st_ino,(uintmax_t)b.st_dev,(uintmax_t)b.st_ino);
        errno=EXDEV;return -1;
    }
    if(!strcmp(name,"pid")&&ar&&br&&ae==ENOENT&&be==ENOENT&&confirm_no_pidns) {
        fprintf(stderr,"PID namespace: both ns/pid interfaces absent (ENOENT); parent explicitly attests CONFIG_PID_NS disabled. Mount namespace remains strict.\n");return 0;
    }
    fprintf(stderr,"namespace %s: stat(%s)=%d errno=%d; stat(%s)=%d errno=%d%s\n",name,path,ar,ae,self,br,be,!strcmp(name,"pid")&&ae==ENOENT&&be==ENOENT?"; requires verified CONFIG_PID_NS disabled + --confirm-no-pidns":"");
    errno=ar?ae:be;return -1;
}
static int target_aux_ok(pid_t pid) {
    char path[96];if(procpath(path,sizeof(path),pid,"auxv"))return -1;
    int fd=open(path,O_RDONLY|O_CLOEXEC);if(fd<0)return -1;
    Elf64_auxv_t a;bool pages=false,hw=false,end=false;int result=-1;
    for(size_t i=0;i<256;i++) {
        ssize_t n=read(fd,&a,sizeof(a));if(n!=sizeof(a))break;
        if(a.a_type==AT_NULL){end=true;break;}
        if(a.a_type==AT_PAGESZ)pages=a.a_un.a_val==(uint64_t)sysconf(_SC_PAGESIZE);
        if(a.a_type==AT_HWCAP){hw=true;if(a.a_un.a_val&(1ULL<<22))goto done;}
        /* SME/SME2 state, MTE and guarded control stack are deliberately unsupported. */
        if(a.a_type==AT_HWCAP2&&(a.a_un.a_val&((1ULL<<18)|(1ULL<<23)|(1ULL<<39)|(1ULL<<44))))goto done;
    }
    if(end&&pages&&hw)result=0;
done:close(fd);if(result)errno=ENOTSUP;return result;
}
static int identity(struct trace *t,bool full) {
    char path[96],actual[PATH_MAX];struct stat st;
    if(procpath(path,sizeof(path),t->c->pid,"exe"))return -1;
    ssize_t n=readlink(path,actual,sizeof(actual)-1);if(n<=0||n>=(ssize_t)sizeof(actual)-1)return -1;actual[n]=0;
    if(strcmp(actual,t->c->exe)||stat(path,&st)||!trusted(&st)||starttime(t->c->pid)!=t->starttime||st.st_dev!=t->exe_dev||st.st_ino!=t->exe_ino){errno=ESTALE;return -1;}
    if(full) {
        struct elf_image e;if(elf_open(&e,path))return -1;char sha[65];sha256_hex(e.data,e.size,sha);
        bool ok=!strcmp(sha,t->c->sha)&&elf_musl_interp(&e);elf_close(&e);if(!ok){errno=ENOEXEC;return -1;}
    }
    return 0;
}
static void free_maps(struct maps *ms) { free(ms->m);ms->m=NULL;ms->n=0; }
static int get_maps(pid_t pid,struct maps *ms) {
    memset(ms,0,sizeof(*ms));char path[96];if(procpath(path,sizeof(path),pid,"maps"))return -1;
    FILE *f=fopen(path,"re");if(!f)return -1;ms->m=calloc(MAX_MAPS,sizeof(*ms->m));if(!ms->m){fclose(f);return -1;}
    char *line=NULL;size_t cap=0;int result=-1;
    while(getline(&line,&cap,f)>=0) {
        if(ms->n==MAX_MAPS)goto done;
        struct mapping *m=&ms->m[ms->n];int at=0;
        if(sscanf(line,"%"SCNx64"-%"SCNx64" %4s %"SCNx64" %x:%x %"SCNu64" %n",&m->lo,&m->hi,m->perm,&m->off,&m->major,&m->minor,&m->ino,&at)!=7||m->lo>=m->hi)goto done;
        char *p=line+at;while(*p==' ')p++;size_t n=strcspn(p,"\r\n");if(n>=sizeof(m->path))goto done;memcpy(m->path,p,n);m->path[n]=0;ms->n++;
    }
    if(!ferror(f)&&ms->n)result=0;
done:free(line);fclose(f);if(result)free_maps(ms);return result;
}
static bool map_file(const struct mapping *m,const struct stat *s) { return m->ino==(uint64_t)s->st_ino&&m->major==major(s->st_dev)&&m->minor==minor(s->st_dev); }
static bool executable_at(const struct maps *ms,const struct stat *s,uint64_t addr) {
    for(size_t i=0;i<ms->n;i++){const struct mapping *m=&ms->m[i];if(map_file(m,s)&&m->perm[2]=='x'&&addr>=m->lo&&addr<m->hi)return true;}return false;
}
static int load_bias(const struct maps *ms,const struct elf_image *e,uint64_t *out) {
    uint64_t page=(uint64_t)sysconf(_SC_PAGESIZE),bias=0;bool found=false;
    for(size_t i=0;i<ms->n;i++) {
        const struct mapping *m=&ms->m[i];if(!map_file(m,&e->st)||m->perm[2]!='x')continue;
        for(size_t j=0;j<e->eh.e_phnum;j++) {
            const Elf64_Phdr *p=&e->ph[j];uint64_t va=p->p_vaddr&~(page-1),off=p->p_offset&~(page-1);
            if(p->p_type!=PT_LOAD||!(p->p_flags&PF_X)||m->off!=off||m->lo<va)continue;
            uint64_t b=m->lo-va;if(found&&b!=bias)return -1;bias=b;found=true;
        }
    }
    if(!found)return -1;*out=bias;return 0;
}
static int find_libc(pid_t pid,const struct maps *ms,struct elf_image *e,uint64_t *bias,Elf64_Sym *mmap_sym,Elf64_Sym *dlopen_sym,Elf64_Sym *dlsym_sym,uint64_t *svc) {
    for(size_t i=0;i<ms->n;i++) {
        const struct mapping *m=&ms->m[i];const char *b=strrchr(m->path,'/');if(!b||m->perm[2]!='x')continue;b++;
        if(strcmp(b,"libc.so")&&strcmp(b,"libc.musl-aarch64.so.1")&&strcmp(b,"ld-musl-aarch64.so.1"))continue;
        char path[PATH_MAX+64];int n=snprintf(path,sizeof(path),"/proc/%ld/root%s",(long)pid,m->path);if(n<0||(size_t)n>=sizeof(path)||elf_open(e,path))continue;
        if(e->eh.e_type!=ET_DYN||!trusted(&e->st)||!map_file(m,&e->st)||load_bias(ms,e,bias)||elf_symbol(e,"mmap",mmap_sym)||elf_symbol(e,"dlopen",dlopen_sym)||elf_symbol(e,"dlsym",dlsym_sym)||elf_mmap_svc(e,mmap_sym,svc)){elf_close(e);continue;}
        if(*bias>UINT64_MAX-*svc||*bias>UINT64_MAX-dlopen_sym->st_value||*bias>UINT64_MAX-dlsym_sym->st_value){elf_close(e);continue;}
        *svc+=*bias;
        if(!executable_at(ms,&e->st,*svc)||!executable_at(ms,&e->st,*bias+dlopen_sym->st_value)||!executable_at(ms,&e->st,*bias+dlsym_sym->st_value)){elf_close(e);continue;}
        fprintf(stderr,"libc=%s bias=0x%"PRIx64" mmap=0x%"PRIx64" dlopen=0x%"PRIx64" dlsym=0x%"PRIx64"\n",m->path,*bias,*bias+mmap_sym->st_value,*bias+dlopen_sym->st_value,*bias+dlsym_sym->st_value);return 0;
    }
    errno=ENOEXEC;return -1;
}
static bool allowed_syscall(uint64_t nr) { return nr==101||nr==115||nr==73||nr==22||nr==441; }
static int observe_boundary(pid_t tid,struct boundary *b,int wait_ms) {
    char path[96],buf[512];if(procpath(path,sizeof(path),tid,"syscall"))return -1;
    uint64_t until=now_ms()+(unsigned)wait_ms;
    do {
        if(cancelled){errno=EINTR;return -1;}
        if(!read_text(path,buf,sizeof(buf))&&sscanf(buf,"%"SCNu64" %"SCNx64" %"SCNx64" %"SCNx64" %"SCNx64" %"SCNx64" %"SCNx64" %"SCNx64" %"SCNx64,&b->nr,&b->args[0],&b->args[1],&b->args[2],&b->args[3],&b->args[4],&b->args[5],&b->sp,&b->pc)==9&&allowed_syscall(b->nr))return 0;
        delay();
    }while(now_ms()<until);
    errno=ETIMEDOUT;return -1;
}
static int getreg(pid_t tid,int note,void *buf,size_t *n) {
    struct iovec io={buf,*n};if(ptrace(PTRACE_GETREGSET,tid,(void *)(uintptr_t)note,&io)<0)return -1;*n=io.iov_len;return 0;
}
static int exact_get(pid_t tid,int note,void *buf,size_t size) {
    size_t n=size;int rc=getreg(tid,note,buf,&n),err=rc?errno:0;
    if(rc||n!=size){fprintf(stderr,"REGSET_GET note=0x%x rc=%d errno=%d requested=%zu actual_len=%zu\n",note,rc,err,size,n);errno=rc?err:EIO;return -1;}return 0;
}
static int setreg(pid_t tid,int note,const void *buf,size_t n) {
    struct iovec io={(void *)buf,n};int rc=ptrace(PTRACE_SETREGSET,tid,(void *)(uintptr_t)note,&io)<0?-1:0,err=rc?errno:0;
    fprintf(stderr,"REGSET_SET note=0x%x rc=%d errno=%d requested=%zu returned_len=%zu\n",note,rc,err,n,io.iov_len);
    if(rc)errno=err;return rc;
}
static void diag_gprs(const char *label,const struct gprs *r,int syscallno,bool syscall_ok) {
    fprintf(stderr,"REGS %s PC=0x%016"PRIx64" SP=0x%016"PRIx64" x0=0x%016"PRIx64" x8=0x%016"PRIx64" PSTATE=0x%016"PRIx64" SS=%u syscallno=%d syscallno_valid=%d\n",label,r->pc,r->sp,r->x[0],r->x[8],r->pstate,(unsigned)((r->pstate>>21)&1),syscallno,syscall_ok);
}
static void diag_stop(struct trace *t,const char *where) {
    int original_errno=errno;
    fprintf(stderr,"STOP where=%s phase=%s stop_seq=%u resume_seq=%u raw_status=0x%08x WIFSTOPPED=%d WSTOPSIG=%d event=%u attached=%d stopped=%d saved=%d modified=%d inflight=%d\n",where,t->phase?t->phase:"initial",t->stop_sequence,t->resume_sequence,(unsigned)t->status,WIFSTOPPED(t->status),WIFSTOPPED(t->status)?WSTOPSIG(t->status):-1,(unsigned)t->status>>16,t->attached,t->stopped,t->saved,t->modified,t->remote_inflight);
    if(t->saved)diag_gprs("saved",&t->regs,t->syscallno,true);
    if(t->requested_valid)diag_gprs("requested-input",&t->requested,-1,true);
    if(t->stopped) {
        siginfo_t si;memset(&si,0,sizeof(si));errno=0;int rc=ptrace(PTRACE_GETSIGINFO,t->c->tid,0,&si)<0?-1:0,err=rc?errno:0;
        fprintf(stderr,"SIGINFO rc=%d errno=%d signo=%d si_errno=%d code=%d",rc,err,si.si_signo,si.si_errno,si.si_code);
        if(!rc&&(si.si_code==SI_USER||si.si_code==SI_QUEUE||si.si_code==SI_TKILL))fprintf(stderr," sender_pid=%ld sender_uid=%lu",(long)si.si_pid,(unsigned long)si.si_uid);
        else if(!rc&&(si.si_signo==SIGTRAP||si.si_signo==SIGSEGV||si.si_signo==SIGBUS||si.si_signo==SIGILL||si.si_signo==SIGFPE))fprintf(stderr," fault_addr=0x%016"PRIxPTR,(uintptr_t)si.si_addr);
        fprintf(stderr,"\n");
        struct gprs r;int sc=-1;int gr=exact_get(t->c->tid,NT_PRSTATUS,&r,sizeof(r));int ge=gr?errno:0;
        int sr=exact_get(t->c->tid,NOTE_SYSCALL,&sc,sizeof(sc));int se=sr?errno:0;
        fprintf(stderr,"STOP_REG_READ gpr_rc=%d errno=%d syscall_rc=%d errno=%d\n",gr,ge,sr,se);
        if(!gr)diag_gprs("actual-stop",&r,sc,!sr);
    }
    errno=original_errno;
}
static int remote_read(pid_t tid,uint64_t addr,void *dest,size_t n) {
    unsigned char *p=dest;
    while(n) {uint64_t aligned=addr&~7ULL;errno=0;long v=ptrace(PTRACE_PEEKDATA,tid,(void *)(uintptr_t)aligned,0);if(v==-1&&errno)return -1;size_t off=(size_t)(addr-aligned),take=8-off;if(take>n)take=n;memcpy(p,(unsigned char *)&v+off,take);p+=take;addr+=take;n-=take;}return 0;
}
/* This is the only target-memory write primitive; caller cannot choose arbitrary addresses. */
static int scratch_write(struct trace *t,uint64_t addr,const void *src,size_t n) {
    if(!t->scratch||addr<t->scratch||addr-t->scratch>t->scratch_len||n>t->scratch_len-(addr-t->scratch)){errno=EFAULT;return -1;}
    const unsigned char *p=src;
    while(n) {uint64_t aligned=addr&~7ULL;size_t off=(size_t)(addr-aligned),take=8-off;if(take>n)take=n;long v=0;
        if((off||take<8)&&remote_read(t->c->tid,aligned,&v,8))return -1;
        memcpy((unsigned char *)&v+off,p,take);
        if(ptrace(PTRACE_POKEDATA,t->c->tid,(void *)(uintptr_t)aligned,(void *)(uintptr_t)(unsigned long)v)<0)return -1;
        p+=take;addr+=take;n-=take;
    }return 0;
}
static int wait_stop(struct trace *t,int timeout_ms,bool recovery) {
    uint64_t until=now_ms()+(unsigned)timeout_ms;
    do {
        int status=0;pid_t r=waitpid(t->c->tid,&status,__WALL|WNOHANG);
        if(r==t->c->tid) {
            t->status=status;
            if(WIFEXITED(status)||WIFSIGNALED(status)){fprintf(stderr,"WAIT_TERMINAL raw_status=0x%08x exited=%d exit_code=%d signaled=%d term_signal=%d\n",(unsigned)status,WIFEXITED(status),WIFEXITED(status)?WEXITSTATUS(status):-1,WIFSIGNALED(status),WIFSIGNALED(status)?WTERMSIG(status):-1);t->gone=true;t->attached=false;t->stopped=false;errno=ESRCH;return -1;}
            if(WIFSTOPPED(status)) {
                t->stopped=true;t->stop_sequence++;unsigned event=(unsigned)status>>16;
                if(event==PTRACE_EVENT_EXEC||event==PTRACE_EVENT_EXIT)t->invalid_space=true;
                diag_stop(t,recovery?"recovery-wait":"wait");
                return 0;
            }
        } else if(r<0&&errno!=EINTR){if(errno==ECHILD||errno==ESRCH){t->gone=true;t->attached=false;}return -1;}
        if(!recovery&&cancelled){errno=EINTR;return -1;}delay();
    }while(now_ms()<until);
    errno=ETIMEDOUT;return -1;
}
static int resume_wait(struct trace *t,int request) {
    fprintf(stderr,"RESUME seq=%u request=%d phase=%s prior_status=0x%08x injected_signal=0\n",++t->resume_sequence,request,t->phase?t->phase:"initial",(unsigned)t->status);
    if(ptrace(request,t->c->tid,0,0)<0){error_msg("resume ptrace request");return -1;}t->stopped=false;
    return wait_stop(t,t->c->timeout_ms,false);
}
static int save_state(struct trace *t) {
    if(exact_get(t->c->tid,NT_PRSTATUS,&t->regs,sizeof(t->regs))||exact_get(t->c->tid,NT_FPREGSET,&t->fp,sizeof(t->fp))||exact_get(t->c->tid,NOTE_SYSCALL,&t->syscallno,sizeof(t->syscallno)))return -1;
    t->tls_len=sizeof(t->tls);if(getreg(t->c->tid,NOTE_TLS,t->tls,&t->tls_len)||t->tls_len!=8)return -1;
    if(ptrace(PTRACE_GETSIGMASK,t->c->tid,(void *)(uintptr_t)8,&t->mask)<0)return -1;
    /* Refuse rather than silently lose scalable vector/matrix state. */
    const int extra[]={NOTE_SVE,NOTE_SSVE,NOTE_ZA};
    for(size_t i=0;i<sizeof(extra)/sizeof(extra[0]);i++) {
        unsigned char buf[32];size_t n=sizeof(buf);errno=0;
        if(!getreg(t->c->tid,extra[i],buf,&n)){errno=ENOTSUP;return -1;}
        if(errno!=EINVAL&&errno!=EIO&&errno!=ENODEV)return -1;
    }
    if((t->regs.pstate&0x1f)||t->regs.pc%4||t->regs.sp%16||t->syscallno!=-1){errno=ENOTSUP;return -1;}
    t->saved=true;diag_gprs("saved-validated",&t->regs,t->syscallno,true);
    fprintf(stderr,"SAVED_STATE FP_bytes=%zu FP_arch_bytes=%zu FPSR=0x%08"PRIx32" FPCR=0x%08"PRIx32" TLS_bytes=%zu signal_mask=0x%016"PRIx64"\n",sizeof(t->fp),offsetof(struct fpsimd,padding),t->fp.fpsr,t->fp.fpcr,t->tls_len,t->mask);return 0;
}
static int confirm_boundary(struct trace *t,const struct boundary *b) {
    uint32_t word=0;
    if(b->pc<4||b->sp!=t->regs.sp||t->regs.x[8]!=b->nr||(t->regs.pc!=b->pc&&t->regs.pc!=b->pc-4)||remote_read(t->c->tid,b->pc-4,&word,4)||word!=0xd4000001U){errno=EAGAIN;return -1;}
    int64_t x=(int64_t)t->regs.x[0];
    if(x==-512||x==-513||x==-514||x==-516){errno=EAGAIN;return -1;}
    if(t->regs.pc==b->pc-4&&t->regs.x[0]!=b->args[0]){errno=EAGAIN;return -1;}
    fprintf(stderr,"observed blocked syscall=%"PRIu64"; saved PC=0x%"PRIx64" (lock freedom is NOT proven)\n",b->nr,t->regs.pc);return 0;
}
static int set_context(struct trace *t,const struct gprs *r) {
    int no=-1;t->modified=true;t->requested=*r;t->requested_valid=true;
    if(setreg(t->c->tid,NOTE_SYSCALL,&no,sizeof(no))||setreg(t->c->tid,NT_PRSTATUS,r,sizeof(*r)))return -1;
    diag_stop(t,"context-installed-before-resume");return 0;
}
static void unexpected_stop(struct trace *t) {
    if(!t->stopped||t->invalid_space)return;
    siginfo_t si;int sig=WSTOPSIG(t->status);unsigned event=(unsigned)t->status>>16;
    if(event==PTRACE_EVENT_STOP) {
        if(sig==SIGSTOP||sig==SIGTSTP||sig==SIGTTIN||sig==SIGTTOU)t->deliver=sig;
        return;
    }
    if(event||sig==(SIGTRAP|0x80))return;
    if(ptrace(PTRACE_GETSIGINFO,t->c->tid,0,&si)==0) {
        if(sig==SIGTRAP) {
            bool external=si.si_signo==SIGTRAP&&trap_external_sender(si.si_code,si.si_pid,getpid(),t->c->pid,t->c->tid,t->modified);
            fprintf(stderr,"SIGNAL_POLICY SIGTRAP code=%d external_sender_verified=%d action=%s (protocol failure remains a failure)\n",si.si_code,external,external?"forward-external":"suppress-injector-or-unverified-trap");
            if(external){t->deliver=SIGTRAP;t->deliver_external_trap=true;}
            else if(t->deliver==SIGTRAP){t->deliver=0;t->deliver_external_trap=false;}
            return;
        }
        if(si.si_code<=0)t->deliver=sig; /* Preserve externally sent non-trap signals. */
        else fprintf(stderr,"injected operation fault signal=%d code=%d suppressed on rollback\n",sig,si.si_code);
    }
}
static int remote_mmap(struct trace *t,uint64_t svc) {
    t->phase="bootstrap-mmap-syscall";
    struct gprs r=t->regs;r.pc=svc;r.pstate&=~0xc00ULL;
    r.x[0]=0;r.x[1]=t->scratch_len;r.x[2]=PROT_READ|PROT_WRITE;r.x[3]=MAP_PRIVATE|MAP_ANONYMOUS|MAP_STACK;r.x[4]=UINT64_MAX;r.x[5]=0;r.x[8]=__NR_mmap;
    if(set_context(t,&r))return -1;t->remote_inflight=true;
    /* Trace exactly one syscall entry and exit. This avoids this kernel's
     * post-SVC SINGLESTEP SI_USER trap and forced PSTATE.SS normalization. */
    struct gprs got={0};
    for(int stage=0;stage<2;stage++){
        if(resume_wait(t,PTRACE_SYSCALL))return -1;
        int sc=-1;
        if(t->invalid_space||((unsigned)t->status>>16)||WSTOPSIG(t->status)!=(SIGTRAP|0x80)||
            exact_get(t->c->tid,NT_PRSTATUS,&got,sizeof(got))||
            exact_get(t->c->tid,NOTE_SYSCALL,&sc,sizeof(sc))||
            got.pc!=svc+4||got.sp!=r.sp||got.x[8]!=__NR_mmap||
            (stage==0&&sc!=__NR_mmap)){
            fprintf(stderr,"MMAP syscall stage=%d protocol mismatch: status=0x%x pc=0x%"PRIx64" sc=%d\n",stage,(unsigned)t->status,got.pc,sc);
            unexpected_stop(t);errno=EPROTO;return -1;
        }
        fprintf(stderr,"MMAP syscall %s verified: pc=0x%"PRIx64" nr=%d x0=0x%"PRIx64"\n",stage?"exit":"entry",got.pc,sc,got.x[0]);
    }
    t->remote_inflight=false;
    if(got.x[0]>=UINT64_MAX-4094||!got.x[0]){errno=got.x[0]?-(int64_t)got.x[0]:EFAULT;return -1;}
    t->scratch=got.x[0];if(t->scratch>UINT64_MAX-t->scratch_len){errno=EOVERFLOW;return -1;}
    fprintf(stderr,"scratch=[0x%"PRIx64",0x%"PRIx64") retained; bootstrap touched no original stack\n",t->scratch,t->scratch+t->scratch_len);
    struct maps ms;if(get_maps(t->c->pid,&ms))return -1;bool ok=false;
    for(size_t i=0;i<ms.n;i++){const struct mapping *m=&ms.m[i];if(t->scratch>=m->lo&&t->scratch+t->scratch_len<=m->hi&&m->perm[0]=='r'&&m->perm[1]=='w'&&m->perm[2]=='-')ok=true;}
    free_maps(&ms);if(!ok){errno=EPERM;return -1;}return 0;
}
static int remote_call(struct trace *t,uint64_t fn,uint64_t a0,uint64_t a1,uint64_t *result,const char *label) {
    t->phase=label;
    struct gprs r=t->regs;r.pc=fn;r.sp=(t->scratch+t->scratch_len-16)&~15ULL;r.pstate&=~0xc00ULL;
    r.x[0]=a0;r.x[1]=a1;r.x[29]=0;r.x[30]=t->scratch;
    if(set_context(t,&r))return -1;t->remote_inflight=true;
    fprintf(stderr,"calling %s on selected TID %ld\n",label,(long)t->c->tid);
    if(resume_wait(t,PTRACE_CONT))return -1;
    siginfo_t si;struct gprs got;
    if(t->invalid_space||((unsigned)t->status>>16)||WSTOPSIG(t->status)!=SIGSEGV||ptrace(PTRACE_GETSIGINFO,t->c->tid,0,&si)<0||si.si_code!=SEGV_ACCERR||(uint64_t)(uintptr_t)si.si_addr!=t->scratch||exact_get(t->c->tid,NT_PRSTATUS,&got,sizeof(got))||got.pc!=t->scratch||got.sp!=r.sp){unexpected_stop(t);errno=EPROTO;return -1;}
    /* Only this precise NX-sentinel return is considered a successful function call. */
    t->remote_inflight=false;*result=got.x[0];fprintf(stderr,"%s returned 0x%"PRIx64"; NX return signal suppressed\n",label,*result);return 0;
}
static int restore_state(struct trace *t) {
    int failed=0;
    diag_stop(t,"restore-before-writes");
    if(t->modified) {
        if(setreg(t->c->tid,NT_FPREGSET,&t->fp,sizeof(t->fp)))failed=1;
        if(setreg(t->c->tid,NOTE_TLS,t->tls,t->tls_len))failed=1;
        if(setreg(t->c->tid,NOTE_SYSCALL,&t->syscallno,sizeof(t->syscallno)))failed=1;
        if(setreg(t->c->tid,NT_PRSTATUS,&t->regs,sizeof(t->regs)))failed=1;
    }
    if(t->mask_changed) {
        int rc=ptrace(PTRACE_SETSIGMASK,t->c->tid,(void *)(uintptr_t)8,&t->mask)<0?-1:0,err=rc?errno:0;
        fprintf(stderr,"RESTORE_SET signal-mask rc=%d errno=%d expected=0x%016"PRIx64"\n",rc,err,t->mask);if(rc)failed=1;
    }
    if(t->modified) {
        struct gprs r;struct fpsimd f;unsigned char tls[32];size_t n=sizeof(tls);int sc;
        int rc=exact_get(t->c->tid,NT_PRSTATUS,&r,sizeof(r)),err=rc?errno:0;
        unsigned differences=rc?0:gpr_differences(stderr,&t->regs,&r);
        fprintf(stderr,"RESTORE_VERIFY GPR rc=%d errno=%d field_mismatches=%u bytes=%zu\n",rc,err,differences,sizeof(r));
        if(rc||differences)failed=1;
        rc=exact_get(t->c->tid,NT_FPREGSET,&f,sizeof(f));err=rc?errno:0;
        differences=rc?0:fp_differences(stderr,&t->fp,&f);
        fprintf(stderr,"RESTORE_VERIFY FP rc=%d errno=%d field_mismatches=%u compared_arch_bytes=520 total_bytes=%zu\n",rc,err,differences,sizeof(f));
        if(rc||differences)failed=1;
        rc=getreg(t->c->tid,NOTE_TLS,tls,&n);err=rc?errno:0;
        fprintf(stderr,"RESTORE_VERIFY TLS rc=%d errno=%d expected_len=%zu actual_len=%zu\n",rc,err,t->tls_len,n);
        if(rc||n!=t->tls_len)failed=1;
        else for(size_t i=0;i<n;i++)if(tls[i]!=t->tls[i]){fprintf(stderr,"RESTORE_DIFF TLS byte=%zu saved=%02x actual=%02x\n",i,(unsigned)t->tls[i],(unsigned)tls[i]);failed=1;}
        sc=0;rc=exact_get(t->c->tid,NOTE_SYSCALL,&sc,sizeof(sc));err=rc?errno:0;
        fprintf(stderr,"RESTORE_VERIFY syscallno rc=%d errno=%d saved=%d actual=%d match=%d\n",rc,err,t->syscallno,sc,!rc&&sc==t->syscallno);
        if(rc||sc!=t->syscallno)failed=1;
    }
    if(t->mask_changed){uint64_t mask=0;int rc=ptrace(PTRACE_GETSIGMASK,t->c->tid,(void *)(uintptr_t)8,&mask)<0?-1:0,err=rc?errno:0;fprintf(stderr,"RESTORE_VERIFY signal-mask rc=%d errno=%d saved=0x%016"PRIx64" actual=0x%016"PRIx64" match=%d\n",rc,err,t->mask,mask,!rc&&mask==t->mask);if(rc||mask!=t->mask)failed=1;}
    diag_stop(t,"restore-after-verification");
    if(failed){errno=EIO;return -1;}return 0;
}
/* Always entered after SEIZE, including partial snapshots and all timeout paths. */
static int cleanup(struct trace *t) {
    if(!t->attached)return t->gone?4:0;
    if(!t->stopped) {
        if(ptrace(PTRACE_INTERRUPT,t->c->tid,0,0)<0&&errno!=EIO)error_msg("recovery interrupt");
        if(wait_stop(t,RECOVERY_MS,true)) {
            fprintf(stderr,"CRITICAL: cannot obtain a recovery stop; no kill sent. Register restoration is UNCONFIRMED. Kernel detaches when tracer exits.\n");return 4;
        }
        unexpected_stop(t);
    }
    if(t->remote_inflight){t->unsafe_unwind=true;fprintf(stderr,"UNSAFE LIFECYCLE: remote operation did not return; registers can be restored, loader/constructor/entry side effects and locks CANNOT be rolled back.\n");}
    int result=t->unsafe_unwind?3:0;
    if(t->invalid_space||identity(t,false)) {
        fprintf(stderr,"CRITICAL: target exiting/execed/identity changed; refusing stale register restoration.\n");result=4;
    } else if(t->saved&&(t->modified||t->mask_changed)) {
        if(restore_state(t)){error_msg("register/signal-mask restoration verification FAILED");result=4;}
        else fprintf(stderr,"RESTORED: all GPRs + FP/NEON + TLS + syscall number + signal mask verified before detach\n");
    }
    if(t->deliver==SIGTRAP&&t->modified&&!t->deliver_external_trap){fprintf(stderr,"SIGNAL_POLICY detach blocked unverified/injector SIGTRAP forwarding; result unchanged\n");t->deliver=0;}
    if(ptrace(PTRACE_DETACH,t->c->tid,0,(void *)(uintptr_t)t->deliver)<0){error_msg("detach failed; tracer exit will release trace relationship");result=4;}
    else {t->attached=false;t->stopped=false;fprintf(stderr,"DETACHED TID %ld (forwarded signal=%d); no kill / no service restart\n",(long)t->c->tid,t->deliver);}
    return result;
}
static int number(const char *s,uint64_t *v) {if(!s||!*s||*s=='-'||isspace((unsigned char)*s))return -1;char *end;errno=0;*v=strtoull(s,&end,0);return errno||*end?-1:0;}
static bool hash_arg(const char *s,char out[65]) {if(strlen(s)!=64)return false;for(size_t i=0;i<64;i++){if(!isxdigit((unsigned char)s[i]))return false;out[i]=(char)tolower((unsigned char)s[i]);}out[64]=0;return true;}
static int canonical(const char *s,char out[PATH_MAX]) {if(s[0]!='/'||!realpath(s,out)||strcmp(s,out)){errno=EINVAL;return -1;}return 0;}
static void usage(void) {
    fprintf(stderr,"EXPERIMENTAL AArch64-musl ptrace loader; offline review + dummy test first.\n"
        "ptrace-load --expect-exe /canonical/exe --expect-sha256 HEX --pid PID\n"
        "  --so /canonical/library.so --expect-so-sha256 HEX\n"
        "  [--tid TID] [--timeout-ms 1000..30000] [--safe-wait-ms 0..10000]\n"
        "  [--entry exported_function --arg0 UINT64] [--check-only | --ack-loader-risk]\n"
        "  [--confirm-no-pidns] ONLY after parent verifies CONFIG_PID_NS disabled\n"
        "ptrace-load --inspect-elf /absolute/file [symbol]\n"
        "Exit: 0 success/check, 1 operation/refusal, 2 usage, 3 unsafe in-flight rewind, 4 cleanup/lifecycle uncertain.\n");
}
int main(int argc,char **argv) {
    if(argc>=3&&!strcmp(argv[1],"--inspect-elf")) {
        if(argc>4)return 2;struct elf_image e;if(elf_open(&e,argv[2])){error_msg("ELF inspection");return 1;}
        char sha[65];sha256_hex(e.data,e.size,sha);printf("AArch64 ELF type=%u sha256=%s dynsyms=%zu musl-interp=%d\n",e.eh.e_type,sha,e.nsyms,elf_musl_interp(&e));
        int result=0;if(argc==4){Elf64_Sym s;if(elf_symbol(&e,argv[3],&s)){fprintf(stderr,"symbol absent/unsupported\n");result=1;}else printf("%s value=0x%"PRIx64" size=%"PRIu64"\n",argv[3],(uint64_t)s.st_value,(uint64_t)s.st_size);}
        elf_close(&e);return result;
    }
    struct config c={.timeout_ms=5000,.safe_wait_ms=3000};bool has_arg=false;
    for(int i=1;i<argc;i++) {
        const char *a=argv[i];if(!strcmp(a,"--check-only")){c.check=true;continue;}if(!strcmp(a,"--ack-loader-risk")){c.ack=true;continue;}if(!strcmp(a,"--confirm-no-pidns")){c.confirm_no_pidns=true;continue;}
        if(i+1>=argc){usage();return 2;}const char *v=argv[++i];uint64_t n;
        if(!strcmp(a,"--expect-exe")){if(canonical(v,c.exe))return 2;}
        else if(!strcmp(a,"--so")){if(canonical(v,c.so))return 2;}
        else if(!strcmp(a,"--expect-sha256")){if(!hash_arg(v,c.sha))return 2;}
        else if(!strcmp(a,"--expect-so-sha256")){if(!hash_arg(v,c.so_sha))return 2;}
        else if(!strcmp(a,"--entry")){if(!*v||strlen(v)>255)return 2;for(const char *p=v;*p;p++)if(!isalnum((unsigned char)*p)&&*p!='_')return 2;c.entry=v;}
        else if(!strcmp(a,"--pid")){if(number(v,&n)||n<2||n>INT_MAX)return 2;c.pid=(pid_t)n;}
        else if(!strcmp(a,"--tid")){if(number(v,&n)||n<2||n>INT_MAX)return 2;c.tid=(pid_t)n;}
        else if(!strcmp(a,"--arg0")){if(number(v,&c.arg0))return 2;has_arg=true;}
        else if(!strcmp(a,"--timeout-ms")){if(number(v,&n)||n<1000||n>30000)return 2;c.timeout_ms=(int)n;}
        else if(!strcmp(a,"--safe-wait-ms")){if(number(v,&n)||n>10000)return 2;c.safe_wait_ms=(int)n;}
        else {usage();return 2;}
    }
    if(!c.exe[0]||!c.so[0]||!c.sha[0]||!c.so_sha[0]||!c.pid||(!c.check&&!c.ack)||(has_arg&&!c.entry)||(c.entry&&!has_arg)){usage();return 2;}
    if(!c.tid)c.tid=c.pid;
    if(getuid()!=0||geteuid()!=0||c.pid==getpid()||c.tid==getpid()){errno=EPERM;error_msg("root-owned different target required");return 1;}
    long page=sysconf(_SC_PAGESIZE);if(page<4096||page>65536||(page&(page-1))){error_msg("unsupported page size");return 1;}
    struct sigaction sa={.sa_handler=on_signal};sigemptyset(&sa.sa_mask);sigaction(SIGINT,&sa,NULL);sigaction(SIGTERM,&sa,NULL);sigaction(SIGHUP,&sa,NULL);
    struct trace t={.c=&c,.scratch_len=SCRATCH_STACK+(uint64_t)page*2};
    struct elf_image exe={0},so={0},libc={0};struct maps ms={0};struct stat st;char path[96];
    int result=1,clean=0;uint64_t bias=0,svc=0,so_bias=0;Elf64_Sym mm,op,ds,entry={0};
    if(status_ok(c.pid,c.pid,false)){error_msg("preflight target /proc/PID/status: root UIDs/Tgid/TracerPid/Seccomp/state");goto done;}
    if(status_ok(c.tid,c.pid,false)){error_msg("preflight selected /proc/TID/status: root UIDs/Tgid/TracerPid/Seccomp/state");goto done;}
    if(same_ns(c.pid,"mnt",false)){error_msg("preflight strict mount namespace identity");goto done;}
    if(same_ns(c.pid,"pid",c.confirm_no_pidns)){error_msg("preflight PID namespace identity/explicit disabled-kernel attestation");goto done;}
    if(target_aux_ok(c.pid)){error_msg("preflight /proc/PID/auxv page size or unsupported SVE/SME/MTE/GCS");goto done;}
    if(procpath(path,sizeof(path),c.pid,"personality")||read_text(path,(char[96]){0},96)){error_msg("read personality");goto done;}
    {char p[96];if(read_text(path,p,sizeof(p))){goto done;}char *end;errno=0;unsigned long n=strtoul(p,&end,16);if(errno||end==p||(n&0x400000UL)){errno=ENOTSUP;error_msg("READ_IMPLIES_EXEC unsupported");goto done;}}
    if(procpath(path,sizeof(path),c.pid,"exe")||stat(path,&st)){error_msg("target exe");goto done;}
    t.exe_dev=st.st_dev;t.exe_ino=st.st_ino;t.starttime=starttime(c.pid);
    if(!t.starttime||identity(&t,true)||elf_open(&exe,path)||elf_open(&so,c.so)||!trusted(&so.st)||so.eh.e_type!=ET_DYN){error_msg("target/library identity or architecture");goto done;}
    {char h[65];sha256_hex(so.data,so.size,h);if(strcmp(h,c.so_sha)){errno=ESTALE;error_msg("library SHA-256 mismatch");goto done;}}
    if(c.entry&&elf_symbol(&so,c.entry,&entry)){error_msg("entry must be a defined, ordinary exported function in this library");goto done;}
    if(get_maps(c.pid,&ms)||find_libc(c.pid,&ms,&libc,&bias,&mm,&op,&ds,&svc)){error_msg("resolve target musl mmap/dlopen/dlsym (no local-address assumptions)");goto done;}
    if(c.check){fprintf(stderr,"CHECK ONLY: identity/hash/ELF/mappings passed; no attach or writes. Safe point / loader lock freedom / runtime restore NOT tested.\n");result=0;goto done;}
    fprintf(stderr,"EXPERIMENTAL: only TID %ld is attached; other threads remain running. Constructors/entry must be reviewed and nonblocking.\n",(long)c.tid);
    struct boundary b;
    if(observe_boundary(c.tid,&b,c.safe_wait_ms)){error_msg("no allowed blocked syscall within bounded wait; no attach");goto done;}
    if(ptrace(PTRACE_SEIZE,c.tid,0,(void *)(uintptr_t)(PTRACE_O_TRACESYSGOOD|PTRACE_O_TRACEEXEC|PTRACE_O_TRACEEXIT))<0){error_msg("PTRACE_SEIZE");goto done;}t.attached=true;
    if(ptrace(PTRACE_INTERRUPT,c.tid,0,0)<0||wait_stop(&t,c.timeout_ms,false)){error_msg("initial interrupt/wait");goto done;}
    if(t.invalid_space||((unsigned)t.status>>16)!=PTRACE_EVENT_STOP||WSTOPSIG(t.status)!=SIGTRAP){unexpected_stop(&t);error_msg("not an unambiguous requested interrupt stop");goto done;}
    if(identity(&t,true)||status_ok(c.tid,c.pid,true)||save_state(&t)||confirm_boundary(&t,&b)){error_msg("post-attach identity/regset/blocked-context refusal; retry only after review");goto done;}
    /* Confirm bytes used for bootstrap/entry still match the opened libc. */
    {unsigned char remote[16];const void *local=elf_vaddr(&libc,svc-bias,4);if(!local||remote_read(c.tid,svc,remote,4)||memcmp(remote,local,4)){error_msg("live mmap SVC bytes mismatch");goto done;}
        const uint64_t values[]={op.st_value,ds.st_value};for(size_t i=0;i<2;i++){local=elf_vaddr(&libc,values[i],16);if(!local||remote_read(c.tid,bias+values[i],remote,16)||memcmp(remote,local,16)){error_msg("live loader symbol bytes mismatch");goto done;}}}
    /* Recheck the explicitly named SO immediately before allowing constructors to run. */
    {struct elf_image check={0};char h[65];if(elf_open(&check,c.so)){error_msg("library recheck");goto done;}sha256_hex(check.data,check.size,h);bool ok=trusted(&check.st)&&check.st.st_dev==so.st.st_dev&&check.st.st_ino==so.st.st_ino&&!strcmp(h,c.so_sha);elf_close(&check);if(!ok){errno=ESTALE;error_msg("library changed since preflight");goto done;}}
    uint64_t mask=UINT64_MAX&~(1ULL<<(SIGKILL-1))&~(1ULL<<(SIGSTOP-1));
    if(ptrace(PTRACE_SETSIGMASK,c.tid,(void *)(uintptr_t)8,&mask)<0){error_msg("temporarily block catchable signals");goto done;}t.mask_changed=true;
    if(remote_mmap(&t,svc)){error_msg("bootstrap mmap/single-step");goto done;}
    uint64_t so_addr=t.scratch+(uint64_t)page,entry_addr=so_addr+strlen(c.so)+1,handle=0,address=0,value=0;
    if(entry_addr-so_addr+(c.entry?strlen(c.entry)+1:0)>(uint64_t)page){errno=E2BIG;error_msg("scratch strings exceed data page");goto done;}
    if(scratch_write(&t,so_addr,c.so,strlen(c.so)+1)||(c.entry&&scratch_write(&t,entry_addr,c.entry,strlen(c.entry)+1))){error_msg("write owned scratch strings");goto done;}
    /* RTLD_NOW=2, RTLD_LOCAL=0; no global symbols or library unloading requested. */
    if(remote_call(&t,bias+op.st_value,so_addr,2,&handle,"dlopen")){error_msg("dlopen did not return normally");goto done;}
    if(!handle){errno=ELIBBAD;error_msg("dlopen returned NULL; dlerror not called");goto done;}
    free_maps(&ms);
    if(get_maps(c.pid,&ms)||load_bias(&ms,&so,&so_bias)){error_msg("loaded library mapping inode mismatch/not present");goto done;}
    if(c.entry) {
        if(remote_call(&t,bias+ds.st_value,handle,entry_addr,&address,"dlsym")){error_msg("dlsym did not return normally");goto done;}
        if(so_bias>UINT64_MAX-entry.st_value||address!=so_bias+entry.st_value||!executable_at(&ms,&so.st,address)){errno=ELIBBAD;error_msg("dlsym must point to the expected export in the verified SO");goto done;}
        if(remote_call(&t,address,c.arg0,0,&value,c.entry)){error_msg("exported entry did not return normally");goto done;}
    }
    fprintf(stderr,"LOAD COMPLETE: handle=0x%"PRIx64"%s; library/refcount and scratch remain mapped.\n",handle,c.entry?"; optional entry completed":"");result=0;
done:
    clean=cleanup(&t);if(clean>result)result=clean;
    free_maps(&ms);elf_close(&exe);elf_close(&so);elf_close(&libc);
    if(cancelled&&result==0)result=1;
    fprintf(stderr,"RESULT=%d\n",result);return result;
}
