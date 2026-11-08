"""Writes Linux x86-64 ELF executables (SPEC §15.2 step 5).

The executable is dynamically linked against libc, which the runtime uses for memory,
copying, comparing, number formatting and I/O.
File layout, loaded at ``BASE``:

    ELF header, program headers        \\
    code, read-only data                 |  read + execute segment
    interpreter path, dynamic symbols    /
    dynamic section, GOT                 -- read + write segment

Imported functions are called through the GOT: `call [rip + slot]`. Each slot has an
`R_X86_64_GLOB_DAT` relocation, which the dynamic loader fills in at startup, so there is
no PLT and no lazy binding.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

BASE = 0x400000
_PAGE = 0x1000
_EHDR = 64
_PHDR = 56
_SEGMENTS = 6  # PT_PHDR, PT_INTERP, PT_LOAD (rx), PT_LOAD (rw), PT_DYNAMIC, PT_GNU_STACK
CODE_OFFSET = _EHDR + _PHDR * _SEGMENTS  # file offset (and offset from BASE) of the code
INTERP = b"/lib64/ld-linux-x86-64.so.2\0"
LIBC = b"libc.so.6"

_PT_LOAD, _PT_DYNAMIC, _PT_INTERP, _PT_PHDR = 1, 2, 3, 6
_PT_GNU_STACK = 0x6474E551
_PF_X, _PF_W, _PF_R = 1, 2, 4
_DT = {
    "NULL": 0, "NEEDED": 1, "HASH": 4, "STRTAB": 5, "SYMTAB": 6, "RELA": 7,
    "RELASZ": 8, "RELAENT": 9, "STRSZ": 10, "SYMENT": 11, "DEBUG": 21,
}  # fmt: skip
_DYNAMIC_ENTRIES = 11
_R_X86_64_GLOB_DAT = 6


def align(n: int, a: int) -> int:
    return (n + a - 1) // a * a


@dataclass
class Layout:
    """File offsets of every part. The read+write segment is mapped one page above its
    file offset, so it never shares a page with the read+execute one."""

    code_len: int
    rodata: int
    interp: int
    dynstr: int
    dynsym: int
    hash: int
    rela: int
    ro_end: int
    dynamic: int
    got: int
    end: int

    @staticmethod
    def of(code_len: int, rodata_len: int, imports: list[str]) -> "Layout":
        n = len(imports)
        rodata = CODE_OFFSET + align(code_len, 16)
        interp = align(rodata + rodata_len, 8)
        dynstr = interp + len(INTERP)
        dynsym = align(dynstr + len(_dynstr(imports)), 8)
        hash_ = dynsym + 24 * (n + 1)
        rela = align(hash_ + 4 * (3 + n + 1), 8)
        ro_end = rela + 24 * n
        dynamic = align(ro_end, 8)
        got = dynamic + 16 * _DYNAMIC_ENTRIES
        return Layout(
            code_len, rodata, interp, dynstr, dynsym, hash_, rela, ro_end, dynamic, got,
            got + 8 * n,
        )  # fmt: skip

    def addr(self, offset: int) -> int:
        """The address a file offset is loaded at."""
        return BASE + offset + (_PAGE if offset >= self.dynamic else 0)

    def from_code(self, offset: int) -> int:
        """The distance from the start of the code to what's at ``offset``."""
        return self.addr(offset) - (BASE + CODE_OFFSET)


def rodata_offset(code_len: int) -> int:
    """Where the read-only data goes, counted from the start of the code."""
    return align(code_len, 16)


def got_offset(code_len: int, rodata_len: int, imports: list[str]) -> int:
    """Where the GOT is, counted from the start of the code (in memory)."""
    lay = Layout.of(code_len, rodata_len, imports)
    return lay.from_code(lay.got)


def _dynstr(imports: list[str]) -> bytes:
    return b"\0" + LIBC + b"\0" + b"".join(name.encode() + b"\0" for name in imports)


def executable(code: bytes, rodata: bytes, entry: int, imports: list[str]) -> bytes:
    """An executable whose code starts at ``CODE_OFFSET``; ``entry`` is an offset into
    ``code``. ``imports`` are the libc functions the code calls through the GOT."""
    lay = Layout.of(len(code), len(rodata), imports)
    n = len(imports)
    out = bytearray(lay.end)

    def put(offset: int, data: bytes) -> None:
        out[offset : offset + len(data)] = data

    put(0, struct.pack(
        "<4sBBBBB7xHHIQQQIHHHHHH",
        b"\x7fELF", 2, 1, 1, 0, 0,  # 64-bit, little-endian, version 1, System V ABI
        2, 0x3E, 1,  # ET_EXEC, x86-64, version
        BASE + CODE_OFFSET + entry,  # entry point
        _EHDR, 0,  # program headers right after this header; no section headers
        0, _EHDR, _PHDR, _SEGMENTS, 64, 0, 0,
    ))  # fmt: skip
    rw_size = lay.end - lay.dynamic
    phdrs = [
        (_PT_PHDR, _PF_R, _EHDR, _PHDR * _SEGMENTS, 8),
        (_PT_INTERP, _PF_R, lay.interp, len(INTERP), 1),
        (_PT_LOAD, _PF_R | _PF_X, 0, lay.ro_end, _PAGE),
        (_PT_LOAD, _PF_R | _PF_W, lay.dynamic, rw_size, _PAGE),
        (_PT_DYNAMIC, _PF_R | _PF_W, lay.dynamic, 16 * _DYNAMIC_ENTRIES, 8),
    ]
    for i, (kind, flags, offset, size, al) in enumerate(phdrs):
        addr = lay.addr(offset)
        put(_EHDR + i * _PHDR, struct.pack("<IIQQQQQQ", kind, flags, offset, addr, addr, size, size, al))
    put(_EHDR + 5 * _PHDR, struct.pack("<IIQQQQQQ", _PT_GNU_STACK, _PF_R | _PF_W, 0, 0, 0, 0, 0, 16))

    put(CODE_OFFSET, code.ljust(lay.rodata - CODE_OFFSET, b"\xcc"))  # int3 padding
    put(lay.rodata, rodata)
    put(lay.interp, INTERP)
    strings = _dynstr(imports)
    put(lay.dynstr, strings)
    name = 1 + len(LIBC) + 1
    for i, imp in enumerate(imports):  # symbol 0 is the null symbol
        put(lay.dynsym + 24 * (i + 1), struct.pack("<IBBHQQ", name, 0x12, 0, 0, 0, 0))
        name += len(imp) + 1  # 0x12: global function, undefined
        got = lay.addr(lay.got + 8 * i)
        info = (i + 1) << 32 | _R_X86_64_GLOB_DAT
        put(lay.rela + 24 * i, struct.pack("<QQq", got, info, 0))
    # A hash table with one empty bucket: the executable defines no symbols.
    put(lay.hash, struct.pack(f"<II{n + 2}I", 1, n + 1, *[0] * (n + 2)))
    dynamic = [
        ("NEEDED", 1), ("HASH", lay.addr(lay.hash)), ("STRTAB", lay.addr(lay.dynstr)),
        ("SYMTAB", lay.addr(lay.dynsym)), ("STRSZ", len(strings)), ("SYMENT", 24),
        ("RELA", lay.addr(lay.rela)), ("RELASZ", 24 * n), ("RELAENT", 24),
        ("DEBUG", 0), ("NULL", 0),
    ]  # fmt: skip
    assert len(dynamic) == _DYNAMIC_ENTRIES
    put(lay.dynamic, b"".join(struct.pack("<qQ", _DT[k], v) for k, v in dynamic))
    return bytes(out)
