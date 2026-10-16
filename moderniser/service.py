"""Authenticated API control plane. Database execution belongs to workers."""
import hashlib
import json
import os
import re
import time
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field
from .store import store, directory, identifier, now, TERMINAL
from .queue import celery

app = FastAPI(title='Database Moderniser', version='1.0.0')
MAX_BYTES = int(os.getenv('MAX_UPLOAD_BYTES', str(25 * 1024 * 1024)))
STEPS = [('loading_script','Import source safely'), ('reading_schema','Inspect source'),
         ('mapping_types','Review exact type mappings'), ('writing_postgres_schema','Create isolated target'),
         ('migrating_data','Copy rows'), ('verifying_data','Compare every value'),
         ('exporting_files','Build downloads'), ('restoring_export','Restore and verify download')]


@app.middleware('http')
async def request_id(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.cookies.get('moderniser_session'):
        origin = request.headers.get('origin')
        # Browser sessions require a same-site origin. Bearer clients are independent.
        if origin and origin.split('://')[-1] != request.headers.get('host') and not request.headers.get('authorization'):
            return JSONResponse({'detail':'Cross-origin session write rejected'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Request-ID'] = str(uuid.uuid4())
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def token(request):
    auth = request.headers.get('authorization', '')
    return auth[7:] if auth.startswith('Bearer ') else request.cookies.get('moderniser_session', '')


def user(request: Request, credentials=Depends(HTTPBearer(auto_error=False))):
    account = store.user_for_token(token(request))
    if not account:
        raise HTTPException(401, 'Sign in to access your migrations and files.')
    return account['username']


def owned(kind, value, owner):
    try:
        record = store.get(kind, value, owner)
    except (ValueError, AttributeError):
        record = None
    if not record or record.get('deleted'):
        raise HTTPException(404, kind.title() + ' not found')
    return record


class Login(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=256)


@app.post('/v1/auth/login')
def login(body: Login, request: Request, response: Response):
    try:
        session = store.authenticate(body.username, body.password, request.client.host)
    except PermissionError as exc:
        raise HTTPException(429, str(exc))
    if not session:
        raise HTTPException(401, 'Incorrect username or password')
    response.set_cookie('moderniser_session', session, httponly=True, samesite='strict',
                        secure=os.getenv('COOKIE_SECURE') == '1', max_age=43200)
    store.audit(body.username, 'login')
    return {'username':body.username, 'access_token':session, 'token_type':'bearer', 'expires_in':43200}


@app.get('/v1/auth/me')
def me(owner=Depends(user)):
    return {'username':owner}


@app.post('/v1/auth/logout')
def logout(request: Request, response: Response, owner=Depends(user)):
    store.logout(token(request))
    response.delete_cookie('moderniser_session')
    return {'status':'signed_out'}


class NewUser(Login):
    pass


@app.post('/v1/users', status_code=201)
def create_user(body: NewUser, request: Request, owner=Depends(user)):
    if store.user_for_token(token(request))['role'] != 'admin':
        raise HTTPException(403, 'Administrator access required')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,80}', body.username) or len(body.password) < 12:
        raise HTTPException(422, 'Use a 3–80 character username and a password of at least 12 characters.')
    try:
        store.create_user(body.username, body.password)
    except Exception:
        raise HTTPException(409, 'Username already exists or account could not be created')
    store.audit(owner, 'create_user', {'username':body.username})
    return {'username':body.username}


@app.get('/health/live')
def live():
    return {'status':'ok', 'service':'legacy-db-modernizer'}


@app.get('/health/ready')
def ready():
    checks = []
    try:
        with store.connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT 1')
        checks.append({'service':'Metadata', 'status':'PASS'})
        import redis
        redis.Redis.from_url(os.getenv('REDIS_URL','redis://queue:6379/0'), socket_timeout=2).ping()
        checks.append({'service':'Queue','status':'PASS'})
        worker = store.health('worker')
        if not worker or worker['age'] > 45:
            raise RuntimeError('Worker heartbeat expired')
        checks.append({'service':'Worker','status':'PASS'})
        checks.extend(worker['data'].get('checks',[]))
        if any(check['status']!='PASS' for check in checks):
            raise RuntimeError('Source unavailable')
        return {'status':'ready','checks':checks}
    except Exception:
        return JSONResponse({'status':'not_ready','checks':checks}, status_code=503)


@app.get('/system/info')
def info():
    return {'source':'Oracle','target':'PostgreSQL','ai':os.getenv('LLM_PROVIDER','ollama'),
            'max_upload_bytes':MAX_BYTES,'job_history':'persistent', 'deployment':'local Docker',
            'retention_days':7, 'parse_limit_bytes':MAX_BYTES}


@app.get('/v1/benchmarks')
def benchmarks(owner=Depends(user)):
    path=Path(__file__).resolve().parents[1]/'benchmarks/results/local.json'
    if not path.is_file():
        return {'status':'not_run','items':[]}
    data=json.loads(path.read_text())
    return {'status':'completed' if data.get('summary') else 'in_progress','dataset':data['dataset'],
        'source_commit':data['commit'],'rows':data['expected_rows'],'scope':data['scope'],
        'summary':data.get('summary',[]),'items':[
            {k:run.get(k) for k in ('method','batch_size','repeat','measured','seconds','worker_peak_memory_bytes','api_p95_seconds','status')}
            | {'data_verified':run.get('verification',{}).get('passed',False),
               'restore_verified':run.get('verification',{}).get('export_restore',{}).get('passed',False),
               'copy_seconds':run.get('copy_summary',{}).get('seconds')}
            for run in data['runs']]}


def file_response(record, path, filename):
    if not path.is_file():
        raise HTTPException(404, 'File unavailable or expired')
    if record.get('expires_at') and datetime.fromisoformat(record['expires_at']) < datetime.now(timezone.utc):
        raise HTTPException(410, 'File retention period has expired')
    return FileResponse(path, filename=filename, headers={'Cache-Control':'private, no-store'})


@app.post('/v1/uploads', status_code=201)
async def upload(file: UploadFile = File(...), owner=Depends(user)):
    # A bounded parser limit is deliberately retained until larger scripts are benchmarked.
    uid = str(uuid.uuid4())
    path = directory(uid) / 'original.sql'
    size, digest = 0, hashlib.sha256()
    with store.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT coalesce(sum((data->>'size')::bigint),0) n FROM records WHERE kind='upload' AND owner=%s AND NOT coalesce((data->>'deleted')::boolean,false)", (owner,))
        if cur.fetchone()['n'] >= 500 * 1024 * 1024:
            raise HTTPException(413, 'Your stored upload quota is 500 MiB. Delete unused uploads first.')
    try:
        with path.open('wb') as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, 'File exceeds the tested 25 MiB parser limit.')
                digest.update(chunk)
                out.write(chunk)
        if not size:
            raise HTTPException(400, 'The file is empty')
        record = dict(id=uid,owner=owner,filename=re.sub(r'[^A-Za-z0-9._-]', '_', Path(file.filename or 'original.sql').name),
            size=size, sha256=digest.hexdigest(), created_at=now(),
            expires_at=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat())
        from psycopg2.extras import Json
        with store.connection() as conn,conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(owner,))
            cur.execute("SELECT coalesce(sum((data->>'size')::bigint),0) n FROM records WHERE kind='upload' AND owner=%s AND NOT coalesce((data->>'deleted')::boolean,false)",(owner,))
            if cur.fetchone()['n']+size>500*1024*1024:
                raise HTTPException(413,'Upload would exceed your 500 MiB storage quota')
            cur.execute("INSERT INTO records(kind,id,owner,data) VALUES ('upload',%s,%s,%s)",(uid,owner,Json(record)))
        store.audit(owner, 'upload', {'id':uid, 'bytes':size})
        return record
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@app.get('/v1/uploads/{uid}')
def get_upload(uid, owner=Depends(user)):
    return owned('upload', uid, owner)


