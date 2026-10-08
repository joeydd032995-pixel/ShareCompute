#!/usr/bin/env python3
import base64
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
from unittest import mock
import unittest
import zipfile
from split_launcher import TestKit, make_handler, pairing_code, ThreadingHTTPServer, instance_lock, rank_addresses, startup_banner

class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.kit=TestKit(Path(self.tmp.name));self.secret='test-secret'
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.kit,self.secret))
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,path='status',method='GET',headers=None):
        c=HTTPConnection('127.0.0.1',self.server.server_port);c.request(method,'/'+self.secret+'/'+path,body='{}' if method=='POST' else None,headers=headers or {});r=c.getresponse();result=(r.status,r.read());c.close();return result
    def test_local_session_and_origin_required(self):
        self.assertEqual(self.request()[0],200)
        self.assertEqual(self.request(headers={'Host':'attacker.invalid'})[0],400)
        self.assertEqual(self.request('../status')[0],400)
        self.assertEqual(self.request('stop','POST',{'Origin':'https://attacker.invalid'})[0],400)
        self.assertEqual(self.request('stop','POST',{'Origin':f'http://127.0.0.1:{self.server.server_port}'})[0],200)
        self.secret='wrong';self.assertEqual(self.request()[0],400)
    def test_pairing_qr_and_code(self):
        self.assertEqual(self.request('pair/android')[0],400)
        self.kit.pair_dir.mkdir();pair={'node':'android','token':'private','host':'192.168.1.2'}
        (self.kit.pair_dir/'android.json').write_text(json.dumps(pair));self.kit.state['phase']='running'
        encoded=json.loads(self.request('pair/android')[1])['code'][4:];self.assertEqual(json.loads(base64.urlsafe_b64decode(encoded+'='*((4-len(encoded)%4)%4))),pair)
        status,svg=self.request('qr/android');self.assertEqual(status,200);self.assertIn(b'<svg',svg)
        self.assertEqual(self.request('pair/../cluster')[0],400)
    def test_export_excludes_pairing_and_private_keys(self):
        out=Path(self.tmp.name)/'run';out.mkdir();self.kit.current_out=out
        (out/'report.json').write_text('{"status":"PASS"}');(out/'split.log').write_text('native graphs')
        (out/'cluster.json').write_text('SECRET');(out/'server.key').write_text('PRIVATE');(out/'pairing').mkdir();(out/'pairing'/'android.json').write_text('SECRET')
        with zipfile.ZipFile(io.BytesIO(self.kit.report_zip())) as z:self.assertEqual(set(z.namelist()),{'report.json','split.log'})
        self.kit.state['active']=True
        with self.assertRaises(ValueError):self.kit.report_zip()
    def test_second_launcher_cannot_use_same_data(self):
        with instance_lock(Path(self.tmp.name)):
            with self.assertRaises(RuntimeError):
                with instance_lock(Path(self.tmp.name)): pass
        with instance_lock(Path(self.tmp.name)): pass
    def test_live_worker_status(self):
        self.kit.current_out=Path(self.tmp.name)/'run';self.kit.state.update(active=True,phase='running')
        (self.kit.directory/'active.log').write_text('Joined laptop: linux\nJoined android: android\n')
        self.assertEqual(self.kit.snapshot()['workers'],['laptop','android'])
class ModelChoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.kit=TestKit(Path(self.tmp.name))
    def test_default_is_the_small_proof_model(self):
        self.assertEqual((self.kit.profile,self.kit.model_path.name),('proof','model.gguf'))
    def test_unknown_choice_rejected_before_anything_starts(self):
        with self.assertRaises(ValueError):self.kit.start('192.168.1.2','nope')
        self.assertFalse(self.kit.state['active'])
    def test_capacity_choice_keeps_its_own_model_file(self):
        # A separate file, so choosing it never evicts the 469 MiB model the quick test reuses.
        with mock.patch.object(TestKit,'prepare_and_run'):
            self.kit.start('192.168.1.2','capacity');self.kit.thread.join()
        self.assertEqual((self.kit.profile,self.kit.model_path.name),('capacity','model-capacity.gguf'))

class AddressTests(unittest.TestCase):
    def test_active_route_wins_over_virtual_adapters(self):
        # Wi-Fi on 10/8 must beat VirtualBox's host-only 192.168.56.1 and a WSL 172.x adapter.
        self.assertEqual(rank_addresses('10.0.0.20',['192.168.56.1','172.25.160.1','10.0.0.20']),
                         ['10.0.0.20','192.168.56.1','172.25.160.1'])
    def test_without_a_lan_route_home_ranges_come_first(self):
        # A tunnel route (CGNAT/public) is excluded, so the subnet decides among the rest.
        self.assertEqual(rank_addresses('100.101.2.3',['172.25.160.1','10.5.0.2','192.168.1.20']),
                         ['192.168.1.20','10.5.0.2','172.25.160.1'])
    def test_unreachable_addresses_excluded(self):
        self.assertEqual(rank_addresses('',['127.0.0.1','127.0.1.1','169.254.3.4','100.100.1.2','8.8.8.8','::1','fe80::1','junk']),[])
    def test_banner_names_address_and_alternatives(self):
        text=startup_banner(['192.168.1.20','172.25.160.1'],'http://127.0.0.1:1/s/')
        self.assertIn('Laptop Wi-Fi address: 192.168.1.20',text);self.assertIn('172.25.160.1',text);self.assertIn('http://127.0.0.1:1/s/',text)
        self.assertIn('not detected',startup_banner([],'http://127.0.0.1:1/s/'))
    def test_status_offers_detected_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            kit=TestKit(Path(tmp));kit.state.update(host='192.168.1.20',hosts=['192.168.1.20','10.8.0.6'])
            snapshot=kit.snapshot();self.assertEqual(snapshot['hosts'],['192.168.1.20','10.8.0.6'])
if __name__=='__main__':unittest.main()
