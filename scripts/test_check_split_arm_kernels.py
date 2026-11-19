#!/usr/bin/env python3
"""Pin the decisions of check_split_arm_kernels.py, including each way it must refuse."""
import os, stat, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import check_split_arm_kernels as check

def counts(sdot=0, smmla=0): return {'sdot': sdot, 'smmla': smmla}

class Decisions(unittest.TestCase):
    def test_variant_with_what_was_asked_passes(self):
        self.assertEqual(check.check(counts(), counts(sdot=9), ['sdot'])[0], 0)
        self.assertEqual(check.check(counts(), counts(sdot=9, smmla=3), ['sdot', 'smmla'])[0], 0)
    def test_flag_that_never_reached_the_compiler_fails(self):
        self.assertEqual(check.check(counts(), counts(), ['sdot'])[0], 1)
        self.assertEqual(check.check(counts(), counts(sdot=9), ['sdot', 'smmla'])[0], 1)
    def test_a_baseline_that_already_has_the_instruction_is_not_a_control(self):
        self.assertEqual(check.check(counts(sdot=1), counts(sdot=9), ['sdot'])[0], 2)
    def test_an_instruction_nobody_asked_for_fails(self):
        # The Android phone has dotprod but not i8mm: smmla there would be a SIGILL.
        self.assertEqual(check.check(counts(), counts(sdot=9, smmla=2), ['sdot'])[0], 3)

class Counting(unittest.TestCase):
    def fake_objdump(self, directory, text):
        script = Path(directory) / 'objdump'
        script.write_text(f'#!{sys.executable}\nimport sys\nsys.stdout.write({text!r})\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)
    def test_counts_whole_mnemonics_in_both_syntaxes(self):
        text = '  0: 4e829400  sdot v0.4s, v1.16b, v2.16b\n  4: 4e82a400  sdot.4s v0, v1, v2\n  8: 4e80a400  smmla v0.4s, v1.16b, v2.16b\n  c: 0  sdotx v0\n  10: 0 vsdot\n'
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(check.count(self.fake_objdump(d, text), 'lib.a'), {'sdot': 2, 'smmla': 1})
    def test_command_line_exit_code_follows_the_decision(self):
        with tempfile.TemporaryDirectory() as d:
            tool = self.fake_objdump(d, 'x: sdot v0.4s, v1.16b, v2.16b\n')
            run = lambda *extra: subprocess.run([sys.executable, str(ROOT / 'check_split_arm_kernels.py'), '--objdump', tool,
                                                 '--baseline', 'a.a', '--variant', 'b.a', *extra], capture_output=True, text=True)
            # Both libraries contain sdot here, so as a control it is invalid.
            self.assertEqual(run('--expect', 'sdot').returncode, 2)

if __name__ == '__main__': unittest.main()
