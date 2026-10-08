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
PROFILES = {
    'proof': {'url': URL, 'sha256': SHA256, 'bytes': 491400032, 'label': 'test model', 'file': 'model.gguf',
              'probe_timeout': 300},
    # The first run uploads about 2,580 MiB to the phones, so the generation is given an hour.
    'capacity': {'url': CAPACITY_URL, 'sha256': CAPACITY_SHA256, 'bytes': 3616088480, 'label': '3B capacity model',
                 'file': 'model-capacity.gguf', 'probe_timeout': 3600},
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
