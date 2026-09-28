#!/usr/bin/env python3
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from split_cluster import NODES, Relay, close, init_cluster, paired_connection, receive, send, validate_proof

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
        await send(writer, {'kind': 'control', 'node': 'iphone', 'token': 'incorrect'})
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        await close(writer); self.assertEqual(self.relay.nodes, {})
    async def test_unsolicited_data_rejected(self):
        reader, writer = await paired_connection(self.pair)
        await send(writer, {'kind': 'data', 'node': 'iphone', 'token': self.pair['token'], 'channel': '0' * 32})
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        await close(writer)

if __name__ == '__main__': unittest.main()
