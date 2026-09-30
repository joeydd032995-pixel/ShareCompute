#!/usr/bin/env python3
import base64
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from split_launcher import TestKit, make_handler, pairing_code, ThreadingHTTPServer, instance_lock

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
if __name__=='__main__':unittest.main()