@app.get('/v1/uploads/{uid}/download')
def original(uid, owner=Depends(user)):
    record = owned('upload', uid, owner)
    return file_response(record, directory(uid)/'original.sql', record['filename'])


@app.delete('/v1/uploads/{uid}')
def delete_upload(uid, owner=Depends(user)):
    owned('upload', uid, owner)
    active = [j for j in store.list('job', owner, limit=1000) if j.get('upload_id') == uid and j['status'] not in TERMINAL]
    if active:
        raise HTTPException(409, 'Cancel or finish this upload’s active jobs first.')
    (directory(uid)/'original.sql').unlink(missing_ok=True)
    store.update('upload', uid, lambda r: r.update(deleted=True))
    return {'status':'deleted'}


class JobInput(BaseModel):
    upload_id: str | None = None
    source_schema: str = 'DEMO'
    plan_id: str | None = None
    translate_procedures: bool = False
    mappings: dict[str,str] = Field(default_factory=dict)
    batch_size: int = Field(default=5000, ge=100, le=20000)
    load_method: Literal['copy','insert'] = 'copy'
    procedure_tests: dict | None = None


def validate_mappings(mappings):
    for key, value in mappings.items():
        if not re.fullmatch(r'[a-z][a-z0-9_$#]*\.[a-z][a-z0-9_$#]*', key) or not re.fullmatch(
            r'(?:NUMERIC(?:\(\d{1,2}(?:,\d{1,2})?\))?|INTEGER|BIGINT|TEXT|VARCHAR\(\d{1,6}\)|TIMESTAMP(?:\([0-6]\))?|BYTEA)', value):
            raise HTTPException(422, 'Mapping override is outside the supported exact-type allowlist: ' + key)


