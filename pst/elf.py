"""Writes Linux x86-64 ELF executables (SPEC §15.2 step 5).

For now the executable is static: one read+execute segment holding the headers, the
code and the read-only data, loaded at ``BASE``. The stack is marked non-executable.
Dynamic linking (`PT_INTERP`, PLT/GOT) comes with `extern` support.
"""

from __future__ import annotations

import struct

BASE = 0x400000
_EHDR = 64
_PHDR = 56
_SEGMENTS = 2  # PT_LOAD, PT_GNU_STACK
CODE_OFFSET = _EHDR + _PHDR * _SEGMENTS  # file offset (and offset from BASE) of the code

_PT_LOAD = 1
_PT_GNU_STACK = 0x6474E551
_PF_X, _PF_W, _PF_R = 1, 2, 4


def align(n: int, a: int) -> int:
    return (n + a - 1) // a * a


def rodata_offset(code_len: int) -> int:
    """Where the read-only data goes, counted from the start of the code."""
    return align(code_len, 16)


def executable(code: bytes, rodata: bytes, entry: int) -> bytes:
    """An executable whose code starts at ``CODE_OFFSET``; ``entry`` is an offset into
    ``code``."""
    body = code.ljust(rodata_offset(len(code)), b"\xcc") + rodata  # int3 padding
    size = CODE_OFFSET + len(body)
    ehdr = struct.pack(
        "<4sBBBBB7xHHIQQQIHHHHHH",
        b"\x7fELF", 2, 1, 1, 0, 0,  # 64-bit, little-endian, version 1, System V ABI
        2, 0x3E, 1,  # ET_EXEC, x86-64, version
        BASE + CODE_OFFSET + entry,  # entry point
        _EHDR, 0,  # program headers right after this header; no section headers
        0, _EHDR, _PHDR, _SEGMENTS, 64, 0, 0,
    )  # fmt: skip
    load = struct.pack(
        "<IIQQQQQQ", _PT_LOAD, _PF_R | _PF_X, 0, BASE, BASE, size, size, 0x1000
    )
    stack = struct.pack("<IIQQQQQQ", _PT_GNU_STACK, _PF_R | _PF_W, 0, 0, 0, 0, 0, 16)
    out = ehdr + load + stack
    assert len(out) == CODE_OFFSET
    return out + body
