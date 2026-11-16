import sys
from binxref import *
b=Bin("/mnt/user-data/uploads/libminecraftpe.so")
def probe(s, maxshow=8):
    sv=b.find_string(s)
    if sv<0: print(f"{s!r}: not found"); return
    fns={}
    for r in b.refs_to(sv): fns.setdefault(b.fn(int(r)),0); fns[b.fn(int(r))]+=1
    for p in b.data_ptrs_to(sv):
        for r in b.refs_to(p-64,p+8): fns.setdefault(b.fn(int(r)),0); fns[b.fn(int(r))]+=1
    out=[(f,b.fn_size(f),len(b.data_ptrs_to(f))) for f in sorted(fns)]
    print(f"{s!r}: {len(out)} fn  " + "  ".join(f"{f:#x}(sz{sz},vt{vt})" for f,sz,vt in out[:maxshow]))
for s in sys.argv[1:]: probe(s)