def enqueue(body, owner, inspect=False, idem=None):
    if body.source_schema not in ('DEMO', 'SH'):
        raise HTTPException(422, 'Only configured DEMO and SH sources are allowed.')
    if body.plan_id:
        plan = owned('job', body.plan_id, owner)
        if not plan.get('inspect_only') or not plan.get('inspection'):
            raise HTTPException(409, 'Inspection has not finished')
        body.upload_id = plan.get('upload_id')
        body.source_schema = plan['source_schema']
        body.mappings = plan.get('mappings', {}) | body.mappings
    if body.upload_id:
        owned('upload', body.upload_id, owner)
    validate_mappings(body.mappings)
    if body.procedure_tests is not None:
        spec=body.procedure_tests
        if not body.upload_id or not isinstance(spec.get('cases'),list) or len(spec['cases'])>100 or len(json.dumps(spec))>32768:
            raise HTTPException(422,'Routine test cases require an uploaded isolated source and a maximum 32 KiB / 100 cases.')
        for case in spec['cases']:
            if not isinstance(case,dict) or not isinstance(case.get('call'),str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}',case.get('call','')):
                raise HTTPException(422,'Invalid routine name')
            if not isinstance(case.get('args',[]),list) or len(case.get('args',[]))>20:
                raise HTTPException(422,'Routine arguments must be an array of at most 20 values')
            if 'returns' in case and case['returns'] not in ('number','string','date'):
                raise HTTPException(422,'Routine return type must be number, string or date')
            for value in case.get('args',[]):
                if not (value is None or isinstance(value,(str,int,float)) or
                    isinstance(value,dict) and set(value)=={'out'} and value['out'] in ('number','string','date')):
                    raise HTTPException(422,'Unsupported routine argument')
            if not isinstance(case.get('observe',[]),list):
                raise HTTPException(422,'Observation queries must be an array')
            for query in case.get('observe',[]):
                if not isinstance(query,str) or not re.fullmatch(r'\s*SELECT\s+[A-Za-z0-9_, *]+\s+FROM\s+[A-Za-z_][A-Za-z0-9_]*(?:\s+ORDER\s+BY\s+[A-Za-z0-9_, ]+)?\s*',query,re.I):
                    raise HTTPException(422,'Observation queries support only single-table column projections and ORDER BY.')
    payload = body.model_dump() | {'inspect_only':inspect}
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    jid = str(uuid.uuid4())
    # Idempotency and record creation share one transaction, including concurrent requests.
    with store.connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (owner,))
        if idem:
            if len(idem) > 120:
                raise HTTPException(422, 'Idempotency-Key is too long')
            cur.execute('SELECT request_hash,job_id FROM idempotency WHERE owner=%s AND key=%s', (owner, idem))
            prior = cur.fetchone()
            if prior:
                if prior['request_hash'] != request_hash:
                    raise HTTPException(409, 'Idempotency key was already used for different input')
                return {'job_id':str(prior['job_id']), 'status':'accepted'}
        cur.execute("SELECT count(*) n FROM records WHERE kind='job' AND owner=%s AND data->>'status' IN ('queued','running')", (owner,))
        if cur.fetchone()['n'] >= 3:
            raise HTTPException(429, 'At most three queued or running jobs per account')
        from psycopg2.extras import Json
        record = dict(payload, id=jid, owner=owner, status='queued', phase='queued', created_at=now(),
                      started_ts=time.time(), steps=[dict(key=k,label=v) for k,v in STEPS], step=0,
                      step_total=len(STEPS), tables={}, table_order=[], rows_total=0, warnings=[],
                      procedures=[], committed=False, error=None, failure=None,
                      mode='upload' if body.upload_id else 'database')
        if body.upload_id:
            record['filename'] = owned('upload', body.upload_id, owner)['filename']
        cur.execute("INSERT INTO records(kind,id,owner,data) VALUES ('job',%s,%s,%s)", (jid,owner,Json(record)))
        if idem:
            cur.execute('INSERT INTO idempotency VALUES (%s,%s,%s,%s)', (owner,idem,request_hash,jid))
    try:
        celery.send_task('moderniser.run', args=[jid])
    except Exception:
        # Reconciler republishes durable queued jobs if the broker is temporarily down.
        pass
    store.audit(owner, 'queue_job', {'id':jid})
    return {'job_id':jid, 'status':'queued'}


