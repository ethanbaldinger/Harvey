"""MySQL mailbox for PA; independent prefixed tables, serialized transactions."""
from contextlib import contextmanager
import json
import re

from .common import DEFAULT_SETTINGS
from .mailbox import Mailbox

SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS barbie_jobs (
        id VARCHAR(32) PRIMARY KEY, word VARCHAR(250) COLLATE utf8mb4_bin NOT NULL,
        provider VARCHAR(32) NOT NULL, lane VARCHAR(16) NOT NULL,
        created VARCHAR(40) NOT NULL, result LONGTEXT, received VARCHAR(40), collected VARCHAR(40),
        UNIQUE KEY provider_word(provider,word)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
    '''CREATE TABLE IF NOT EXISTS barbie_control (
        id INT PRIMARY KEY, value LONGTEXT NOT NULL) ENGINE=InnoDB''',
    '''CREATE TABLE IF NOT EXISTS barbie_heartbeat (
        id INT PRIMARY KEY, at VARCHAR(40) NOT NULL, value LONGTEXT NOT NULL) ENGINE=InnoDB''',
]


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class Connection:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, parameters=()):
        for table in ('jobs', 'control', 'heartbeat'):
            sql = re.sub(r'\b' + table + r'\b', 'barbie_' + table, sql)
        sql = sql.replace('?', '%s').replace('INSERT OR IGNORE', 'INSERT IGNORE')
        sql = sql.replace('INSERT OR REPLACE', 'REPLACE')
        cur = self.conn.cursor(dictionary=True, buffered=True)
        try:
            cur.execute(sql, parameters)
            return Result([Row(row) for row in cur.fetchall()] if cur.with_rows else [])
        finally:
            cur.close()


class MySQLMailbox(Mailbox):
    def __init__(self, connect, worker_token, manager_token):
        if min(len(worker_token), len(manager_token)) < 24 or worker_token == manager_token:
            raise ValueError('Distinct mailbox tokens of at least 24 characters required')
        self.mysql_connect = connect
        conn = connect()
        try:
            cur = conn.cursor()
            for sql in SCHEMA:
                cur.execute(sql)
            cur.execute('INSERT IGNORE INTO barbie_control VALUES(1,%s)', (json.dumps(DEFAULT_SETTINGS),))
            conn.commit()
            cur.close()
        finally:
            conn.close()
        super().__init__(None, worker_token, manager_token, connection_factory=self.transaction)

    @contextmanager
    def transaction(self):
        conn = self.mysql_connect()
        try:
            cur = conn.cursor(buffered=True)
            cur.execute('SELECT id FROM barbie_control WHERE id=1 FOR UPDATE')
            cur.fetchall()
            cur.close()
            yield Connection(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
