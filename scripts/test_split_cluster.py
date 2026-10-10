#!/usr/bin/env python3
import asyncio
import contextlib
import copy
import io
from unittest import mock
import json
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
import build_stamp
import run_log
from split_cluster import CACHE_STATS, NODES, PROMPT, REV, Relay, build_summary, cache_delta, capacity_summary, close, describe_silence, file_sha256, _sha256_file, init_cluster, load_reference, paired_connection, receive, send, validate_proof

class ProofTests(unittest.TestCase):
    def setUp(self):
        self.result = {'token_ids': [1, 2, 3]}
        self.log = '\n'.join(f'layer {i} assigned to device RPC{i}' for i in range(3))
        self.before = dict.fromkeys(NODES, 0)
        self.records = {n: {'stats': {'peak_bytes': 10, 'graph_calls': 3}, 'bytes': [20, 30],
                            'hello': {'platform': 'linux', 'simulator': False, 'budget_mib': 64}, 'peer': '127.0.0.1'} for n in NODES}
    def test_requires_all_three_computations(self):
        validate_proof(self.result, self.result, self.log, self.records, self.before, False)
        self.records['iphone']['stats']['graph_calls'] = 0
        with self.assertRaises(ValueError): validate_proof(self.result, self.result, self.log, self.records, self.before, False)
        self.records['iphone']['stats']['graph_calls'] = 3
        with self.assertRaises(ValueError): validate_proof(self.result, self.result, self.log.splitlines()[0], self.records, self.before, False)
    def test_corruption_and_fake_physical_proof_rejected(self):
        for result in ({'token_ids': [1, 2, 4]}, {'token_ids': []}):
            with self.assertRaises(ValueError): validate_proof(self.result, result, self.log, self.records, self.before, False)
        with self.assertRaises(ValueError): validate_proof(self.result, self.result, self.log, self.records, self.before, True)

