"""PostgreSQL control plane: transactions, durable state and owner-scoped records."""
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, RealDictCursor

TERMINAL = {"completed", "completed_with_warnings", "needs_review", "failed", "cancelled", "interrupted"}
ROOT = Path(os.getenv("ARTIFACT_ROOT", "/app/uploads/service"))


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    return str(uuid.UUID(str(value)))


def directory(value):
    path = ROOT / identifier(value)
    path.mkdir(parents=True, exist_ok=True)
    return path


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ":" + digest


def password_matches(password, encoded):
    try:
        return hmac.compare_digest(password_hash(password, encoded.split(":")[0]), encoded)
    except (ValueError, TypeError):
        return False


class Store:
    @contextmanager
    def connection(self):
        conn = psycopg2.connect(host=os.getenv("META_HOST", "postgres"),
            port=os.getenv("META_PORT", "5432"), dbname=os.getenv("META_DB", "moderniser_meta"),
            user=os.getenv("META_USER", "moderniser"), password=os.environ["META_PASSWORD"],
            connect_timeout=5, cursor_factory=RealDictCursor)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users(
                    username text PRIMARY KEY, password_hash text NOT NULL,
                    role text NOT NULL CHECK(role IN ('admin','user')), created timestamptz DEFAULT now());
                CREATE TABLE IF NOT EXISTS sessions(
                    token_hash text PRIMARY KEY, username text REFERENCES users(username),
                    expires timestamptz NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                    kind text NOT NULL, id uuid NOT NULL, owner text NOT NULL REFERENCES users(username),
                    data jsonb NOT NULL, created timestamptz DEFAULT now(), updated timestamptz DEFAULT now(),
                    PRIMARY KEY(kind,id));
                CREATE INDEX IF NOT EXISTS records_owner ON records(owner,kind,created DESC);
                CREATE TABLE IF NOT EXISTS events(
                    seq bigserial PRIMARY KEY, job_id uuid NOT NULL, owner text NOT NULL,
                    data jsonb NOT NULL, created timestamptz DEFAULT now());
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id,seq);
                CREATE TABLE IF NOT EXISTS idempotency(
                    owner text NOT NULL, key text NOT NULL, request_hash text NOT NULL,
                    job_id uuid NOT NULL, PRIMARY KEY(owner,key));
                CREATE TABLE IF NOT EXISTS audit(
                    seq bigserial PRIMARY KEY, owner text, action text NOT NULL,
                    detail jsonb NOT NULL DEFAULT '{}', created timestamptz DEFAULT now());
                CREATE TABLE IF NOT EXISTS service_health(
                    name text PRIMARY KEY, data jsonb NOT NULL, updated timestamptz DEFAULT now());
                CREATE TABLE IF NOT EXISTS login_attempts(
                    address text NOT NULL, created timestamptz DEFAULT now());
            """)

    def create_user(self, username, password, role="user", if_missing=False):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO users(username,password_hash,role) VALUES (%s,%s,%s) " +
                        ("ON CONFLICT DO NOTHING" if if_missing else ""),
                        (username, password_hash(password), role))

    def authenticate(self, username, password, address):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM login_attempts WHERE created<now()-interval '10 minutes'")
            cur.execute("SELECT count(*) n FROM login_attempts WHERE address=%s AND created>now()-interval '1 minute'", (address,))
            if cur.fetchone()["n"] >= 8:
                raise PermissionError("Too many login attempts. Try again in one minute.")
            cur.execute("INSERT INTO login_attempts(address) VALUES (%s)", (address,))
            cur.execute("SELECT * FROM users WHERE username=%s", (username,))
            user = cur.fetchone()
            # Always perform a password hash to reduce username timing differences.
            encoded = user["password_hash"] if user else password_hash("unavailable-account")
            if not password_matches(password, encoded) or not user:
                return None
            token = secrets.token_urlsafe(40)
            cur.execute("INSERT INTO sessions VALUES (%s,%s,now()+interval '12 hours')",
                        (hashlib.sha256(token.encode()).hexdigest(), username))
            return token

    def user_for_token(self, token):
        if not token:
            return None
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT u.username,u.role FROM sessions s JOIN users u ON u.username=s.username
                           WHERE token_hash=%s AND expires>now()""",
                        (hashlib.sha256(token.encode()).hexdigest(),))
            return cur.fetchone()

    def logout(self, token):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token_hash=%s",
                        (hashlib.sha256(token.encode()).hexdigest(),))

    def put(self, kind, owner, data, record_id=None):
        record_id = identifier(record_id or uuid.uuid4())
        data = dict(data, id=record_id, owner=owner)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO records(kind,id,owner,data) VALUES (%s,%s,%s,%s)",
                        (kind, record_id, owner, Json(data, dumps=lambda x: json.dumps(x, default=str))))
        return data

    def get(self, kind, record_id, owner=None):
        record_id = identifier(record_id)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM records WHERE kind=%s AND id=%s" +
                        (" AND owner=%s" if owner else ""),
                        (kind, record_id, owner) if owner else (kind, record_id))
            row = cur.fetchone()
            return row["data"] if row else None

    def update(self, kind, record_id, change):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM records WHERE kind=%s AND id=%s FOR UPDATE",
                        (kind, identifier(record_id)))
            row = cur.fetchone()
            if not row:
                raise KeyError(record_id)
            data = row["data"]
            change(data)
            cur.execute("UPDATE records SET data=%s,updated=now() WHERE kind=%s AND id=%s",
                        (Json(data, dumps=lambda x: json.dumps(x, default=str)), kind, record_id))
            return data

    def list(self, kind, owner=None, limit=30, offset=0):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM records WHERE kind=%s" +
                        (" AND owner=%s" if owner else "") + " ORDER BY created DESC LIMIT %s OFFSET %s",
                        (kind, owner, limit, offset) if owner else (kind, limit, offset))
            return [r["data"] for r in cur]

    def event(self, job, data):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO events(job_id,owner,data) VALUES (%s,%s,%s)",
                        (job["id"], job["owner"], Json(data, dumps=lambda x: json.dumps(x, default=str))))

    def events(self, job_id, owner, after=0):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT seq,data,created FROM events WHERE job_id=%s AND owner=%s AND seq>%s ORDER BY seq LIMIT 300",
                        (identifier(job_id), owner, after))
            return list(cur)

    def claim(self, job_id, lease):
        claimed = [False]
        def change(job):
            if job["status"] == "queued" and not job.get("cancel_requested"):
                job.update(status="running", lease=lease, heartbeat=time.time(), started_at=now(),
                           queued_seconds=round(time.time()-job['started_ts'],3))
                claimed[0] = True
        self.update("job", job_id, change)
        return claimed[0]

    def health(self, name, data=None):
        with self.connection() as conn, conn.cursor() as cur:
            if data is not None:
                cur.execute("""INSERT INTO service_health(name,data) VALUES (%s,%s)
                               ON CONFLICT(name) DO UPDATE SET data=excluded.data,updated=now()""",
                            (name, Json(data)))
            cur.execute("SELECT data,extract(epoch FROM now()-updated) age FROM service_health WHERE name=%s", (name,))
            return cur.fetchone()

    def audit(self, owner, action, detail=None):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO audit(owner,action,detail) VALUES (%s,%s,%s)",
                        (owner, action, Json(detail or {})))


store = Store()
