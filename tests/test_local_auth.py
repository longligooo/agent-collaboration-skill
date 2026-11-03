import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_api
import ui_server
import collab

class LocalAuthTests(unittest.TestCase):
    def test_real_http_toggle_boundaries(self):
        for required in (True, False):
            with self.subTest(required=required), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'COLLAB_UI_HOME':tmp}):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
                service=Mock(); service.dispatch.return_value={'providers':[]}
                credentials=auto_api.Credentials(token_required=required)
                server=ui_server.make_server(port, auto_service=service, auto_credentials=credentials)
                thread=threading.Thread(target=server.serve_forever); thread.start()
                def request(path='/api/auto/v1/session', headers=None, method='GET', body=None):
                    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                    conn.request(method,path,body=body,headers=headers or {})
                    result=conn.getresponse(); status=result.status; data=json.loads(result.read()); conn.close()
                    return status,data
                try:
                    status,health=request('/api/health')
                    self.assertEqual(health['automatic_token_required'],required)
                    h={'X-Collab-Local':'1'}
                    self.assertEqual(request(headers=h)[0],401 if required else 200)
                    if required: h['Authorization']='Bearer '+credentials.token
                    status,value=request(headers=h); self.assertEqual(status,200)
                    self.assertNotIn('token',value)
                    for invalid in ({'Host':f'evil.example:{port}'},{'Origin':'null'},
                                    {'Origin':'https://evil.example'},{'Sec-Fetch-Site':'cross-site'}):
                        self.assertEqual(request(headers={**h,**invalid})[0],403)
                    post={**h,'Content-Type':'application/json','Idempotency-Key':'local-request-001'}
                    self.assertEqual(request('/api/auto/v1/providers/refresh',post,'POST','{}')[0],403)
                    post['X-Collab-CSRF']=value['csrf_token']
                    self.assertEqual(request('/api/auto/v1/providers/refresh',post,'POST','{}')[0],200)
                    if not required:
                        self.assertEqual(request()[0],403)
                        self.assertEqual(request(headers={**h,'Forwarded':'for=192.0.2.1'})[0],403)
                        self.assertEqual(request(headers={**h,'X-Forwarded-For':'127.0.0.1'})[0],403)
                finally:
                    server.shutdown(); server.server_close(); thread.join(3)

    def test_background_no_token_and_reuse_mismatch(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'project'; root.mkdir()
            subprocess.run(['git','init','-q',str(root)],check=True)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            env=dict(os.environ,COLLAB_UI_HOME=str(Path(tmp)/'home'))
            command=[sys.executable,str(collab.SCRIPT_PATH),'ui','--root',str(root),'--port',str(port)]
            # Test actual background entry; kill only its returned owned PID.
            result=subprocess.run(command+['--no-token'],env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            info=json.loads(result.stdout); pid=info['pid']
            try:
                self.assertTrue(info['started']); self.assertIsNone(info['token_log'])
                self.assertNotIn('Local access token', (Path(env['COLLAB_UI_HOME'])/'ui.log').read_text())
                reused=subprocess.run(command+['--no-token'],env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(reused.returncode,0,reused.stderr)
                self.assertFalse(json.loads(reused.stdout)['started'])
                mismatch=subprocess.run(command,env=env,capture_output=True,text=True,timeout=10)
                self.assertNotEqual(mismatch.returncode,0)
                self.assertIn('authentication mode differs',mismatch.stderr)
            finally:
                import signal
                os.kill(pid,signal.SIGTERM)
                for _ in range(30):
                    if not ui_server.read_health(port): break
                    time.sleep(.1)

if __name__=='__main__': unittest.main()
