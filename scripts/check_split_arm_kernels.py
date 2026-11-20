#!/usr/bin/env python3
"""Check that an ARM build of the split runtime really contains the kernels it was asked for (speed experiment E4).

Asking ggml for `-march=armv8.2-a+dotprod` does nothing visible if the flag is dropped on the way to the
compiler, and a build that silently ignored it would be timed as though it had changed. So this disassembles the
CPU backend archive and counts the instructions only those features introduce:

    sdot   dotprod   (Android phone: Cortex-A78, no i8mm)
    smmla  i8mm      (iPhone: A18, has both)

Three outcomes are errors, each with its own exit code:
  2  the baseline already contains an expected instruction, so it is not a control and proves nothing
  1  the variant is missing an expected instruction: the flag did not reach the compiler
  3  the variant contains an instruction that was not asked for, which would crash a CPU without it

Only the standard library; `--objdump` is a command line, so `xcrun llvm-objdump` works on a Mac.
"""
import argparse, re, shlex, subprocess, sys

FEATURES = {'sdot': 'dotprod', 'smmla': 'i8mm'}

def count(objdump, lib):
    out = subprocess.run([*shlex.split(objdump), '-d', str(lib)], check=True, capture_output=True, text=True).stdout
    return {m: len(re.findall(rf'\b{m}\b', out)) for m in FEATURES}

def check(baseline, variant, expect):
    """Return (exit_code, message) for two {mnemonic: count} dicts and the mnemonics the variant must have."""
    for m in expect:
        if baseline[m]:
            return 2, f'baseline already has {baseline[m]} {m} ({FEATURES[m]}); it is not a control'
    for m in expect:
        if not variant[m]:
            return 1, f'variant has no {m}: the {FEATURES[m]} flag did not reach the compiler'
    for m in FEATURES:
        if m not in expect and variant[m]:
            return 3, f'variant has {variant[m]} {m} ({FEATURES[m]}) that was not asked for; a CPU without it would crash'
    return 0, 'ok'

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--objdump', required=True, help='disassembler command, e.g. llvm-objdump or "xcrun llvm-objdump"')
    p.add_argument('--baseline', required=True, help='libggml-cpu.a built with no ARM arch')
    p.add_argument('--variant', required=True, help='libggml-cpu.a built with the ARM arch under test')
    p.add_argument('--expect', nargs='+', required=True, choices=sorted(FEATURES), help='instructions the variant must contain')
    a = p.parse_args()
    base, var = count(a.objdump, a.baseline), count(a.objdump, a.variant)
    print(f'baseline: {base}\nvariant:  {var}')
    code, message = check(base, var, a.expect)
    print(message)
    return code

if __name__ == '__main__': sys.exit(main())
