"""Bounded disk storage for verified recoveries; never stores input scripts."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3


def engine_namespace(engine, bot_source):
    """Invalidate results when recovery sources, binaries or bot behavior change."""
    engine = Path(engine)
    digest = hashlib.sha256(b'luraph-results-v1\0' + Path(bot_source).read_bytes())
    candidates = []
    for folder in (engine / 'luraph', engine / 'vendor' / 'caomod2077'):
        if folder.is_dir():
            candidates.extend(p for p in folder.rglob('*') if p.is_file() and (
                p.suffix in ('.py', '.js', '.luau') or p.name in ('luau', 'luau-ast', 'luau.exe', 'luau-ast.exe')))
    build = engine / 'scripts' / 'build_runtime.py'
    if build.is_file():
        candidates.append(build)
    for file in sorted(candidates):
        digest.update(str(file.relative_to(engine)).encode())
        digest.update(b'\0')
        with file.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        digest.update(b'\0')
    return digest.hexdigest()


class ResultStore:
    """Fail open to recovery on disk errors; each operation owns its connection."""
    def __init__(self, directory, namespace, max_bytes=128 * 1024 * 1024, max_entries=256):
        self.path = Path(directory) / 'luraph-results.sqlite3'
        self.namespace = namespace
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.enabled = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS results ('
                           'namespace TEXT, key BLOB, value BLOB, sha256 TEXT, size INTEGER, '
                           'used INTEGER, PRIMARY KEY(namespace, key))')
                db.execute('CREATE INDEX IF NOT EXISTS result_lru ON results(used)')
            self.enabled = True
        except (OSError, sqlite3.Error):
            pass

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=0.05)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, key):
        if not self.enabled:
            return None
        try:
            with self._connect() as db:
                row = db.execute('SELECT value, sha256, size FROM results WHERE namespace=? AND key=? '
                                 'AND length(value)<=? AND size<=?',
                                 (self.namespace, key, min(self.max_bytes, 32 * 1024 * 1024),
                                  min(self.max_bytes, 32 * 1024 * 1024))).fetchone()
                if row is None:
                    return None
                value, expected, size = row
                if not value or len(value) != size or hashlib.sha256(value).hexdigest() != expected:
                    db.execute('DELETE FROM results WHERE namespace=? AND key=?', (self.namespace, key))
                    return None
                db.execute('UPDATE results SET used=(SELECT coalesce(max(used),0)+1 FROM results) '
                           'WHERE namespace=? AND key=?', (self.namespace, key))
                return value
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return None

    def put(self, key, value):
        if not self.enabled or not value or len(value) > min(self.max_bytes, 32 * 1024 * 1024):
            return
        try:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR REPLACE INTO results VALUES '
                           '(?,?,?,?,?,(SELECT coalesce(max(used),0)+1 FROM results))',
                           (self.namespace, key, value, hashlib.sha256(value).hexdigest(), len(value)))
                size, count = db.execute('SELECT coalesce(sum(size),0), count(*) FROM results').fetchone()
                while size > self.max_bytes or count > self.max_entries:
                    namespace, old_key, removed = db.execute(
                        'SELECT namespace,key,size FROM results ORDER BY used LIMIT 1').fetchone()
                    db.execute('DELETE FROM results WHERE namespace=? AND key=?', (namespace, old_key))
                    size -= removed
                    count -= 1
        except (OSError, sqlite3.Error):
            pass