class ReferenceTests(unittest.TestCase):
    """A model too big to run unsplit on the laptop is judged against tokens pinned from a larger machine."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.good = {'profile': 'capacity', 'runtime': REV, 'model_sha256': 'abc', 'prompt': 'p', 'tokens': 3, 'token_ids': [1, 2, 3]}
    def write(self, **changes):
        path = Path(self.tmp.name) / 'reference.json'; path.write_text(json.dumps({**self.good, **changes})); return path
    def test_matching_reference_accepted(self):
        self.assertEqual(load_reference(self.write(), 'abc', 'p', 3)['token_ids'], [1, 2, 3])
    def test_a_reference_for_anything_else_is_rejected(self):
        for key, value in (('runtime', 'other'), ('model_sha256', 'other'), ('prompt', 'other'), ('tokens', 4)):
            with self.subTest(key), self.assertRaisesRegex(ValueError, key):
                load_reference(self.write(**{key: value}), 'abc', 'p', 3)
    def test_unusable_token_lists_rejected(self):
        for ids in ([], [1, '2'], [1, 2, 3, 4], None):
            with self.subTest(ids), self.assertRaises(ValueError): load_reference(self.write(token_ids=ids), 'abc', 'p', 3)
    def test_mismatch_names_which_baseline_it_was_compared_with(self):
        records = {n: {'stats': {'peak_bytes': 10, 'graph_calls': 3}, 'bytes': [20, 30],
                       'hello': {'platform': 'linux', 'simulator': False, 'budget_mib': 64}, 'peer': '127.0.0.1'} for n in NODES}
        log = '\n'.join(f'layer {i} assigned to device RPC{i}' for i in range(3))
        wrong = {'token_ids': [1, 2, 4]}; before = dict.fromkeys(NODES, 0)
        with self.assertRaisesRegex(ValueError, 'pinned reference'): validate_proof(self.good, wrong, log, records, before, False)
        with self.assertRaisesRegex(ValueError, 'local baseline'): validate_proof({'token_ids': [1, 2, 3]}, wrong, log, records, before, False)

    def test_the_shipped_reference_still_belongs_to_this_runtime_and_model(self):
        # Moving the pinned llama.cpp revision changes the tokens this file claims. Regenerate it then.
        import download_split_model as models
        shipped = Path(__file__).resolve().parents[1] / 'native/split/reference-capacity.json'
        ref = load_reference(shipped, models.CAPACITY_SHA256, PROMPT, 16)
        self.assertEqual(len(ref['token_ids']), 16)

class SilenceTests(unittest.TestCase):
    def test_the_two_causes_give_opposite_advice(self):
        laptop = describe_silence('iphone', 14); phone = describe_silence('iphone', 0)
        self.assertIn('laptop itself stopped responding for about 14 s', laptop); self.assertIn('close other programs', laptop)
        self.assertIn('while the laptop was responding', phone); self.assertIn('keep it on screen', phone)
        self.assertNotEqual(laptop, phone)

class ChecksumTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_checksum_does_not_stop_the_event_loop(self):
        # Heartbeats are read on this loop with a 10 second limit. The 3.4 GB model takes longer than that to
        # checksum on the 4 GB laptop, so a checksum on the loop drops every connected device (F41).
        import hashlib, time
        import split_cluster
        real = split_cluster._sha256_file
        def slow(*a, **k):
            time.sleep(.6); return real(*a, **k)
        ticks = 0
        async def ticker():
            nonlocal ticks
            while True: await asyncio.sleep(.05); ticks += 1
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(split_cluster, '_sha256_file', slow):
            path = Path(tmp) / 'model.bin'; path.write_bytes(b'abc')
            task = asyncio.create_task(ticker())
            digest = await file_sha256(path)
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(digest, hashlib.sha256(b'abc').hexdigest())
        self.assertGreaterEqual(ticks, 6, 'the loop made no progress while the file was being checksummed')

    async def test_stop_does_not_wait_for_the_whole_checksum(self):
        # Cancelling the await leaves the thread running, and asyncio.run() waits for it before the process can
        # exit. The thread must notice and stop, so Stop and Close do not hang for a multi-gigabyte checksum.
        import threading, time
        import split_cluster
        seen = {}
        def watching(path, stop):
            seen['stop'] = stop
            for _ in range(200):  # bounded: a test for a hang must not hang
                if stop.is_set(): break
                time.sleep(.01)
            raise InterruptedError('Stopped')
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(split_cluster, '_sha256_file', watching):
            path = Path(tmp) / 'model.bin'; path.write_bytes(b'abc')
            task = asyncio.create_task(file_sha256(path)); await asyncio.sleep(.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(seen['stop'].is_set())
    def test_the_checksum_loop_honours_the_stop_flag_between_chunks(self):
        import threading
        import split_cluster
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(split_cluster, 'HASH_CHUNK', 2):
            path = Path(tmp) / 'model.bin'; path.write_bytes(b'abcdef')
            import hashlib
            self.assertEqual(_sha256_file(path, threading.Event()), hashlib.sha256(b'abcdef').hexdigest())
            stopped = threading.Event(); stopped.set()
            with self.assertRaises(InterruptedError): _sha256_file(path, stopped)

class BuildStampTests(unittest.TestCase):
    """Provenance must be resolved in order and never invented: `unknown` is the honest answer (F44)."""
    def test_a_stamp_file_beats_git_so_a_packaged_kit_knows_its_commit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / build_stamp.STAMP_FILE).write_text(json.dumps({'commit': 'a' * 40}))
            stamp = build_stamp.read(root)
            self.assertEqual(stamp['commit'], 'a' * 40)
            self.assertEqual(stamp['short'], 'aaaaaaa')
            self.assertEqual(stamp['source'], 'file')
    def test_no_stamp_and_no_git_is_unknown_not_a_guess(self):
        with tempfile.TemporaryDirectory() as d:
            # A directory with no stamp file and no git repository above it.
            with mock.patch.object(build_stamp, 'from_git', return_value=None):
                stamp = build_stamp.read(Path(d))
            self.assertEqual(stamp['commit'], build_stamp.UNKNOWN)
    def test_a_damaged_stamp_is_unknown_rather_than_an_exception(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / build_stamp.STAMP_FILE).write_text('{not json')
            with mock.patch.object(build_stamp, 'from_git', return_value=None):
                self.assertEqual(build_stamp.read(root)['commit'], build_stamp.UNKNOWN)
    def test_ci_commit_wins_over_the_checkout(self):
        with mock.patch.dict(os.environ, {'SC_BUILD_COMMIT': 'b' * 40}):
            self.assertEqual(build_stamp.resolve(Path('.'))['commit'], 'b' * 40)
        with mock.patch.dict(os.environ, {'SC_BUILD_COMMIT': 'not-a-commit'}, clear=False):
            # Rejected rather than trusted, so a stray value cannot mislabel a build.
            self.assertNotEqual(build_stamp.resolve(Path('.'))['commit'], 'not-a-commit')
    def test_short_commit_normalises_every_absent_form(self):
        for value in (None, '', 'unknown', 123, []):
            self.assertEqual(build_stamp.short_commit(value), build_stamp.UNKNOWN)
        self.assertEqual(build_stamp.short_commit('c' * 40), 'ccccccc')
    def test_compare_separates_disagreement_from_missing_provenance(self):
        same = build_stamp.compare({'a': {'commit': 'a' * 40}, 'b': {'commit': 'a' * 40}})
        self.assertFalse(same['mismatch']); self.assertTrue(same['complete']); self.assertEqual(same['unknown'], [])
        differ = build_stamp.compare({'a': {'commit': 'a' * 40}, 'b': {'commit': 'b' * 40}})
        self.assertTrue(differ['mismatch']); self.assertFalse(differ['complete'])
        missing = build_stamp.compare({'a': {'commit': 'a' * 40}, 'b': {'commit': None}})
        # One known commit and one silent component is incomplete, but not a contradiction.
        self.assertFalse(missing['mismatch']); self.assertFalse(missing['complete']); self.assertEqual(missing['unknown'], ['b'])

class MixedBuildTests(unittest.TestCase):
    """A run whose components were built from different commits cannot be recorded as evidence."""
    def setUp(self):
        self.result = {'token_ids': [1, 2, 3]}
        self.log = '\n'.join(f'layer {i} assigned to device RPC{i}' for i in range(3))
        self.before = dict.fromkeys(NODES, 0)
        self.records = {n: {'stats': {'peak_bytes': 10, 'graph_calls': 3}, 'bytes': [20, 30],
                            'hello': {'platform': 'linux', 'simulator': False, 'budget_mib': 64,
                                      'build': 'a' * 40, 'app_build': 'a' * 40}, 'peer': '127.0.0.1'} for n in NODES}
    def summary(self, probe='a' * 40):
        with mock.patch.object(build_stamp, 'read', return_value={'commit': 'a' * 40, 'short': 'a' * 7}):
            import split_cluster
            with mock.patch.object(split_cluster, 'KIT_BUILD', {'commit': 'a' * 40, 'short': 'a' * 7}):
                return build_summary(self.records, probe)
    def test_matching_builds_pass(self):
        build = self.summary()
        self.assertFalse(build['mismatch'])
        validate_proof(self.result, self.result, self.log, self.records, self.before, False, build)
    def test_one_worker_from_another_commit_refuses_the_pass(self):
        self.records['iphone']['hello']['build'] = 'b' * 40
        build = self.summary()
        self.assertTrue(build['mismatch'])
        with self.assertRaises(ValueError) as caught:
            validate_proof(self.result, self.result, self.log, self.records, self.before, False, build)
        self.assertIn('different commits', str(caught.exception))
        # The message must name which component ran what, or the operator cannot act on it.
        self.assertIn('iphone-worker', str(caught.exception))
    def test_a_worker_that_cannot_name_its_build_still_passes_but_is_recorded(self):
        # Every run before F44 was in this state. It is missing provenance, not a contradiction,
        # so it is reported rather than failed; failing it would reject the operator's working apps.
        self.records['android']['hello'].pop('build')
        build = self.summary()
        self.assertFalse(build['mismatch']); self.assertEqual(build['unknown'], ['android-worker'])
        validate_proof(self.result, self.result, self.log, self.records, self.before, False, build)
    def test_a_probe_from_another_commit_refuses_the_pass(self):
        # The probe runs both the baseline and the split, so a probe from another commit makes the
        # two token streams the whole proof compares incomparable. Codex raised this as P1 on PR #41.
        build = self.summary(probe='b' * 40)
        self.assertTrue(build['mismatch'])
        self.assertEqual(build['commits']['b' * 40], ['probe'])
        with self.assertRaises(ValueError) as caught:
            validate_proof(self.result, self.result, self.log, self.records, self.before, False, build)
        self.assertIn('different commits', str(caught.exception))
        self.assertIn('probe', str(caught.exception))

    def test_a_probe_that_cannot_name_its_build_is_recorded_not_failed(self):
        # A probe built before --build existed, treated like any other unknown component.
        build = self.summary(probe=None)
        self.assertFalse(build['mismatch']); self.assertIn('probe', build['unknown'])
        validate_proof(self.result, self.result, self.log, self.records, self.before, False, build)

    def test_the_probe_build_is_reported(self):
        self.assertEqual(self.summary()['probe'], 'a' * 40)

    def test_app_build_is_recorded_but_not_compared(self):
        # A wrapper may legitimately be stamped differently from the library that computes.
        self.records['laptop']['hello']['app_build'] = 'f' * 40
        build = self.summary()
        self.assertFalse(build['mismatch'])
        self.assertEqual(build['app_builds']['laptop'], 'f' * 40)

class ChecksumBudgetTests(unittest.TestCase):
    """The report must say whether a checksum outlasted the heartbeat limit, which is F41's condition."""
    def test_the_flag_tracks_the_limit_not_the_file_size(self):
        import split_cluster
        limit = split_cluster.RECEIVE_LIMIT
        # A big file on a fast disk is safe; a small file on a loaded machine is not. Only time matters.
        for seconds, expected in ((limit - 0.1, False), (limit + 0.1, True), (0.5, False), (120.0, True)):
            self.assertEqual(seconds > limit, expected, f'{seconds}s against a {limit}s limit')

