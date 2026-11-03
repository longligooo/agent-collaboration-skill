"""Bounded, deduplicated HTTP reads; disconnected clients do not spawn more scans."""
import json
import secrets
import threading
import time

import read_budget


class SnapshotJobs:
    def __init__(self, workers=2, timeout=45.0, ttl=120.0, max_results=16,
                 max_bytes=64 * 1024 * 1024):
        self.workers, self.timeout, self.ttl = workers, timeout, ttl
        self.max_results, self.max_bytes = max_results, max_bytes
        self.lock = threading.Lock()
        self.jobs, self.active = {}, {}
        self.running = 0

    def _prune(self, now):
        # Running jobs retain capacity even after expiry or an OS read stalls.
        for token, job in list(self.jobs.items()):
            if job['done'] and now - job['finished'] > self.ttl:
                self.jobs.pop(token)
                if self.active.get(job['key']) == token:
                    self.active.pop(job['key'])
        done = sorted((j for j in self.jobs.values() if j['done']), key=lambda j: j['finished'])
        while done and (len(self.jobs) >= self.max_results or sum(j['bytes'] for j in self.jobs.values()) > self.max_bytes):
            job = done.pop(0)
            self.jobs.pop(job['id'])
            if self.active.get(job['key']) == job['id']:
                self.active.pop(job['key'])

    @staticmethod
    def error(code, message, status=503):
        return status, {'result': 'ERROR', 'error_code': code, 'error': message}

    def submit(self, key, operation):
        with self.lock:
            self._prune(time.monotonic())
            existing = self.active.get(key)
            if existing in self.jobs and not self.jobs[existing]['done']:
                return self._reply(self.jobs[existing])
            # A new refresh joins only running work, never an orphaned finished
            # initial response. Clients with a token can still replay that result.
            self.active.pop(key, None)
            if self.running >= self.workers:
                return self.error('snapshot_busy', 'snapshot readers busy; a previous read is still finishing')
            token = secrets.token_hex(16)
            job = {'id': token, 'key': key, 'deadline': time.monotonic() + self.timeout,
                   'done': False, 'bytes': 0, 'ready': threading.Event()}
            self.jobs[token] = job
            self.active[key] = token
            self.running += 1
            thread = threading.Thread(target=self._run, args=(job, operation), daemon=True)
            try:
                thread.start()
            except RuntimeError:
                self.running -= 1
                self.jobs.pop(token)
                self.active.pop(key, None)
                return self.error('snapshot_busy', 'snapshot worker could not start; retry shortly')
            # Always hand out a job token, even if a tiny scan already finished.
            return 202, {'pending': True, 'job_id': token, 'retry_after_ms': 100}

    def _run(self, job, operation):
        try:
            with read_budget.budget(job['deadline']):
                payload = operation()
                result = (200, payload)
                size = len(json.dumps(payload, ensure_ascii=False).encode())
                if size > self.max_bytes:
                    result = self.error('snapshot_too_large', 'snapshot result exceeds cache capacity', 413)
                    size = 0
        except read_budget.ReadTimeout:
            result = self.error('snapshot_timeout', 'snapshot read timed out; check large or unavailable project directories and retry')
            size = 0
        except Exception as exc:
            # Known read failures keep their public HTTP status. No traceback or
            # arbitrary exception internals are included for unexpected failures.
            status = getattr(exc, 'status', 400 if isinstance(exc, (ValueError, OSError, TypeError, KeyError)) else 500)
            result = self.error('snapshot_read_failed', str(exc) if status != 500 else 'snapshot read failed', status)
            size = 0
        with self.lock:
            job.update(done=True, finished=time.monotonic(), result=result, bytes=size)
            self.running -= 1
            job['ready'].set()
            self._prune(time.monotonic())

    def _reply(self, job, consume=True):
        if job['done']:
            # Completed initial reads are never reused by a later explicit refresh.
            # Poll tokens still replay safely for concurrent consumers until TTL.
            if consume and self.active.get(job['key']) == job['id']:
                self.active.pop(job['key'])
            return job['result']
        if time.monotonic() >= job['deadline']:
            return self.error('snapshot_timeout', 'snapshot read timed out; an operating-system read may still be finishing')
        return 202, {'pending': True, 'job_id': job['id'], 'retry_after_ms': 100}

    def poll(self, token):
        with self.lock:
            self._prune(time.monotonic())
            job = self.jobs.get(token)
            if job is None:
                return self.error('snapshot_expired', 'snapshot job expired; start a new refresh', 410)
            return self._reply(job)

    def fast_reply(self, status, payload):
        """Avoid a poll round-trip for small batches without blocking on slow IO."""
        if status != 202:
            return status, payload
        token = payload['job_id']
        with self.lock:
            job = self.jobs.get(token)
        if job is not None and job['ready'].wait(.05):
            return self.poll(token)
        return status, payload
