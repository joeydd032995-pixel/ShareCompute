#!/usr/bin/env python3
"""Build the same pinned native runtime on desktop, Android and iOS."""
import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REV = (ROOT / 'native/split/llama-revision.txt').read_text().strip()

def run(*args, cwd=None):
    subprocess.run([str(x) for x in args], cwd=cwd, check=True)

# Applied in order; each is a diff against the tree the previous ones produce.
PATCHES = [ROOT / 'native/split/llama-budget.patch', ROOT / 'native/split/llama-cache.patch']

def patched_tree(source, patches):
    """The tree HEAD becomes with these patches applied, built in a scratch index."""
    index = source / '.git' / 'sc-patch-index'
    env = {**os.environ, 'GIT_INDEX_FILE': str(index)}
    try:
        subprocess.run(['git', 'read-tree', 'HEAD'], cwd=source, env=env, check=True)
        if patches: subprocess.run(['git', 'apply', '--cached', *map(str, patches)], cwd=source, env=env, check=True)
        return subprocess.check_output(['git', 'write-tree'], cwd=source, env=env, text=True).strip()
    finally:
        index.unlink(missing_ok=True)

def apply_patches(source):
    """Bring the checkout to HEAD plus every patch, from HEAD or from any earlier prefix of them."""
    for done in range(len(PATCHES), -1, -1):
        if subprocess.run(['git', 'diff', '--quiet', patched_tree(source, PATCHES[:done])], cwd=source).returncode == 0:
            if PATCHES[done:]: run('git', 'apply', *PATCHES[done:], cwd=source)
            return
    raise SystemExit(f'{source} has changes that are not ShareCompute patches; remove it to start clean')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--platform', choices=['desktop', 'android', 'ios', 'ios-simulator'], default='desktop')
    p.add_argument('--source', type=Path, default=ROOT / '.deps/llama-split')
    p.add_argument('--out', type=Path)
    p.add_argument('--ndk', type=Path)
    p.add_argument('--jobs', type=int, default=2)
    a = p.parse_args()
    source = a.source.resolve(); out = (a.out or ROOT / 'build' / a.platform).resolve()
    if not source.exists():
        source.mkdir(parents=True)
        run('git', 'init', source)
        run('git', 'remote', 'add', 'origin', 'https://github.com/ggml-org/llama.cpp.git', cwd=source)
        run('git', 'fetch', '--depth', '1', 'origin', REV, cwd=source)
        run('git', 'checkout', '--detach', 'FETCH_HEAD', cwd=source)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if head != REV: raise SystemExit(f'Wrong llama.cpp revision: {head}; expected {REV}')
    apply_patches(source)
    flags = ['-DCMAKE_BUILD_TYPE=Release', f'-DLLAMA_SOURCE_DIR={source}']
    ios = a.platform.startswith('ios')
    if ios:
        sdk = 'iphoneos' if a.platform == 'ios' else 'iphonesimulator'
        flags += ['-G', 'Xcode', '-DSC_IOS=ON', '-DCMAKE_SYSTEM_NAME=iOS', f'-DCMAKE_OSX_SYSROOT={sdk}',
                  '-DCMAKE_OSX_ARCHITECTURES=arm64', '-DCMAKE_OSX_DEPLOYMENT_TARGET=16.0', '-DCMAKE_XCODE_ATTRIBUTE_CODE_SIGNING_ALLOWED=NO']
    elif a.platform == 'android':
        flags += ['-DGGML_LLAMAFILE=OFF']
        if a.ndk:
            flags += [f'-DCMAKE_TOOLCHAIN_FILE={a.ndk.resolve()}/build/cmake/android.toolchain.cmake',
                      '-DANDROID_ABI=arm64-v8a', '-DANDROID_PLATFORM=android-28', '-DANDROID_STL=c++_static', '-DANDROID_SUPPORT_FLEXIBLE_PAGE_SIZES=ON']
        elif not os.environ.get('ANDROID_ROOT'):
            raise SystemExit('Use --ndk on a build host, or build natively inside Termux')
    run('cmake', '-S', ROOT / 'native/split', '-B', out, *flags)
    targets = ['sc-worker-core'] if ios else ['sc-rpc-worker', 'sc-split-probe']
    run('cmake', '--build', out, '--config', 'Release', '--parallel', a.jobs, '--target', *targets)
    (out / 'runtime-revision.txt').write_text(REV + '\n')
    license_text = (source / 'LICENSE').read_text(encoding='utf-8')
    notices = 'llama.cpp / ggml\n' + license_text
    notices += '\nJSON for Modern C++\nCopyright (c) 2013-2025 Niels Lohmann <https://nlohmann.me>\n\n'
    notices += license_text[license_text.index('Permission is hereby granted'):]
    # CPU builds may use this MIT-licensed kernel. Preserve its complete notice.
    kernel = (source / 'ggml/src/ggml-cpu/llamafile/sgemm.cpp').read_text(encoding='utf-8')
    notices += '\nllamafile CPU kernel\n' + kernel[:kernel.index('#include')]
    (out / 'THIRD_PARTY_NOTICES.txt').write_text(notices, encoding='utf-8')
    if ios:
        (out / 'RuntimeRevision.swift').write_text(f'let runtimeRevision = "{REV}"\n')

if __name__ == '__main__': main()