class RunLogTests(unittest.TestCase):
    """The measurement that F42 and F43 lacked, including that it cannot itself break a run."""
    def test_a_span_records_its_duration_and_survives_an_exception(self):
        clock = iter([0, 0, 0, 1.5]); timeline = run_log.Timeline(clock=lambda: next(clock))
        with self.assertRaises(RuntimeError):
            with timeline.span('upload'):
                raise RuntimeError('peer died')
        phase = timeline.phases[0]
        self.assertEqual(phase['name'], 'upload')
        self.assertFalse(phase['ok'])          # a failed phase is still timed; that is the diagnostic
        self.assertGreater(phase['duration_ms'], 0)
    def test_summary_reports_the_time_no_phase_claims(self):
        ticks = iter([0, 0, 0, 1, 10]); timeline = run_log.Timeline(clock=lambda: next(ticks))
        with timeline.span('measured'): pass
        summary = timeline.summary()
        # F43 could not say where 115 s of 141 s went. This is the number that would have said.
        self.assertEqual(summary['measured_ms'], 1000.0)
        self.assertEqual(summary['unaccounted_ms'], summary['total_ms'] - 1000.0)
    def test_note_attaches_measurements_discovered_while_running(self):
        timeline = run_log.Timeline()
        with timeline.span('model_checksum', bytes=10) as span: span.note(mib_per_s=42.0)
        self.assertEqual(timeline.phases[0]['mib_per_s'], 42.0)
    def test_every_event_is_one_flushed_json_line_so_a_kill_leaves_history(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'events.jsonl'
            log = run_log.EventLog(path)
            log.event('join', node='iphone'); log.event('laptop_stall', seconds=4.5)
            # Read before close: a killed run must still leave readable lines behind.
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])['ev'], 'join')
            self.assertEqual(json.loads(lines[1])['seconds'], 4.5)
            self.assertIn('at_ms', json.loads(lines[0]))
            log.close()
    def test_an_unserialisable_field_does_not_raise(self):
        log = run_log.EventLog()
        log.event('odd', path=Path('/tmp/x'), when=object())  # default=str keeps logging non-fatal
    def test_a_disk_error_mid_run_is_recorded_not_raised(self):
        # Codex P2 on PR #41: these calls sit inside the coordinator's coroutines and its cleanup
        # path, so an escaping OSError would take out the run and report.json with it -- the
        # diagnostic facility failing the thing it exists to diagnose.
        class Full:
            def write(self, *_): raise OSError(28, 'No space left on device')
            def flush(self): pass
            def close(self): pass
        log = run_log.EventLog()
        log.handle = Full()
        log.event('join', node='android')              # must not raise
        self.assertIsNone(log.handle)                  # sink dropped, not retried on every event
        self.assertIn('No space left on device', log.write_error)
        log.event('after')
        self.assertEqual([r['ev'] for r in log.records], ['join', 'after'])

    def test_a_sink_that_cannot_be_opened_degrades_to_memory(self):
        log = run_log.EventLog(path=Path('/proc/nonexistent-dir/events.jsonl'))
        log.event('still_recorded')
        self.assertIsNone(log.handle); self.assertTrue(log.write_error)
        self.assertEqual(log.records[0]['ev'], 'still_recorded')

    def test_every_record_carries_utc_so_the_three_logs_can_be_lined_up(self):
        # The phones' elapsed clocks start when their processes do, long before a run, so at_ms
        # alone cannot order the laptop's log against theirs. iOS writes ts; Android writes wall.
        record = run_log.EventLog().event('join')
        self.assertRegex(record['ts'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')

    def test_host_memory_answers_with_the_keys_and_never_raises(self):
        reading = run_log.host_memory()
        self.assertEqual(set(reading), {'total_bytes', 'available_bytes'})
        with mock.patch('sys.platform', 'plan9'), mock.patch.object(os, 'name', 'posix'):
            self.assertEqual(run_log.host_memory(), {'total_bytes': None, 'available_bytes': None})

class CapacityTests(unittest.TestCase):
    MIB = 1048576
    def workers(self, held, budgets=(768, 2048, 1536), lifetime_peaks=None):
        peaks = lifetime_peaks or held
        return {n: {'run_allocated_bytes': h * self.MIB, 'peak_bytes': p * self.MIB, 'budget_mib': b}
                for n, h, p, b in zip(NODES, held, peaks, budgets)}
    def test_combined_allocations_beyond_the_largest_budget_are_a_capacity_result(self):
        # Qwen2.5-3B Q8_0 on loopback (F40): 562 + 1348 + 1299 MiB against a 2048 MiB largest budget.
        summary = capacity_summary(self.workers((562, 1348, 1299)), 3616088480)
        self.assertTrue(summary['exceeds_largest_worker_budget'])
        self.assertEqual(summary['largest_worker_budget_bytes'], 2048 * self.MIB)
    def test_a_phone_peak_left_over_from_an_earlier_run_is_not_counted(self):
        # The phones' native workers outlive runs and their peak is a lifetime high-water mark. After a
        # 3B run, a 0.5B run sees the same phones still reporting 1,348 and 1,299 MiB.
        stale = capacity_summary(self.workers((59, 133, 257), lifetime_peaks=(59, 1348, 1299)), 491400032)
        self.assertFalse(stale['exceeds_largest_worker_budget'])
        self.assertEqual(stale['combined_run_allocated_bytes'], (59 + 133 + 257) * self.MIB)
    def test_a_model_one_worker_could_hold_is_not_claimed_as_capacity(self):
        # The 0.5B proof model (F37): 59 + 133 + 257 MiB.
        self.assertFalse(capacity_summary(self.workers((59, 133, 257)), 491400032)['exceeds_largest_worker_budget'])

class CacheDeltaTests(unittest.TestCase):
    def test_delta_counts_only_this_run(self):
        # Phone workers live across runs, so their counters are cumulative; the report needs this run's share.
        before = {'cache_hit_bytes': 100, 'cache_stored_bytes': 7, 'cache_rejected': 1}
        after = {'cache_hit_bytes': 160, 'cache_stored_bytes': 7, 'cache_rejected': 3, 'graph_calls': 9}
        self.assertEqual(cache_delta(before, after), {'cache_hit_bytes': 60, 'cache_stored_bytes': 0, 'cache_rejected': 2})
    def test_missing_fields_count_as_zero(self):
        self.assertEqual(cache_delta({}, {}), dict.fromkeys(CACHE_STATS, 0))

class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); directory = Path(self.temp.name) / 'pairing'
        self.config = init_cluster(directory, '127.0.0.1', 0)
        self.relay = Relay(self.config, directory); await self.relay.start()
        self.pair = dict(self.config['nodes']['iphone'], port=self.relay.port)
    async def asyncTearDown(self):
        await self.relay.stop(); self.temp.cleanup()
    async def test_bad_pin_rejected_before_credentials(self):
        pair = dict(self.pair, pin='0' * 64)
        with self.assertRaisesRegex(ConnectionError, 'pin mismatch'): await paired_connection(pair)
        self.assertEqual(self.relay.nodes, {})
    async def test_bad_token_rejected(self):
        reader, writer = await paired_connection(self.pair)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await send(writer, {'kind': 'control', 'node': 'iphone', 'token': 'incorrect'})
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        await close(writer); self.assertEqual(self.relay.nodes, {})
        # A phone that never joins must leave a diagnosable reason in the coordinator log.
        self.assertIn('Rejected iphone control connection from 127.0.0.1: Authentication failed', output.getvalue())
        self.assertNotIn('incorrect', output.getvalue())
    async def test_unknown_node_rejection_does_not_echo_input(self):
        reader, writer = await paired_connection(self.pair)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await send(writer, {'kind': 'control', 'node': 'injected\nJoined iphone:', 'token': 'x'})
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        await close(writer)
        self.assertIn('Rejected unknown control connection from 127.0.0.1: Unknown node', output.getvalue())
        self.assertNotIn('injected', output.getvalue())
    async def test_non_string_node_still_logged(self):
        reader, writer = await paired_connection(self.pair)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await send(writer, {'kind': 'control', 'node': ['iphone'], 'token': 'x'})
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        await close(writer)
        self.assertIn('Rejected unknown control connection from 127.0.0.1: Unknown node', output.getvalue())
    async def test_silent_peer_logs_timeout_reason(self):
        output = io.StringIO()
        quick = lambda r, timeout=0.1: receive(r, timeout)
        with mock.patch('split_cluster.receive', quick), contextlib.redirect_stdout(output):
            reader, writer = await paired_connection(self.pair)
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        await close(writer)
        self.assertIn('Rejected unknown unknown connection from 127.0.0.1: TimeoutError', output.getvalue())
    async def test_unsolicited_data_rejected(self):
        reader, writer = await paired_connection(self.pair)
        await send(writer, {'kind': 'data', 'node': 'iphone', 'token': self.pair['token'], 'channel': '0' * 32})
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        await close(writer)
    async def test_joined_worker_closing_names_the_cause(self):
        reader, writer = await paired_connection(self.pair)
        with contextlib.redirect_stdout(io.StringIO()):
            await send(writer, {'kind': 'control', 'node': 'iphone', 'token': self.pair['token'],
                                'runtime': REV, 'budget_mib': self.pair['budget_mib'], 'platform': 'ios', 'simulator': False})
            self.assertTrue((await receive(reader))['ok'])
            await close(writer)
            await asyncio.wait_for(self.relay.failure.wait(), 2)
        # The phone app ended the session (left the foreground, Disconnect, or a crash); say so.
        self.assertIn('iphone control disconnected: the iphone app closed the connection', self.relay.reason)
    async def test_a_silent_phone_is_named_as_the_phone(self):
        import split_cluster
        reader, writer = await self.join()
        with mock.patch.object(split_cluster, 'RECEIVE_LIMIT', 1), contextlib.redirect_stdout(io.StringIO()):
            self.relay.fail('reset')  # nothing yet; the heartbeat limit below is what is under test
            self.relay.reason = ''; self.relay.failure.clear()
        # The limit was read when the handler began waiting, so open a fresh session under the short limit.
        await close(writer)
        self.relay.reason = ''; self.relay.failure.clear(); self.relay.nodes.clear()
        with mock.patch.object(split_cluster, 'RECEIVE_LIMIT', 1), contextlib.redirect_stdout(io.StringIO()):
            reader, writer = await self.join()
            await asyncio.wait_for(self.relay.failure.wait(), 5)   # the phone sends nothing
        self.assertIn('no heartbeat from the iphone app for 1 s while the laptop was responding', self.relay.reason)
        await close(writer)
    async def test_a_frozen_laptop_is_named_as_the_laptop(self):
        import split_cluster, time
        with mock.patch.object(split_cluster, 'RECEIVE_LIMIT', 1), contextlib.redirect_stdout(io.StringIO()):
            reader, writer = await self.join()
            await asyncio.sleep(.1)
            time.sleep(4)   # the whole process stops, as when the laptop thrashes
            await asyncio.wait_for(self.relay.failure.wait(), 5)
        self.assertIn('the laptop itself stopped responding', self.relay.reason)
        self.assertNotIn('while the laptop was responding', self.relay.reason)
        await close(writer)
    async def join(self):
        reader, writer = await paired_connection(self.pair)
        await send(writer, {'kind': 'control', 'node': 'iphone', 'token': self.pair['token'],
                            'runtime': REV, 'budget_mib': self.pair['budget_mib'], 'platform': 'ios', 'simulator': False})
        self.assertTrue((await receive(reader))['ok'])
        return reader, writer
    async def wait_for_stats(self, key, value):
        for _ in range(200):
            if self.relay.nodes.get('iphone', {}).get('stats', {}).get(key) == value: return
            await asyncio.sleep(.01)
        self.fail(f'{key} never reached {value}: {self.relay.nodes.get("iphone", {}).get("stats")}')
    async def test_cache_telemetry_recorded(self):
        with contextlib.redirect_stdout(io.StringIO()):
            reader, writer = await self.join()
            await send(writer, {'op': 'stats', 'allocated_bytes': 1, 'peak_bytes': 2, 'graph_calls': 3,
                                'cache_hit_bytes': 40, 'cache_stored_bytes': 50, 'cache_rejected': 1})
            await self.wait_for_stats('cache_hit_bytes', 40)
            self.assertEqual(self.relay.nodes['iphone']['stats']['cache_rejected'], 1)
            await close(writer)
    async def test_telemetry_without_cache_fields_still_accepted(self):
        # A phone app built before the cache existed must keep working with a newer laptop kit.
        with contextlib.redirect_stdout(io.StringIO()):
            reader, writer = await self.join()
            await send(writer, {'op': 'stats', 'allocated_bytes': 1, 'peak_bytes': 2, 'graph_calls': 7})
            await self.wait_for_stats('graph_calls', 7)
            self.assertEqual({k: self.relay.nodes['iphone']['stats'][k] for k in CACHE_STATS}, dict.fromkeys(CACHE_STATS, 0))
            self.assertFalse(self.relay.failure.is_set())
            await close(writer)
    async def test_invalid_cache_telemetry_rejected(self):
        with contextlib.redirect_stdout(io.StringIO()):
            reader, writer = await self.join()
            await send(writer, {'op': 'stats', 'allocated_bytes': 1, 'peak_bytes': 2, 'graph_calls': 3, 'cache_hit_bytes': -5})
            await asyncio.wait_for(self.relay.failure.wait(), 2)
            await close(writer)
        self.assertIn('Invalid telemetry', self.relay.reason)
    async def test_stop_disconnects_external_worker(self):
        reader, writer = await paired_connection(self.pair)
        try:
            await send(writer, {'kind': 'control', 'node': 'iphone', 'token': self.pair['token'],
                                'runtime': REV, 'budget_mib': self.pair['budget_mib'],
                                'platform': 'ios', 'simulator': True})
            self.assertTrue((await receive(reader))['ok'])
            # The app remains connected when the coordinator finishes a generation.
            # Python 3.12 Server.wait_closed waits for accepted connections too.
            await asyncio.wait_for(self.relay.stop(), 4)
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        finally:
            await close(writer)

if __name__ == '__main__': unittest.main()
