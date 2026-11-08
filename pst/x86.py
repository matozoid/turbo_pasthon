"""A small x86-64 assembler: just the instructions the emit pass uses (SPEC §15.2 step 4).

Code goes into ``code`` and constant data into ``rodata``. Jumps, calls and
RIP-relative loads refer to labels, resolved by ``finish`` once the distance from the
code to the read-only data is known.
"""

from __future__ import annotations

RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI = range(8)
R8, R9, R10, R11, R12, R13, R14, R15 = range(8, 16)

# Condition codes, for jcc: `jcc(L, "e")` jumps if equal.
CC = {
    "o": 0x0, "no": 0x1, "b": 0x2, "ae": 0x3, "e": 0x4, "ne": 0x5, "be": 0x6, "a": 0x7,
    "s": 0x8, "ns": 0x9, "l": 0xC, "ge": 0xD, "le": 0xE, "g": 0xF,
}  # fmt: skip


def _rex(w: bool, reg: int = 0, rm: int = 0) -> int:
    return 0x40 | (8 if w else 0) | (4 if reg >= 8 else 0) | (1 if rm >= 8 else 0)


def _i32(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "little")


def _fits32(v: int) -> bool:
    return -(2**31) <= v < 2**31


class Assembler:
    def __init__(self) -> None:
        self.code = bytearray()
        self.rodata = bytearray()
        self.labels: dict[str, int] = {}  # code labels -> offset in code
        self.data_labels: dict[str, int] = {}  # -> offset in rodata
        self._fixups: list[tuple[int, str, bool]] = []  # (position of rel32, label, is_data)
        self._strings: dict[bytes, str] = {}
        self._n = 0

    # ------------------------------------------------------------------ labels

    def new_label(self, hint: str = "L") -> str:
        self._n += 1
        return f".{hint}{self._n}"

    def label(self, name: str) -> None:
        if name in self.labels:
            raise ValueError(f"label {name} defined twice")
        self.labels[name] = len(self.code)

    def data(self, value: bytes, align: int = 1) -> str:
        """Adds read-only data; returns its label. Equal byte strings are shared."""
        if value in self._strings:
            return self._strings[value]
        while len(self.rodata) % align:
            self.rodata.append(0)
        name = self.new_label("D")
        self.data_labels[name] = len(self.rodata)
        self.rodata += value
        self._strings[value] = name
        return name

    def _rel32(self, label: str, is_data: bool = False) -> None:
        self._fixups.append((len(self.code), label, is_data))
        self.code += b"\0\0\0\0"

    def finish(self, rodata_offset: int) -> bytes:
        """Resolves labels. ``rodata_offset`` is where the data starts, counted from the
        start of the code."""
        for pos, label, is_data in self._fixups:
            if is_data:
                target = rodata_offset + self.data_labels[label]
            else:
                if label not in self.labels:
                    raise ValueError(f"undefined label {label}")
                target = self.labels[label]
            self.code[pos : pos + 4] = _i32(target - (pos + 4))
        return bytes(self.code)

    # ------------------------------------------------------------ instructions

    def emit(self, *bs: int) -> None:
        self.code += bytes(bs)

    def _rr(self, op: int, dst: int, src: int, w: bool = True) -> None:
        """`op r/m, reg` with both operands registers."""
        rex = _rex(w, src, dst)
        if rex != 0x40:
            self.emit(rex)
        self.emit(op, 0xC0 | (src & 7) << 3 | (dst & 7))

    def mov(self, dst: int, src: int) -> None:
        self._rr(0x89, dst, src)

    def mov_imm(self, dst: int, value: int) -> None:
        if 0 <= value < 2**32:  # mov r32, imm32 zero-extends
            if dst >= 8:
                self.emit(0x41)
            self.emit(0xB8 + (dst & 7))
            self.code += _i32(value)
        elif _fits32(value):  # mov r/m64, imm32 sign-extends
            self.emit(_rex(True, 0, dst), 0xC7, 0xC0 | (dst & 7))
            self.code += _i32(value)
        else:
            self.emit(_rex(True, 0, dst), 0xB8 + (dst & 7))
            self.code += (value & (2**64 - 1)).to_bytes(8, "little")

    def _mem(self, op: bytes, reg: int, base: int, disp: int, w: bool = True, byte_reg: bool = False) -> None:
        """An instruction with a `[base + disp]` operand. ``reg`` is the register operand
        or the opcode extension."""
        rex = _rex(w, reg, base)
        if rex != 0x40 or (byte_reg and reg >= 4):  # spl..dil need a REX prefix
            self.emit(rex)
        self.code += op
        if disp == 0 and base & 7 != RBP:
            mod, tail = 0, b""
        elif -128 <= disp < 128:
            mod, tail = 1, bytes([disp & 0xFF])
        else:
            mod, tail = 2, _i32(disp)
        self.emit(mod << 6 | (reg & 7) << 3 | (base & 7))
        if base & 7 == RSP:
            self.emit(0x24)  # SIB: no index
        self.code += tail

    def load(self, dst: int, base: int, disp: int = 0) -> None:
        """`mov dst, [base + disp]`."""
        self._mem(b"\x8b", dst, base, disp)

    def store(self, base: int, disp: int, src: int) -> None:
        """`mov [base + disp], src`."""
        self._mem(b"\x89", src, base, disp)

    def store8(self, base: int, disp: int, src: int) -> None:
        """`mov byte [base + disp], src8`."""
        self._mem(b"\x88", src, base, disp, w=False, byte_reg=True)

    def store8_imm(self, base: int, disp: int, value: int) -> None:
        self._mem(b"\xc6", 0, base, disp, w=False)
        self.emit(value & 0xFF)

    def lea(self, dst: int, base: int, disp: int) -> None:
        self._mem(b"\x8d", dst, base, disp)

    def lea_data(self, dst: int, label: str) -> None:
        """`lea dst, [rip + label]` for a rodata label."""
        self.emit(_rex(True, dst, 0), 0x8D, 0x05 | (dst & 7) << 3)
        self._rel32(label, is_data=True)

    def add(self, dst: int, src: int) -> None:
        self._rr(0x01, dst, src)

    def sub(self, dst: int, src: int) -> None:
        self._rr(0x29, dst, src)

    def cmp(self, a: int, b: int) -> None:
        self._rr(0x39, a, b)

    def test(self, a: int, b: int) -> None:
        self._rr(0x85, a, b)

    def imul(self, dst: int, src: int) -> None:
        """`imul dst, src` (signed; sets OF on overflow)."""
        self.emit(_rex(True, dst, src), 0x0F, 0xAF, 0xC0 | (dst & 7) << 3 | (src & 7))

    def _unary(self, ext: int, r: int) -> None:
        self.emit(_rex(True, 0, r), 0xF7, 0xC0 | ext << 3 | (r & 7))

    def neg(self, r: int) -> None:
        self._unary(3, r)

    def idiv(self, r: int) -> None:
        """Signed rdx:rax / r: quotient in rax, remainder in rdx."""
        self._unary(7, r)

    def div(self, r: int) -> None:
        """Unsigned rdx:rax / r."""
        self._unary(6, r)

    def cqo(self) -> None:
        """Sign-extends rax into rdx."""
        self.emit(0x48, 0x99)

    def inc(self, r: int) -> None:
        self.emit(_rex(True, 0, r), 0xFF, 0xC0 | (r & 7))

    def dec(self, r: int) -> None:
        self.emit(_rex(True, 0, r), 0xFF, 0xC8 | (r & 7))

    def setcc(self, cc: str, r: int) -> None:
        """Sets the low byte of ``r`` to 1 if the condition holds, else 0."""
        if r >= 4:
            self.emit(_rex(False, 0, r))
        self.emit(0x0F, 0x90 | CC[cc], 0xC0 | (r & 7))

    def movzx8(self, dst: int, src: int) -> None:
        """`movzx dst32, src8`."""
        rex = _rex(False, dst, src)
        if rex != 0x40 or src >= 4:
            self.emit(rex)
        self.emit(0x0F, 0xB6, 0xC0 | (dst & 7) << 3 | (src & 7))

    def xor_imm32(self, dst: int, value: int) -> None:
        """`xor r32, imm8` (flips Booleans)."""
        if dst >= 8:
            self.emit(0x41)
        self.emit(0x83, 0xF0 | (dst & 7), value & 0xFF)

    def sub_imm32_fixed(self, dst: int) -> int:
        """`sub dst, imm32` with the immediate patched later; returns its position."""
        self.emit(_rex(True, 0, dst), 0x81, 0xE8 | (dst & 7))
        pos = len(self.code)
        self.code += b"\0\0\0\0"
        return pos

    def patch32(self, pos: int, value: int) -> None:
        self.code[pos : pos + 4] = _i32(value)

    def leave(self) -> None:
        self.emit(0xC9)

    def xor32(self, dst: int, src: int) -> None:
        """`xor r32, r32`: the short way to zero a register."""
        self._rr(0x31, dst, src, w=False)

    def _alu_imm(self, ext: int, dst: int, value: int) -> None:
        if not _fits32(value):
            raise ValueError("immediate out of range")
        rex = _rex(True, 0, dst)
        if -128 <= value < 128:
            self.emit(rex, 0x83, 0xC0 | ext << 3 | (dst & 7), value & 0xFF)
        else:
            self.emit(rex, 0x81, 0xC0 | ext << 3 | (dst & 7))
            self.code += _i32(value)

    def add_imm(self, dst: int, value: int) -> None:
        self._alu_imm(0, dst, value)

    def sub_imm(self, dst: int, value: int) -> None:
        self._alu_imm(5, dst, value)

    def cmp_imm(self, dst: int, value: int) -> None:
        self._alu_imm(7, dst, value)

    def push(self, r: int) -> None:
        if r >= 8:
            self.emit(0x41)
        self.emit(0x50 + (r & 7))

    def pop(self, r: int) -> None:
        if r >= 8:
            self.emit(0x41)
        self.emit(0x58 + (r & 7))

    def call(self, label: str) -> None:
        self.emit(0xE8)
        self._rel32(label)

    def jmp(self, label: str) -> None:
        self.emit(0xE9)
        self._rel32(label)

    def jcc(self, cc: str, label: str) -> None:
        self.emit(0x0F, 0x80 | CC[cc])
        self._rel32(label)

    def ret(self) -> None:
        self.emit(0xC3)

    def syscall(self) -> None:
        self.emit(0x0F, 0x05)
