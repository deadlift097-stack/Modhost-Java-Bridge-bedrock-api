"""Shared helpers: ELF sections, function table, ADRP xrefs (ADD/LDR/STR/PRFM follow-ups), RELATIVE-reloc data refs."""
import mmap, struct, re
import numpy as np

class Bin:
    def __init__(self, path):
        f = open(path, "rb"); self.mm = mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        shoff, = struct.unpack_from("<Q", mm, 0x28); shentsize, shnum, shstrndx = struct.unpack_from("<HHH", mm, 0x3A)
        raw = [struct.unpack_from("<IIQQQQIIQQ", mm, shoff + i * shentsize) for i in range(shnum)]
        stro = raw[shstrndx][4]; self.secs = {}
        for r in raw:
            n = mm[stro + r[0]: mm.find(b"\0", stro + r[0])].decode(); self.secs[n] = (r[3], r[4], r[5])
        self.t_addr, self.t_off, self.t_size = self.secs[".text"]
        self.text = mm[self.t_off:self.t_off + self.t_size]
        ha, ho, hs = self.secs[".eh_frame_hdr"]
        cnt, = struct.unpack_from("<I", mm, ho + 8)
        tab = np.frombuffer(mm, dtype="<i4", count=cnt * 2, offset=ho + 12).reshape(-1, 2)
        self.starts = tab[:, 0].astype(np.int64) + ha
        self.code = np.frombuffer(mm, dtype="<u4", count=self.t_size // 4, offset=self.t_off)
        self._scan()

    def _scan(self):
        code = self.code
        idx = np.nonzero((code & 0x9F000000) == 0x90000000)[0]
        idx = idx[idx + 1 < len(code)]
        a = code[idx]; rd = a & 31
        imm = (((a >> 5) & 0x7FFFF).astype(np.int64) << 2) | ((a >> 29) & 3).astype(np.int64)
        imm = np.where(imm & (1 << 20), imm - (1 << 21), imm)
        pc = self.t_addr + idx.astype(np.int64) * 4
        page = (pc & ~0xFFF) + (imm << 12)
        n = code[idx + 1]
        rn = (n >> 5) & 31
        isadd = ((n & 0xFFC00000) == 0x91000000) & (rn == rd)
        ldr64 = ((n & 0xFFC00000) == 0xF9400000) & (rn == rd)          # LDR Xt,[Xn,#imm*8]
        off = np.where(isadd, ((n >> 10) & 0xFFF).astype(np.int64), ((n >> 10) & 0xFFF).astype(np.int64) * 8)
        ok = isadd | ldr64
        self.xr_pc = pc[ok]; self.xr_t = (page + off)[ok]; self.xr_ldr = ldr64[ok]
        order = np.argsort(self.xr_t, kind="stable"); self.xr_pc = self.xr_pc[order]; self.xr_t = self.xr_t[order]; self.xr_ldr = self.xr_ldr[order]

    def refs_to(self, lo, hi=None):
        """code addresses that materialise an address in [lo, hi]"""
        hi = lo if hi is None else hi
        a = np.searchsorted(self.xr_t, lo, "left"); b = np.searchsorted(self.xr_t, hi, "right")
        return self.xr_pc[a:b]

    def fn(self, addr):
        i = np.searchsorted(self.starts, addr, "right") - 1
        return int(self.starts[i])

    def fn_size(self, start):
        i = np.searchsorted(self.starts, start, "left")
        return int(self.starts[i + 1] - start) if i + 1 < len(self.starts) else 0

    def find_string(self, s):
        p = self.mm.find(b"\0" + s.encode() + b"\0"); return p + 1 if p >= 0 else -1

    def data_ptrs_to(self, vaddr):
        """slots in the image that hold a pointer to vaddr (from R_AARCH64_RELATIVE relocs)"""
        if not hasattr(self, "_rel"):
            ra, ro, rs = self.secs[".rela.dyn"]
            r = np.frombuffer(self.mm, dtype=[("off", "<u8"), ("info", "<u8"), ("add", "<i8")], count=rs // 24, offset=ro)
            r = r[r["info"] == 1027]
            o = np.argsort(r["add"]); self._rel = r[o]
        a = np.searchsorted(self._rel["add"], vaddr, "left"); b = np.searchsorted(self._rel["add"], vaddr, "right")
        return [int(x) for x in self._rel["off"][a:b]]

def wild(w):
    return ((w & 0xFC000000) in (0x14000000, 0x94000000) or (w & 0x9F000000) in (0x90000000, 0x10000000)
            or (w & 0x7E000000) in (0x34000000, 0x36000000) or (w & 0xFF000010) == 0x54000000 or (w & 0x3B000000) == 0x18000000)

def pattern(b, addr, n):
    ws = struct.unpack_from("<%dI" % n, b.text, addr - b.t_addr)
    parts, rx = [], []
    for w in ws:
        if wild(w): parts += ["??"] * 4; rx.append(b".{4}")
        else:
            x = struct.pack("<I", w); parts += ["%02X" % c for c in x]; rx.append(re.escape(x))
    return " ".join(parts), b"".join(rx)

def unique_pattern(b, addr, sizes=(8, 12, 16, 24, 32, 48)):
    for n in sizes:
        pat, rx = pattern(b, addr, n)
        if len(re.findall(rx, b.text, re.DOTALL)) == 1: return pat, n
    return None, 0

def fn_strings(b, fa, limit=14):
    size = b.fn_size(fa); lo = np.searchsorted(b.xr_pc, 0)  # xr_pc is sorted by target, so filter
    m = (b.xr_pc >= fa) & (b.xr_pc < fa + size)
    out = []
    for t in sorted(set(int(x) for x in b.xr_t[m])):
        if 0x2419f40 <= t < 0x3786034:
            e = b.mm.find(b"\0", t, t + 160)
            s = b.mm[t:e]
            if 3 <= len(s) < 150 and all(32 <= c < 127 for c in s): out.append(s.decode())
    return out[:limit]

def vtable_of(b, mangled):
    """vtable function addresses for an RTTI name like '35ItemUseOnActorInventoryTransaction' (Itanium ABI)."""
    nv = b.find_string(mangled)
    if nv < 0: return None
    # typeinfo object = {vtable ptr, name ptr, [base...]}: the name pointer slot is at typeinfo+8
    ti = [s - 8 for s in b.data_ptrs_to(nv)]
    if not ti: return None
    vts = []
    for t in ti:
        for slot in b.data_ptrs_to(t):          # vtable[-1] holds the typeinfo pointer
            vts.append(slot + 8)
    if not vts: return None
    if not hasattr(b, "_slotmap"):
        r = b._rel; b._slotmap = dict(zip(r["off"].tolist(), r["add"].tolist()))
    out = []
    for v in vts[:1]:
        i = 0
        while i < 80 and (v + 8 * i) in b._slotmap and b.t_addr <= b._slotmap[v + 8 * i] < b.t_addr + b.t_size:
            out.append(b._slotmap[v + 8 * i]); i += 1
    return vts[0], out
