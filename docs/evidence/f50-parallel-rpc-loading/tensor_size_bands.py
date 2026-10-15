"""Share of a GGUF's weight bytes by tensor size, against RPC's hash threshold (F50).

usage: python3 -I tensor_size_bands.py MODEL.gguf   (needs .deps/llama-split/gguf-py from build_split_runtime.py)
token_embd.weight is reported separately: the probe keeps it on the host, not on an RPC endpoint.
"""
import sys
sys.path.insert(0, "/home/user/ShareCompute/.deps/llama-split/gguf-py")
from gguf import GGUFReader
MiB = 1048576
r = GGUFReader(sys.argv[1])
bands = {"<=1 MiB": [0, 0], "1-10 MiB": [0, 0], ">10 MiB": [0, 0]}
total = emb = 0
for t in r.tensors:
    s = int(t.n_bytes)
    if t.name == "token_embd.weight":
        emb = s; continue
    total += s
    k = "<=1 MiB" if s <= MiB else ("1-10 MiB" if s <= 10 * MiB else ">10 MiB")
    bands[k][0] += 1; bands[k][1] += s
print(f"tensors {len(r.tensors)}; total excluding token_embd {total/MiB:.1f} MiB; token_embd {emb/MiB:.1f} MiB")
for k, (n, b) in bands.items():
    print(f"{k:9s} {n:4d} tensors {b/MiB:8.1f} MiB {100*b/total:5.1f}% of bytes")