@app.post('/v1/jobs', status_code=202)
def start(body: JobInput, request: Request, owner=Depends(user)):
    return enqueue(body, owner, idem=request.headers.get('idempotency-key'))


@app.post('/v1/plans', status_code=202)
def inspect(body: JobInput, owner=Depends(user)):
    return enqueue(body, owner, inspect=True)


@app.post('/v1/connections/test', status_code=202)
def test_connection(body: JobInput, owner=Depends(user)):
    """Inspect an allowlisted source asynchronously; arbitrary destinations are not accepted."""
    if body.upload_id:
        raise HTTPException(422, 'Choose the configured DEMO or SH source')
    return enqueue(body,owner,inspect=True)


@app.get('/v1/plans/{jid}')
def get_plan(jid, owner=Depends(user)):
    return owned('job', jid, owner)


class Mappings(BaseModel):
    mappings: dict[str,str]


@app.patch('/v1/plans/{jid}/mappings')
def mappings(jid, body: Mappings, owner=Depends(user)):
    plan = owned('job', jid, owner)
    if not plan.get('inspect_only') or plan['status'] != 'needs_review':
        raise HTTPException(409, 'A completed inspection is required')
    validate_mappings(body.mappings)
    return store.update('job', jid, lambda r: r.update(mappings=body.mappings, mapping_version=r.get('mapping_version',0)+1))


@app.get('/v1/jobs')
def history(limit: int=30, offset: int=0, owner=Depends(user)):
    return {'items':[r for r in store.list('job',owner,max(1,min(limit,100)),max(offset,0)) if not r.get('deleted')]}


@app.delete('/v1/jobs/{jid}',status_code=202)
def delete_job(jid,owner=Depends(user)):
    job = owned('job',jid,owner)
    if job['status'] not in TERMINAL:
        raise HTTPException(409, 'Cancel or finish the job before deleting it')
    store.update('job',jid,lambda r:r.update(delete_requested=True))
    return {'status':'deletion_requested','scope':'Job-owned target and artifacts; original upload has a separate delete operation'}


@app.get('/v1/jobs/{jid}')
@app.get('/migrate/status/{jid}', include_in_schema=False)
def status(jid, owner=Depends(user)):
    record = owned('job', jid, owner)
    record['elapsed_seconds'] = record.get('elapsed_seconds', round(time.time()-record['started_ts'],1))
    path = directory(jid)
    record.update(report_available=(path/'report.txt').is_file(),
                  file_available=(path/'converted_postgresql.sql').is_file(),
                  download_available=(path/'bundle.zip').is_file())
    if record['status'] in TERMINAL and not record.get('artifacts'):
        record.update(status='running',phase='finalizing_files')
    return record


@app.get('/v1/jobs/{jid}/events')
def events(jid, after: int=0, owner=Depends(user)):
    owned('job', jid, owner)
    return {'events':store.events(jid,owner,max(after,0)), 'reconnect':'Pass the last seq as after'}


