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
from split_cluster import CACHE_STATS, NODES, REV, Relay, cache_delta, close, init_cluster, paired_connection, receive, send, validate_proof

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
