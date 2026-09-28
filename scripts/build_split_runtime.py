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
    patch = ROOT / 'native/split/llama-budget.patch'
    applied = subprocess.run(['git', 'apply', '--reverse', '--check', str(patch)], cwd=source, capture_output=True).returncode == 0
    if not applied:
        run('git', 'diff', '--exit-code', cwd=source)
        run('git', 'apply', '--check', patch, cwd=source)
        run('git', 'apply', patch, cwd=source)
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
                      '-DANDROID_ABI=arm64-v8a', '-DANDROID_PLATFORM=android-28', '-DANDROID_STL=c++_static']
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
