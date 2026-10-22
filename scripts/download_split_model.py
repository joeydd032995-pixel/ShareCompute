#!/usr/bin/env python3
"""Immutable, checksum-verified models for the split: a 0.5B proof model and a 3B capacity model."""
import argparse
import hashlib
from pathlib import Path
import urllib.request

# Both files are pinned to a Hugging Face commit, so the bytes cannot change under the checksum.
URL = 'https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf'
SHA256 = '74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db'
# Qwen2.5-3B-Instruct at Q8_0. One worker needs 3,183 MiB to hold it, more than any worker budget
# (768, 2048, 1536 MiB), while the three budgets together (4,352 MiB) can. The Q4_K_M file of the
# same model (1,890 MiB on one worker) fits the Android budget alone and would prove nothing (F40).
CAPACITY_URL = 'https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/7dabda4d13d513e3e842b20f0d435c732f172cbe/qwen2.5-3b-instruct-q8_0.gguf'
CAPACITY_SHA256 = '6dcc22694c8654b045ec40bbe350212b88893fd9010e8474bae5b19a43578ba1'
# Speed-comparison models (not capacity proofs: each fits the Android budget alone). Both are pinned to a
# Hugging Face commit. Their reference tokens come from a single-process run of the pinned runtime on Linux
# x86-64, because neither the 4 GB laptop nor a quick local baseline can run them unsplit.
CODER_URL = 'https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/f86cb2c1fa58255f8052cc32aeede1b7482d4361/qwen2.5-coder-1.5b-instruct-q8_0.gguf'
CODER_SHA256 = '507de59046601282ba768a9789900e6ccf60ed93ddf346730b7c68eb0715bc47'
Q4_URL = 'https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/7dabda4d13d513e3e842b20f0d435c732f172cbe/qwen2.5-3b-instruct-q4_k_m.gguf'
Q4_SHA256 = '626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d'
PROFILES = {
    'proof': {'url': URL, 'sha256': SHA256, 'bytes': 491400032, 'label': 'test model', 'file': 'model.gguf',
              'probe_timeout': 300, 'reference': None},
    # The first run uploads about 2,580 MiB to the phones, so the generation is given an hour.
    'capacity': {'url': CAPACITY_URL, 'sha256': CAPACITY_SHA256, 'bytes': 3616088480, 'label': '3B capacity model',
                 'file': 'model-capacity.gguf', 'probe_timeout': 3600, 'reference': 'reference-capacity.json'},
    'coder': {'url': CODER_URL, 'sha256': CODER_SHA256, 'bytes': 1894532160, 'label': 'Coder 1.5B Q8_0 model',
              'file': 'model-coder.gguf', 'probe_timeout': 1800, 'reference': 'reference-coder.json'},
    'qwen3b-q4': {'url': Q4_URL, 'sha256': Q4_SHA256, 'bytes': 2104932768, 'label': '3B Q4_K_M model',
                  'file': 'model-qwen3b-q4.gguf', 'probe_timeout': 1800, 'reference': 'reference-qwen3b-q4.json'},
}

def digest(path):
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--profile', choices=sorted(PROFILES), default='proof')
    p.add_argument('--out', type=Path, default=None, help='default: models/split-<profile>.gguf')
    a = p.parse_args()
    profile = PROFILES[a.profile]; out = a.out or Path(f'models/split-{a.profile}.gguf')
    if out.exists():
        if digest(out) != profile['sha256']: raise SystemExit('Existing model has the wrong checksum; refusing to overwrite')
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        partial = out.with_suffix('.partial')
        try:
            with urllib.request.urlopen(profile['url'], timeout=60) as response, partial.open('xb') as f:
                while chunk := response.read(1024*1024): f.write(chunk)
            if digest(partial) != profile['sha256']: raise RuntimeError('Downloaded model checksum mismatch')
            partial.replace(out)
        finally:
            partial.unlink(missing_ok=True)
    print(out.resolve(), profile['sha256'])
if __name__ == '__main__': main()
