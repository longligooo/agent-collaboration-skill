import hashlib
import io
import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_api


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        self.calls = []
        def dispatch(*args):
            self.calls.append(args)
            return {'ok': True}
        self.gateway = auto_api.Gateway(auto_api.Credentials('t'*32, 'c'*32),
            lambda: {'projects': [{'root': str(self.root)}]}, dispatch)
        self.path = auto_api.PREFIX + '/projects/' + self.project + '/tasks'

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method='POST', path=None, body=None, headers=None):
        if body is None:
            body = json.dumps({'title':'Task','instruction':'Do work','coordinator':{'participant_id':'lead','provider_id':'claude'}})
        h = Message()
        values = {'Host': '127.0.0.1:23451', 'Origin': 'http://127.0.0.1:23451',
                  'Authorization': 'Bearer '+'t'*32, 'X-Collab-CSRF': 'c'*32,
                  'Content-Type': 'application/json', 'Content-Length': str(len(body.encode())),
                  'Idempotency-Key': 'request-0001'}
        values.update(headers or {})
        for k, v in values.items():
            if v is not None:
                h[k] = v
        results = []
        handler = SimpleNamespace(headers=h, server=SimpleNamespace(server_address=('127.0.0.1',23451)),
            client_address=("127.0.0.1",12345), command=method, path=path or self.path, rfile=io.BytesIO(body.encode()),
            respond=lambda *args: results.append(args), close_connection=False)
        self.assertTrue(self.gateway.handle(handler))
        return results[0]


    def test_no_token_remains_local_explicit_and_csrf_protected(self):
        self.gateway.credentials.token_required = False
        headers = {'Authorization': None, 'X-Collab-Local': '1'}
        self.assertEqual(self.request('GET', auto_api.PREFIX+'/session', headers=headers)[0], 200)
        self.assertEqual(self.request(headers=headers)[0], 201)
        for bad in ({'Host':'evil.example:23451'}, {'Origin':'null'},
                    {'Origin':'https://evil.example'}, {'Sec-Fetch-Site':'cross-site'},
                    {'X-Collab-Local':None}, {'X-Collab-CSRF':None}):
            self.assertEqual(self.request(headers={**headers, **bad})[0],403)

    def test_no_token_rejects_non_loopback_bind_or_peer(self):
        self.gateway.credentials.token_required = False
        for bind, peer in [('0.0.0.0','127.0.0.1'), ('127.0.0.1','192.168.1.10')]:
            headers=Message(); headers['Host']='127.0.0.1:23451'; headers['X-Collab-Local']='1'
            handler=SimpleNamespace(headers=headers, command='GET', client_address=(peer,1234),
                                    server=SimpleNamespace(server_address=(bind,23451)))
            with self.assertRaises(auto_api.ApiError) as error: self.gateway.authorize(handler)
            self.assertEqual(error.exception.code,'loopback_required')

    def test_valid_registry_dispatch(self):
        self.assertEqual(self.request()[0], 201)
        self.assertEqual(self.calls[0][2], self.root)

    def test_auth_all_reads_and_writes(self):
        for method in ('GET','POST'):
            self.assertEqual(self.request(method, headers={'Authorization': None})[0],401)
        self.assertFalse(self.calls)

    def test_no_origin_cli_still_needs_both_keys(self):
        self.assertEqual(self.request(headers={'Origin':None})[0],201)
        self.assertEqual(self.request(headers={'Origin':None,'X-Collab-CSRF':None})[0],403)

    def test_cross_origin_and_dns_rebind(self):
        for headers in ({'Host':'evil.example:23451'}, {'Origin':'https://evil.example'},
                        {'Sec-Fetch-Site':'cross-site'}, {'X-Collab-CSRF':'wrong'}):
            self.assertEqual(self.request(headers=headers)[0],403)
        self.assertFalse(self.calls)

    def test_strict_body_and_limits(self):
        for body in ('[]','{"a":1,"a":2}','{"a":NaN}','{'):
            self.assertEqual(self.request(body=body)[0],400)
        self.assertEqual(self.request(headers={'Content-Length':'999999'})[0],413)
        self.assertEqual(self.request(headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request(headers={'Transfer-Encoding':'chunked'})[0],400)
        self.assertEqual(self.request(headers={'Content-Length':'-1'})[0],400)
        self.assertFalse(self.calls)

    def test_paths_are_registry_only(self):
        for path in (self.path.replace(self.project,'unknown'), self.path+'/../../../tmp',
                     self.path+'?root=/tmp', self.path+'/%2e%2e'):
            self.assertIn(self.request(path=path)[0],(400,404))
        self.assertFalse(self.calls)

    def test_invalid_or_duplicate_headers(self):
        self.assertEqual(self.request(headers={'Idempotency-Key':'bad'})[0],400)
        h=Message();h['Host']='127.0.0.1:23451';h['Host']='evil'
        with self.assertRaises(auto_api.ApiError): self.gateway._header(h,'Host')

    def test_routes(self):
        for suffix, operation in (('/t1','get_task'),('/t1/plan','get_plan'),('/t1/messages','messages')):
            self.request('GET',self.path+suffix)
            self.assertEqual(self.calls[-1][0],operation)
        self.request(path=self.path+'/t1/participants/builder/provider', body=json.dumps({'expected_revision':1,'provider_id':'claude'}))
        self.assertEqual(self.calls[-1][0],'change_provider')

    def test_reviewer_malformed_payloads(self):
        self.assertEqual(self.request(body='{"a":'+'['*12000+'0'+']'*12000+'}')[0],400)
        self.assertEqual(self.request(body='{"root":"/tmp","command":"sh","flags":[]}')[0],422)
        self.assertEqual(self.request(path=self.path+'/t1/approve',body='{"approved":true}')[0],422)
        self.assertFalse(self.calls)

    def test_session_requires_auth(self):
        status, result = self.request('GET',auto_api.PREFIX+'/session')
        self.assertEqual(status,200)
        self.assertEqual(result['csrf_token'],'c'*32)
        self.assertNotIn('token',result)
        self.assertEqual(self.request('GET',auto_api.PREFIX+'/session',headers={'Authorization':None})[0],401)

    def test_cursor_read_only(self):
        self.assertEqual(self.request('GET',self.path+'?cursor=auto-123')[0],200)
        self.assertEqual(self.calls[-1][1]['cursor'],'auto-123')
        self.assertEqual(self.request('GET',self.path+'?cursor=x&cursor=y')[0],400)

if __name__ == '__main__': unittest.main()
