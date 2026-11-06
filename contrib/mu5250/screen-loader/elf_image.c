#define _GNU_SOURCE
#include "elf_image.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define MAX_ELF (256U*1024U*1024U)
#define MAX_SYMBOLS 2000000U
static bool range(size_t size, uint64_t off, uint64_t n) { return off<=size && n<=size-off; }
void elf_close(struct elf_image *e) { free(e->ph); free(e->data); memset(e,0,sizeof(*e)); }
const void *elf_vaddr(const struct elf_image *e, uint64_t va, size_t n) {
    for(size_t i=0;i<e->eh.e_phnum;i++) {
        const Elf64_Phdr *p=&e->ph[i];
        if(p->p_type==PT_LOAD && va>=p->p_vaddr && va-p->p_vaddr<=p->p_filesz && n<=p->p_filesz-(va-p->p_vaddr)) {
            uint64_t off=p->p_offset+(va-p->p_vaddr);
            if(range(e->size,off,n)) return e->data+off;
        }
    }
    return NULL;
}
static int dynamic_count(struct elf_image *e) {
    if(e->hash) {
        uint32_t hdr[2];const void *p=elf_vaddr(e,e->hash,sizeof(hdr));if(!p)return -1;memcpy(hdr,p,sizeof(hdr));
        if(!hdr[0]||!hdr[1]||hdr[1]>MAX_SYMBOLS)return -1;e->nsyms=hdr[1];return 0;
    }
    if(e->gnu_hash) {
        uint32_t h[4];const void *p=elf_vaddr(e,e->gnu_hash,sizeof(h));if(!p)return -1;memcpy(h,p,sizeof(h));
        if(!h[0]||h[0]>MAX_SYMBOLS||!h[2]||h[2]>MAX_SYMBOLS||h[1]>MAX_SYMBOLS)return -1;
        uint64_t buckets=e->gnu_hash+16+8ULL*h[2], chains=buckets+4ULL*h[0];
        p=elf_vaddr(e,buckets,(size_t)h[0]*4);if(!p)return -1;
        uint32_t highest=0;
        for(size_t i=0;i<h[0];i++){uint32_t b;memcpy(&b,(const unsigned char *)p+4*i,4);if(b && b<h[1])return -1;if(b>highest)highest=b;}
        if(!highest){e->nsyms=h[1];return 0;}
        if(highest>=MAX_SYMBOLS)return -1;
        for(uint32_t i=highest;i<MAX_SYMBOLS;i++) {
            uint32_t word;p=elf_vaddr(e,chains+4ULL*(i-h[1]),4);if(!p)return -1;memcpy(&word,p,4);
            if(word&1){e->nsyms=(size_t)i+1;return 0;}
        }
    }
    return -1;
}
int elf_open(struct elf_image *e, const char *path) {
    memset(e,0,sizeof(*e));int fd=open(path,O_RDONLY|O_CLOEXEC);if(fd<0)return -1;
    if(fstat(fd,&e->st)<0||!S_ISREG(e->st.st_mode)||e->st.st_size<(off_t)sizeof(e->eh)||e->st.st_size>MAX_ELF)goto fail;
    e->size=(size_t)e->st.st_size;e->data=malloc(e->size);if(!e->data)goto fail;
    for(size_t at=0;at<e->size;) {ssize_t n=pread(fd,e->data+at,e->size-at,(off_t)at);if(n<0&&errno==EINTR)continue;if(n<=0)goto fail;at+=(size_t)n;}
    close(fd);fd=-1;memcpy(&e->eh,e->data,sizeof(e->eh));
    if(memcmp(e->eh.e_ident,ELFMAG,SELFMAG)||e->eh.e_ident[EI_CLASS]!=ELFCLASS64||e->eh.e_ident[EI_DATA]!=ELFDATA2LSB||e->eh.e_ident[EI_VERSION]!=EV_CURRENT||e->eh.e_version!=EV_CURRENT||e->eh.e_machine!=EM_AARCH64||(e->eh.e_type!=ET_DYN&&e->eh.e_type!=ET_EXEC)||e->eh.e_ehsize!=sizeof(e->eh)||e->eh.e_phentsize!=sizeof(Elf64_Phdr)||!e->eh.e_phnum||e->eh.e_phnum>4096||!range(e->size,e->eh.e_phoff,(uint64_t)e->eh.e_phnum*sizeof(Elf64_Phdr)))goto fail;
    e->ph=malloc((size_t)e->eh.e_phnum*sizeof(Elf64_Phdr));if(!e->ph)goto fail;memcpy(e->ph,e->data+e->eh.e_phoff,(size_t)e->eh.e_phnum*sizeof(Elf64_Phdr));
    for(size_t i=0;i<e->eh.e_phnum;i++) {
        const Elf64_Phdr *p=&e->ph[i];
        if(!range(e->size,p->p_offset,p->p_filesz)||p->p_filesz>p->p_memsz||p->p_vaddr>UINT64_MAX-p->p_memsz)goto fail;
        if(p->p_type==PT_DYNAMIC) {
            bool end=false;
            if(e->symtab || p->p_filesz%sizeof(Elf64_Dyn))goto fail;
            for(size_t j=0;j<p->p_filesz/sizeof(Elf64_Dyn);j++) {
                Elf64_Dyn d;memcpy(&d,e->data+p->p_offset+j*sizeof(d),sizeof(d));
                if(d.d_tag==DT_NULL){end=true;break;}
                switch(d.d_tag){case DT_SYMTAB:e->symtab=d.d_un.d_ptr;break;case DT_STRTAB:e->strtab=d.d_un.d_ptr;break;case DT_STRSZ:e->strsz=d.d_un.d_val;break;case DT_SYMENT:e->syment=d.d_un.d_val;break;case DT_HASH:e->hash=d.d_un.d_ptr;break;case DT_GNU_HASH:e->gnu_hash=d.d_un.d_ptr;break;default:break;}
            }
            if(!end)goto fail;
        }
    }
    /* Static ET_EXEC is accepted for inspection; injection additionally requires PT_INTERP. */
    if(e->symtab && (e->syment!=sizeof(Elf64_Sym)||!e->strtab||!e->strsz||e->strsz>MAX_ELF||!elf_vaddr(e,e->strtab,(size_t)e->strsz)||dynamic_count(e)<0||!elf_vaddr(e,e->symtab,e->nsyms*sizeof(Elf64_Sym))))goto fail;
    return 0;
fail:
    {int err=errno; if(fd>=0)close(fd);elf_close(e);errno=err?err:ENOEXEC;return -1;}
}
int elf_symbol(const struct elf_image *e,const char *name,Elf64_Sym *out) {
    if(!e->symtab||!e->nsyms)return -1;
    const unsigned char *table=elf_vaddr(e,e->symtab,e->nsyms*sizeof(Elf64_Sym));
    const char *strings=elf_vaddr(e,e->strtab,(size_t)e->strsz);if(!table||!strings)return -1;
    for(size_t i=0;i<e->nsyms;i++) {
        Elf64_Sym s;memcpy(&s,table+i*sizeof(s),sizeof(s));
        if(s.st_name>=e->strsz||!memchr(strings+s.st_name,0,(size_t)(e->strsz-s.st_name)))return -1;
        unsigned bind=ELF64_ST_BIND(s.st_info),vis=ELF64_ST_VISIBILITY(s.st_other);
        if(s.st_shndx!=SHN_UNDEF&&s.st_shndx<SHN_LORESERVE&&ELF64_ST_TYPE(s.st_info)==STT_FUNC&&(bind==STB_GLOBAL||bind==STB_WEAK)&&(vis==STV_DEFAULT||vis==STV_PROTECTED)&&!strcmp(strings+s.st_name,name)) {
            /* musl aarch64/dlsym.s intentionally has no .size directive. A zero
             * st_size is valid ELF: still require a complete instruction in an
             * executable, file-backed PT_LOAD. mmap's SVC scan requires size. */
            uint64_t bytes=s.st_size?s.st_size:4;
            if(s.st_value%4||s.st_value>UINT64_MAX-bytes||!elf_vaddr(e,s.st_value,(size_t)bytes))return -1;
            bool executable=false;for(size_t j=0;j<e->eh.e_phnum;j++){const Elf64_Phdr *p=&e->ph[j];if(p->p_type==PT_LOAD&&(p->p_flags&PF_X)&&s.st_value>=p->p_vaddr&&s.st_value-p->p_vaddr<p->p_filesz&&bytes<=p->p_filesz-(s.st_value-p->p_vaddr))executable=true;}
            if(!executable)return -1;*out=s;return 0;
        }
    }
    return -1;
}
int elf_mmap_svc(const struct elf_image *e,const Elf64_Sym *s,uint64_t *va) {
    if(!s->st_size||s->st_size>4096||s->st_value%4)return -1;
    const unsigned char *p=elf_vaddr(e,s->st_value,(size_t)s->st_size);if(!p)return -1;
    for(size_t i=0;i+4<=s->st_size;i+=4){uint32_t word;memcpy(&word,p+i,4);if(word==0xd4000001U){*va=s->st_value+i;return 0;}}
    return -1;
}
bool elf_musl_interp(const struct elf_image *e) {
    for(size_t i=0;i<e->eh.e_phnum;i++) {const Elf64_Phdr *p=&e->ph[i];if(p->p_type==PT_INTERP&&p->p_filesz>1&&p->p_filesz<256){const char *s=(const char *)e->data+p->p_offset;const char *z=memchr(s,0,(size_t)p->p_filesz);if(!z||z!=s+p->p_filesz-1)return false;const char *b=strrchr(s,'/');return b&&!strcmp(b+1,"ld-musl-aarch64.so.1");}}
    return false;
}
