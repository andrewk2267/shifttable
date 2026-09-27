"""One shared database for every copy of the hosted demo.

Vercel runs several copies of the app at once, each with its own /tmp, so on its own
each copy keeps separate demo data. With BLOB_READ_WRITE_TOKEN set, every copy mirrors
its SQLite file in one private Vercel Blob store:

- Before a request, a copy fetches the shared file if it may be behind. The fetch is
  conditional, so an unchanged file costs no download.
- After a request that changed anything, it uploads the file on condition that nobody
  saved since it fetched. If another copy got there first, the upload is refused; the
  caller fetches the newer file and runs the request again on it.
- Snapshots travel as one zip, uploaded only when they change.
- A demo nobody has changed for an hour is seeded afresh for the next visitor.

Standard library only: the Blob calls are plain HTTPS requests.
"""
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

DB_BLOB = 'shifttable/roster.db.z'
SNAP_RE = re.compile(r'^[A-Za-z0-9._-]+\.db$')
CHECK_EVERY = 2.0   # seconds; sooner when a visitor has already seen a newer version
IDLE_RESET = 3600   # seconds without changes before the next visitor gets a fresh demo
COOKIE = 'shifttable_v'


class Conflict(Exception):
    """Another copy saved the shared file first."""


class BlobError(Exception):
    """The Blob store couldn't be reached or refused a request."""


def log(message):
    print(f'demo sync: {message}', file=sys.stderr, flush=True)


class BlobStore:
    """The Vercel Blob calls the demo needs, as used by Vercel's own @vercel/blob client."""

    def __init__(self, token, api_url=None, files_url=None):
        self.token = token
        parts = token.split('_')
        self.store_id = parts[3] if len(parts) > 3 else ''
        self.api = (api_url or os.environ.get('VERCEL_BLOB_API_URL') or 'https://vercel.com/api/blob').rstrip('/')
        self.files = (files_url or f'https://{self.store_id}.private.blob.vercel-storage.com').rstrip('/')

    def _call(self, req):
        req.add_header('Authorization', f'Bearer {self.token}')
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
        except OSError as e:
            raise BlobError(f'{req.get_method()} {urllib.parse.urlparse(req.full_url).path}: {e}') from None

    def _api(self, method, path, data, headers):
        headers = {'x-api-version': '12', 'x-vercel-blob-store-id': self.store_id, **headers}
        return self._call(urllib.request.Request(self.api + path, data=data, method=method, headers=headers))

    def get(self, pathname, etag=None):
        """(etag, data), where data is None if unchanged since etag, or (None, None) if missing."""
        req = urllib.request.Request(f'{self.files}/{urllib.parse.quote(pathname)}?cache=0')
        if etag:
            req.add_header('If-None-Match', etag)
        status, headers, body = self._call(req)
        if status == 304:
            return etag, None
        if status == 404:
            return None, None
        if status != 200:
            raise BlobError(f'GET {pathname}: HTTP {status}')
        return headers.get('ETag'), body

    def put(self, pathname, data, if_match=None):
        headers = {'x-vercel-blob-access': 'private', 'x-add-random-suffix': '0', 'x-allow-overwrite': '1',
                   'x-content-type': 'application/octet-stream'}
        if if_match:
            headers['x-if-match'] = if_match
        status, _h, body = self._api('PUT', '/?' + urllib.parse.urlencode({'pathname': pathname}), data, headers)
        code = '' if status == 200 else _error_code(body)
        if status == 412 or code == 'precondition_failed':
            raise Conflict()
        if status != 200:
            raise BlobError(f'PUT {pathname}: HTTP {status} {code}')
        return json.loads(body).get('etag') or self.get(pathname)[0]

    def delete(self, pathnames):
        urls = [f'{self.files}/{urllib.parse.quote(p)}' for p in pathnames]
        status, _h, body = self._api('POST', '/delete', json.dumps({'urls': urls}).encode(), {'content-type': 'application/json'})
        if status != 200:
            raise BlobError(f'delete: HTTP {status} {_error_code(body)}')


def _error_code(body):
    try:
        return json.loads(body)['error']['code']
    except (ValueError, KeyError, TypeError):
        return ''