@app.get('/v1/jobs/{jid}/events/stream')
def event_stream(jid,request: Request,after: int=0,owner=Depends(user)):
    owned('job',jid,owner)
    try:
        cursor=max(after,int(request.headers.get('last-event-id','0')))
    except ValueError:
        raise HTTPException(422,'Last-Event-ID must be an integer')
    async def generate():
        nonlocal cursor
        for _ in range(300):
            if await request.is_disconnected():
                return
            rows=await run_in_threadpool(store.events,jid,owner,cursor)
            for row in rows:
                cursor=row['seq']
                yield 'id: '+str(cursor)+'\ndata: '+json.dumps(row['data'],default=str)+'\n\n'
            job=await run_in_threadpool(store.get,'job',jid,owner)
            if job['status'] in TERMINAL and not rows:
                yield 'event: finished\ndata: '+json.dumps({'status':job['status']})+'\n\n'
                return
            yield ': heartbeat\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@app.post('/v1/jobs/{jid}/cancel', status_code=202)
def cancel(jid, owner=Depends(user)):
    job = owned('job', jid, owner)
    if job['status'] in TERMINAL:
        raise HTTPException(409, 'Job already finished')
    store.update('job',jid,lambda r:r.update(cancel_requested=True))
    return {'status':'cancellation_requested'}


@app.post('/v1/jobs/{jid}/retry', status_code=202)
def retry(jid, owner=Depends(user)):
    job = owned('job',jid,owner)
    if job['status'] not in TERMINAL:
        raise HTTPException(409, 'Job is still active')
    return enqueue(JobInput(**{k:job[k] for k in JobInput.model_fields if k in job and k != 'plan_id'}),owner)


@app.get('/v1/jobs/{jid}/verification')
def verification(jid, owner=Depends(user)):
    return owned('job',jid,owner).get('verification',{'status':'not_verified'})


@app.post('/v1/jobs/{jid}/verification',status_code=202)
def repeat_verification(jid,owner=Depends(user)):
    """Fresh-source verification reruns into a new isolated target; old evidence stays immutable."""
    return retry(jid,owner)


@app.get('/v1/jobs/{jid}/artifacts')
def artifacts(jid, owner=Depends(user)):
    return {'items':owned('job',jid,owner).get('artifacts',[])}


@app.get('/v1/artifacts/{aid}/download')
def download(aid, owner=Depends(user)):
    record = owned('artifact',aid,owner)
    path = directory(record['job_id']) / record['filename']
    # Stream hashing so serving a large file never doubles its memory footprint.
    if not path.is_file():
        raise HTTPException(404, 'Artifact unavailable')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream,'sha256').hexdigest()
    if digest != record['sha256']:
        raise HTTPException(409, 'Artifact checksum mismatch or file unavailable')
    return file_response(record,path,record['filename'])


@app.post('/upload/file', status_code=202, include_in_schema=False)
async def legacy_upload(file: UploadFile=File(...), translate_procedures: bool=Form(False), owner=Depends(user)):
    record = await upload(file,owner)
    return enqueue(JobInput(upload_id=record['id'],translate_procedures=translate_procedures),owner)


@app.post('/migrate/start', status_code=202, include_in_schema=False)
def legacy_start(body: JobInput, owner=Depends(user)):
    return enqueue(body,owner)


@app.get('/upload/original/{jid}', include_in_schema=False)
def legacy_original(jid, owner=Depends(user)):
    job = owned('job',jid,owner)
    if not job.get('upload_id'):
        raise HTTPException(404,'This job has no upload')
    return original(job['upload_id'],owner)


def legacy_file(jid, filename, owner):
    job = owned('job',jid,owner)
    record = next((a for a in job.get('artifacts',[]) if a['filename']==filename),None)
    if not record:
        raise HTTPException(404,'File has not been produced')
    return download(record['id'],owner)


@app.get('/migrate/file/{jid}', include_in_schema=False)
def converted(jid, owner=Depends(user)):
    return legacy_file(jid,'converted_postgresql.sql',owner)


@app.get('/migrate/report/{jid}', include_in_schema=False)
def report(jid, owner=Depends(user)):
    return legacy_file(jid,'report.txt',owner)


@app.get('/migrate/download/{jid}', include_in_schema=False)
@app.get('/upload/download/{jid}', include_in_schema=False)
def bundle(jid, owner=Depends(user)):
    return legacy_file(jid,'bundle.zip',owner)
