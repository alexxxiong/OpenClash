#ifndef INJECT_TRACE_STATE_H
#define INJECT_TRACE_STATE_H
#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

struct gprs { uint64_t x[31],sp,pc,pstate; };
struct fpsimd { unsigned char v[32][16]; uint32_t fpsr,fpcr,padding[2]; };
_Static_assert(sizeof(struct gprs)==272,"AArch64 GPR layout");
_Static_assert(sizeof(struct fpsimd)==528,"AArch64 FPSIMD layout");
_Static_assert(offsetof(struct fpsimd,padding)==520,"FP padding starts after all architectural state");

/* Linux SI_USER=0, SI_QUEUE=-1, SI_TKILL=-6. A nonpositive code alone does
 * not establish an external sender (notably code=0, pid=0 pseudo-traps).
 * During injection a trap raised by the selected process belongs to that
 * injected execution, not the saved original continuation. */
static inline bool trap_external_sender(int code,int sender,int tracer,int tgid,int tid,bool modified) {
    return (code==0||code==-1||code==-6)&&sender>0&&sender!=tracer&&
           (!modified||(sender!=tgid&&sender!=tid));
}
static inline unsigned gpr_differences(FILE *log,const struct gprs *saved,const struct gprs *actual) {
    unsigned count=0;
    for(unsigned i=0;i<31;i++)if(saved->x[i]!=actual->x[i]) {
        count++;if(log)fprintf(log,"RESTORE_DIFF GPR.x%u saved=0x%016"PRIx64" actual=0x%016"PRIx64" xor=0x%016"PRIx64"\n",i,saved->x[i],actual->x[i],saved->x[i]^actual->x[i]);
    }
    const char *names[]={"SP","PC","PSTATE"};
    const uint64_t want[]={saved->sp,saved->pc,saved->pstate},got[]={actual->sp,actual->pc,actual->pstate};
    for(unsigned i=0;i<3;i++)if(want[i]!=got[i]) {
        count++;if(log)fprintf(log,"RESTORE_DIFF GPR.%s saved=0x%016"PRIx64" actual=0x%016"PRIx64" xor=0x%016"PRIx64"\n",names[i],want[i],got[i],want[i]^got[i]);
    }
    if(log&&saved->pstate!=actual->pstate)fprintf(log,"RESTORE_DIFF PSTATE.detail saved_SS=%u actual_SS=%u saved_BTYPE=%u actual_BTYPE=%u; NO bits ignored\n",(unsigned)((saved->pstate>>21)&1),(unsigned)((actual->pstate>>21)&1),(unsigned)((saved->pstate>>10)&3),(unsigned)((actual->pstate>>10)&3));
    return count;
}
static inline unsigned fp_differences(FILE *log,const struct fpsimd *saved,const struct fpsimd *actual) {
    unsigned count=0;
    for(unsigned i=0;i<32;i++)if(memcmp(saved->v[i],actual->v[i],16)) {
        uint64_t a[2],b[2];memcpy(a,saved->v[i],16);memcpy(b,actual->v[i],16);count++;
        if(log)fprintf(log,"RESTORE_DIFF FP.v%u saved_hi:lo=%016"PRIx64":%016"PRIx64" actual_hi:lo=%016"PRIx64":%016"PRIx64"\n",i,a[1],a[0],b[1],b[0]);
    }
    if(saved->fpsr!=actual->fpsr){count++;if(log)fprintf(log,"RESTORE_DIFF FP.FPSR saved=0x%08"PRIx32" actual=0x%08"PRIx32"\n",saved->fpsr,actual->fpsr);}
    if(saved->fpcr!=actual->fpcr){count++;if(log)fprintf(log,"RESTORE_DIFF FP.FPCR saved=0x%08"PRIx32" actual=0x%08"PRIx32"\n",saved->fpcr,actual->fpcr);}
    if(log)fprintf(log,"RESTORE_PADDING FP saved=%08"PRIx32":%08"PRIx32" actual=%08"PRIx32":%08"PRIx32" changed=%d ignored=1 (same preexisting 520-byte architectural comparison)\n",saved->padding[1],saved->padding[0],actual->padding[1],actual->padding[0],memcmp(saved->padding,actual->padding,sizeof(saved->padding))!=0);
    return count;
}
#endif
