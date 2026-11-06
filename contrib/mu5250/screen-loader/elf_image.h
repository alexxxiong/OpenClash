#ifndef INJECT_ELF_IMAGE_H
#define INJECT_ELF_IMAGE_H
#include <elf.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/stat.h>
struct elf_image {
    unsigned char *data;
    size_t size;
    struct stat st;
    Elf64_Ehdr eh;
    Elf64_Phdr *ph;
    uint64_t symtab, strtab, strsz, syment, hash, gnu_hash;
    size_t nsyms;
};
int elf_open(struct elf_image *, const char *);
void elf_close(struct elf_image *);
const void *elf_vaddr(const struct elf_image *, uint64_t, size_t);
int elf_symbol(const struct elf_image *, const char *, Elf64_Sym *);
int elf_mmap_svc(const struct elf_image *, const Elf64_Sym *, uint64_t *);
bool elf_musl_interp(const struct elf_image *);
#endif