class DemoSync:
    """Keeps this copy's database file and snapshots in step with the shared ones.

    Callers hold `lock` around before(), the request and after(), so one copy handles one
    request at a time while it may be swapping files.
    """

    def __init__(self, store, db_path, backups_dir, seed):
        self.store, self.db_path, self.backups, self.seed = store, db_path, backups_dir, seed
        self.lock = threading.RLock()
        self.known = False   # reached the store at least once, so etag is trustworthy
        self.etag = None     # the shared file's ETag that the local file matches (None: nothing shared yet)
        self.digest = None   # sha256 of the local file when it last matched the shared one
        self.bundle = ''     # the shared snapshot zip that the local backups folder matches
        self.snaps = []      # the local snapshots at that point, as (name, size)
        self.checked = 0.0

    @property
    def version(self):
        """A short tag for the shared file, kept in a cookie so a visitor never sees older data."""
        return hashlib.sha256(self.etag.encode()).hexdigest()[:12] if self.etag else ''

    def start(self):
        """At cold start: fetch the shared demo, migrate or seed it, and share any change."""
        with self.lock:
            try:
                self.pull()
            except BlobError as e:
                log(e)
            self.seed()
            self.save_quietly()

    def before(self, method, seen):
        """Bring the local file up to date as far as this request needs. Returns a status word."""
        status = 'cached'
        try:
            if (method != 'GET' or not self.known or (seen and seen != self.version)
                    or time.monotonic() - self.checked > CHECK_EVERY):
                status = self.pull()
            if self.known and self._idle():
                status = self.reset()
        except BlobError as e:
            log(e)
            status = 'offline'
        return status

    def after(self):
        """Share the local file if the request changed it. Raises Conflict if another copy saved first."""
        if not self.known:
            return None
        snaps = self._local_snaps()
        if self._digest() == self.digest and snaps == self.snaps:
            return None
        bundle = self.bundle if snaps == self.snaps else self._push_snapshots(snaps)
        conn = sqlite3.connect(self.db_path)
        try:
            for key, value in (('_demo_backups', bundle), ('_demo_changed_at', repr(time.time()))):
                conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                             (key, value))
            conn.commit()
        finally:
            conn.close()
        with open(self.db_path, 'rb') as f:
            raw = f.read()
        try:
            etag = self.store.put(DB_BLOB, zlib.compress(raw, 6), self.etag)
        except Conflict:
            if bundle != self.bundle:
                self._forget(bundle)
            raise
        old = self.bundle
        self.etag, self.digest, self.bundle, self.snaps = etag, _fingerprint(raw), bundle, snaps
        self.checked = time.monotonic()
        if old and old != bundle:
            self._forget(old)
        return 'saved'

    def save_quietly(self):
        try:
            return self.after()
        except Conflict:
            return self.pull()
        except BlobError as e:
            log(e)
            return 'offline'

    def pull(self):
        """Fetch the shared file if it differs from the local one."""
        etag, data = self.store.get(DB_BLOB, self.etag)
        self.known, self.checked = True, time.monotonic()
        if etag is None:
            self.etag, self.digest = None, None  # nothing shared yet: the next after() creates it
            return 'empty'
        if data is None:
            return 'current'
        raw = zlib.decompress(data)
        tmp = self.db_path + '.pull'
        with open(tmp, 'wb') as f:
            f.write(raw)
        try:
            bundle = _read_setting(tmp, '_demo_backups') or ''
            files = self._fetch_snapshots(bundle) if bundle != self.bundle else None
            os.replace(tmp, self.db_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        if files is not None:
            shutil.rmtree(self.backups, ignore_errors=True)
            os.makedirs(self.backups, exist_ok=True)
            for name, content in files.items():
                with open(os.path.join(self.backups, name), 'wb') as f:
                    f.write(content)
            self.bundle, self.snaps = bundle, self._local_snaps()
        self.etag, self.digest = etag, _fingerprint(raw)
        return 'pulled'

    def reset(self):
        """Seed a fresh demo for the next visitor."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        shutil.rmtree(self.backups, ignore_errors=True)
        self.seed()
        try:
            self.after()
            return 'reset'
        except Conflict:
            return self.pull()  # someone changed or reset it meanwhile; use theirs

    def cookie(self, seen, secure):
        if self.version and seen != self.version:
            return f'{COOKIE}={self.version}; HttpOnly; SameSite=Strict; Path=/' + ('; Secure' if secure else '')
        return None

    def _idle(self):
        changed = _read_setting(self.db_path, '_demo_changed_at')
        return changed is not None and time.time() - float(changed) > IDLE_RESET

    def _digest(self):
        try:
            with open(self.db_path, 'rb') as f:
                return _fingerprint(f.read())
        except FileNotFoundError:
            return None

    def _local_snaps(self):
        try:
            return sorted((n, os.path.getsize(os.path.join(self.backups, n))) for n in os.listdir(self.backups) if SNAP_RE.match(n))
        except FileNotFoundError:
            return []

    def _push_snapshots(self, snaps):
        if not snaps:
            return ''
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, _size in snaps:
                z.write(os.path.join(self.backups, name), name)
        data = buf.getvalue()
        pathname = f'shifttable/backups-{hashlib.sha256(data).hexdigest()[:16]}.zip'
        self.store.put(pathname, data)
        return pathname

    def _fetch_snapshots(self, bundle):
        if not bundle:
            return {}
        _etag, data = self.store.get(bundle)
        if data is None:
            log(f'snapshot bundle {bundle} is missing')
            return {}
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return {n: z.read(n) for n in z.namelist() if SNAP_RE.match(n)}

    def _forget(self, pathname):
        try:
            self.store.delete([pathname])
        except BlobError as e:
            log(e)


def _fingerprint(raw):
    """sha256 of an SQLite file, leaving out the header's change counters (bytes 24-27 and
    92-95), which tick on every write transaction even when it changed nothing."""
    return hashlib.sha256(raw[:24] + raw[28:92] + raw[96:]).digest()


def _read_setting(path, key):
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path)
    try:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()
