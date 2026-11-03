"""Opt-in automatic collaboration HTTP boundary.

No endpoint registers filesystem roots or executes a command directly. Credentials
are explicitly supplied by a local owner; this module never exposes them through
an unauthenticated browser bootstrap. Existing read-only UI remains independent.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import time
from urllib.parse import parse_qs
from pathlib import Path
from typing import Callable

PREFIX = '/api/auto/v1'
MAX_BODY = 65536
ID = r'[a-z0-9][a-z0-9._-]{0,79}'


class ApiError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


class Credentials:
    """Memory-only owner credentials. Persistence/bootstrap belongs to local CLI."""
    def __init__(self, token=None, csrf=None, *, token_required=True):
        self.token_required = bool(token_required)
        self.token = token or secrets.token_urlsafe(32)
        self.csrf = csrf or secrets.token_urlsafe(32)
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise ValueError('local token must have at least 32 characters')
        if not isinstance(self.csrf, str) or len(self.csrf) < 32:
            raise ValueError('CSRF token must have at least 32 characters')


class Gateway:
    def __init__(self, credentials: Credentials, registry: Callable, dispatch: Callable):
        self.credentials, self.registry, self.dispatch = credentials, registry, dispatch

    @staticmethod
    def _header(headers, key, required=True):
        values = headers.get_all(key, [])
        if len(values) != 1 or not values[0]:
            if not values and not required:
                return None
            raise ApiError('invalid_headers', 'required headers must occur exactly once')
        return values[0]

    def check_origin(self, handler):
        headers = handler.headers
        host = self._header(headers, 'Host')
        port = handler.server.server_address[1]
        if host not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
            raise ApiError('invalid_host', 'loopback Host required', 403)
        origin = self._header(headers, 'Origin', required=False)
        if origin is not None and origin != 'http://' + host:
            raise ApiError('invalid_origin', 'same-origin request required', 403)
        fetch_site = self._header(headers, 'Sec-Fetch-Site', required=False)
        if fetch_site not in {None, 'same-origin', 'none'}:
            raise ApiError('cross_site', 'cross-site requests are not allowed', 403)

    def authorize(self, handler):
        self.check_origin(handler)
        headers = handler.headers
        # All callers, including CLI callers without Origin, must prove local
        # ownership. Browser mutations additionally require a separate CSRF key.
        auth = self._header(headers, 'Authorization', required=False)
        expected = 'Bearer ' + self.credentials.token
        if self.credentials.token_required and (auth is None or not hmac.compare_digest(auth.encode(), expected.encode())):
            raise ApiError('unauthorized', 'local authentication required', 401)
        if not self.credentials.token_required:
            if any(name.lower() == 'forwarded' or name.lower().startswith('x-forwarded-')
                   for name in headers.keys()):
                raise ApiError('proxy_forbidden', 'forwarded requests require token authentication', 403)
            # Disabling ownership authentication is local-process trust, not
            # permission for remote peers or ambient cross-site browser requests.
            try:
                local = all(ipaddress.ip_address(address).is_loopback for address in
                            (handler.server.server_address[0], handler.client_address[0]))
            except (ValueError, AttributeError):
                local = False
            if not local:
                raise ApiError('loopback_required', 'no-token mode requires loopback bind and peer', 403)
            if self._header(headers, 'X-Collab-Local', required=False) != '1':
                raise ApiError('local_header', 'explicit local request header required', 403)
        if handler.command != 'GET':
            csrf = self._header(headers, 'X-Collab-CSRF', required=False)
            if csrf is None or not hmac.compare_digest(csrf.encode(), self.credentials.csrf.encode()):
                raise ApiError('csrf', 'CSRF token required', 403)
    def project_root(self, project_id):
        # Registry is trusted local configuration, not supplied by HTTP input.
        # Match the existing UI's stable ID; reject duplicate/canonical aliases.
        matches = []
        for item in self.registry().get('projects', []):
            raw = item.get('root')
            if not isinstance(raw, str):
                continue
            if hashlib.sha256(raw.encode()).hexdigest()[:12] == project_id:
                root = Path(raw)
                if str(root) != raw or not root.is_absolute() or root.is_symlink() or root.resolve() != root:
                    raise ApiError('invalid_project', 'registered project is not canonical', 409)
                if not root.is_dir():
                    raise ApiError('project_unavailable', 'registered project is unavailable', 409)
                matches.append(root)
        if len(matches) != 1:
            raise ApiError('unknown_project', 'registered project required', 404)
        return matches[0]

    def route(self, method, path):
        if '?' in path or '%' in path or '\\' in path:
            raise ApiError('invalid_route', 'query and encoded paths are not supported')
        if path == PREFIX + '/session' and method == 'GET':
            return 'session', {}, None
        if path == PREFIX + '/providers' and method == 'GET':
            return 'providers', {}, None
        if path == PREFIX + '/providers/refresh' and method == 'POST':
            return 'refresh_providers', {}, None
        capability = re.fullmatch(PREFIX + r'/projects/(' + ID + r')/capabilities', path)
        if capability and method == 'GET':
            project = capability.group(1)
            return 'capabilities', {'project_id': project}, self.project_root(project)
        match = re.fullmatch(PREFIX + r'/projects/(' + ID + r')/tasks(?:/(' + ID + r'))?(?:/(.*))?', path)
        if not match:
            raise ApiError('not_found', 'not found', 404)
        project, task, action = match.groups()
        params = {'project_id': project}
        if task is None:
            if action is not None:
                raise ApiError('not_found', 'not found', 404)
            operation = {'GET': 'list_tasks', 'POST': 'create_task'}.get(method)
        else:
            params['task_id'] = task
            operation = None
            if method == 'GET':
                operation = {None: 'get_task', 'plan': 'get_plan', 'messages': 'messages', 'events': 'events'}.get(action)
            elif method == 'POST':
                if action in {'approve', 'revise', 'pause', 'resume', 'cancel', 'retry', 'messages', 'plan-edit'}:
                    operation = 'send_message' if action == 'messages' else 'plan_edit' if action == 'plan-edit' else action
                else:
                    participant = re.fullmatch(r'participants/(' + ID + r')/provider', action or '')
                    if participant:
                        operation = 'change_provider'
                        params['participant_id'] = participant.group(1)
        if operation is None:
            raise ApiError('not_found', 'not found', 404)
        return operation, params, self.project_root(project)

    @staticmethod
    def validate_body(operation, body):
        schemas = {
            'refresh_providers': (set(), set()),
            'create_task': ({'title', 'instruction', 'coordinator'}, set()),
            'approve': ({'expected_revision', 'plan_revision', 'plan_hash'}, set()),
            'plan_edit': ({'expected_revision', 'plan_revision', 'plan_hash', 'plan'}, set()),
            'revise': ({'expected_revision', 'instruction'}, set()),
            'pause': ({'expected_revision'}, set()),
            'resume': ({'expected_revision'}, set()),
            'cancel': ({'expected_revision'}, set()),
            'retry': ({'expected_revision', 'job_id'}, set()),
            'change_provider': ({'expected_revision', 'provider_id'}, {'model_id'}),
            'send_message': ({'body'}, {'recipients'}),
        }
        required, optional = schemas[operation]
        if set(body) - required - optional or required - set(body):
            raise ApiError('invalid_fields', 'unsupported or missing request fields', 422)
        if 'plan' in body and not isinstance(body['plan'], dict):
            raise ApiError('invalid_plan', 'plan object required', 422)
        for field in ('expected_revision', 'plan_revision'):
            if field in body and (type(body[field]) is not int or body[field] < 1):
                raise ApiError('invalid_revision', 'positive integer revision required', 422)
        if 'plan_hash' in body and (not isinstance(body['plan_hash'], str) or not re.fullmatch('[0-9a-f]{64}', body['plan_hash'])):
            raise ApiError('invalid_plan_hash', 'exact plan hash required', 422)
        for field in ('title', 'instruction', 'body', 'provider_id', 'job_id'):
            if field in body and (not isinstance(body[field], str) or not body[field].strip()):
                raise ApiError('invalid_field', 'nonempty text required', 422)
        if 'model_id' in body and body['model_id'] is not None and not isinstance(body['model_id'], str):
            raise ApiError('invalid_field', 'invalid model', 422)
        if 'recipients' in body and (not isinstance(body['recipients'], list) or not all(isinstance(x, str) for x in body['recipients'])):
            raise ApiError('invalid_recipients', 'recipient list required', 422)
        if 'coordinator' in body:
            value = body['coordinator']
            if not isinstance(value, dict) or set(value) - {'participant_id', 'provider_id', 'model_id'} or not {'participant_id', 'provider_id'} <= set(value):
                raise ApiError('invalid_coordinator', 'coordinator binding required', 422)
            for field in ('participant_id', 'provider_id'):
                if not isinstance(value[field], str) or not re.fullmatch(ID, value[field]):
                    raise ApiError('invalid_coordinator', 'invalid coordinator binding', 422)
            if value.get('model_id') is not None and not isinstance(value['model_id'], str):
                raise ApiError('invalid_coordinator', 'invalid coordinator model', 422)

    def handle(self, handler):
        if not handler.path.split('?', 1)[0].startswith(PREFIX):
            return False
        try:
            self.authorize(handler)
            route, separator, query = handler.path.partition('?')
            operation, params, root = self.route(handler.command, route)
            if separator:
                values = parse_qs(query, keep_blank_values=True)
                if handler.command != 'GET' or operation not in {'messages', 'events', 'list_tasks'} or set(values) != {'cursor'} or len(values['cursor']) != 1:
                    raise ApiError('invalid_query', 'one cursor is supported for paginated reads')
                cursor = values['cursor'][0]
                if not re.fullmatch(r'[a-z0-9._-]{1,128}', cursor):
                    raise ApiError('invalid_cursor', 'invalid cursor')
                params['cursor'] = cursor
            body, key = {}, None
            if handler.command == 'POST':
                if self._header(handler.headers, 'Transfer-Encoding', False) is not None:
                    raise ApiError('invalid_body', 'transfer encoding is not supported')
                if self._header(handler.headers, 'Content-Type') != 'application/json':
                    raise ApiError('invalid_content_type', 'application/json required', 415)
                length = self._header(handler.headers, 'Content-Length')
                if not re.fullmatch(r'0|[1-9][0-9]{0,7}', length):
                    raise ApiError('invalid_body', 'valid Content-Length required')
                if int(length) > MAX_BODY:
                    raise ApiError('body_too_large', 'request body exceeds limit', 413)
                key = self._header(handler.headers, 'Idempotency-Key')
                if not re.fullmatch(r'[A-Za-z0-9._-]{8,128}', key):
                    raise ApiError('invalid_idempotency_key', 'invalid Idempotency-Key')
                connection = getattr(handler, 'connection', None)
                old_timeout = connection.gettimeout() if connection is not None else None
                try:
                    deadline = time.monotonic() + 5.0
                    remaining, chunks = int(length), []
                    while remaining:
                        budget = deadline - time.monotonic()
                        if budget <= 0:
                            raise ApiError('request_timeout', 'request body deadline exceeded', 408)
                        if connection is not None:
                            connection.settimeout(budget)
                        chunk = handler.rfile.read1(min(remaining, 8192))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b''.join(chunks)
                finally:
                    if connection is not None:
                        connection.settimeout(old_timeout)
                if len(raw) != int(length):
                    raise ApiError('invalid_body', 'incomplete request body')
                def unique(pairs):
                    result = {}
                    for name, value in pairs:
                        if name in result:
                            raise ApiError('invalid_body', 'duplicate JSON field')
                        result[name] = value
                    return result
                body = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
                if not isinstance(body, dict):
                    raise ApiError('invalid_body', 'JSON object required')
                self.validate_body(operation, body)
            result = {'csrf_token': self.credentials.csrf, 'enabled': True} if operation == 'session' else self.dispatch(operation, params, root, body, key)
            status = 201 if operation == 'create_task' else 202 if operation == 'send_message' else 200
            handler.respond(status, {'schema_version': 1, **result})
        except ApiError as exc:
            handler.close_connection = True
            handler.respond(exc.status, {'error': {'code': exc.code, 'message': str(exc)}, 'request_id': secrets.token_hex(8)})
        except (UnicodeError, ValueError, TypeError, KeyError, RecursionError):
            handler.close_connection = True
            handler.respond(400, {'error': {'code': 'invalid_request', 'message': 'request validation failed'}, 'request_id': secrets.token_hex(8)})
        except OSError:
            handler.close_connection = True
            handler.respond(503, {'error': {'code': 'unavailable', 'message': 'local service unavailable'}, 'request_id': secrets.token_hex(8)})
        return True
