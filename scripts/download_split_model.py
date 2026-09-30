#!/usr/bin/env python3
"""Immutable, checksum-verified 0.5B Q4_K_M model for the split proof."""
import argparse
import hashlib
from pathlib import Path
import urllib.request

URL = 'https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf'
SHA256 = '74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db'
def digest(path):
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()
def main():
    p = argparse.ArgumentParser(); p.add_argument('--out', type=Path, default=Path('models/split-proof.gguf')); a=p.parse_args()
    if a.out.exists():
        if digest(a.out) != SHA256: raise SystemExit('Existing model has the wrong checksum; refusing to overwrite')
    else:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        partial = a.out.with_suffix('.partial')
        try:
            with urllib.request.urlopen(URL, timeout=60) as response, partial.open('xb') as f:
                while chunk := response.read(1024*1024): f.write(chunk)
            if digest(partial) != SHA256: raise RuntimeError('Downloaded model checksum mismatch')
            partial.replace(a.out)
        finally:
            partial.unlink(missing_ok=True)
    print(a.out.resolve(), SHA256)
if __name__ == '__main__': main()
