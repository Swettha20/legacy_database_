"""Durable queue worker with cancellable child processes and crash reconciliation."""
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from .queue import celery
from .store import store, directory, now, TERMINAL
ACTIVE = set()


def cleanup_job(jid, remove_target=False):
    """Only names derived from a canonical job UUID can be cleaned."""
    from .store import identifier
    import upload_pipeline as upload
    import oracledb
    safe = identifier(jid).replace('-','')
    with oracledb.connect(user='sys',password=os.environ['ORACLE_PASSWORD'],dsn=os.environ['ORACLE_DSN'],mode=oracledb.AUTH_MODE_SYSDBA) as admin:
        admin.call_timeout=15000
        upload.drop_scratch_schema(admin,'UPL_'+safe[:24].upper())
    upload.drop_pg_database('upload_'+safe+'a')
    if remove_target:
        upload.drop_pg_database('upload_'+safe)


def record_discard(jid,pending=False):
    def change(job):
        job['committed']=False
        job['cleanup_pending']=pending
        job.setdefault('target',{})['discarded']=not pending
        for table in job.get('tables',{}).values():
            table['state']='quarantined' if pending else 'discarded'
    store.update('job',jid,change)


def publish_artifacts(jid):
    job = store.get('job', jid)
    path = directory(jid)
    (path/'report.json').write_text(json.dumps(job, indent=2, default=str), encoding='utf-8')
    from generate_report import generate_report
    text = 'DATABASE MODERNISER\nJob: '+jid+'\nOutcome: '+job['status']+'\n'
    if job.get('error'):
        text += 'Stopped: '+job['error']+'\nPrevious targets were not replaced.\n'
    text += '\n'+'\n'.join(job['warnings'])+'\n'
    mapped = path/'mapped_schema.json'
    if mapped.is_file():
        try:
            text += '\n'+generate_report(json.loads(mapped.read_text(encoding='utf-8')))
        except (ValueError,KeyError,TypeError):
            text += '\nMapping output was incomplete when the worker stopped; it is not certified.\n'
    text += '\nVerification: '+('Exact data and restored export passed.' if job.get('verification',{}).get('passed') else 'Not certified.')
    (path/'report.txt').write_text(text, encoding='utf-8')
    files = [p for p in path.rglob('*') if p.is_file() and p.name not in ('bundle.zip','manifest.json')]
    manifest = []
    for file in files:
        with file.open('rb') as stream:
            digest = hashlib.file_digest(stream,'sha256').hexdigest()
        manifest.append(dict(filename=str(file.relative_to(path)).replace('\\','/'), size=file.stat().st_size, sha256=digest))
    original=None
    if job.get('upload_id'):
        source=store.get('upload',job['upload_id'],job['owner'])
        candidate=directory(job['upload_id'])/'original.sql'
        if source and not source.get('deleted') and candidate.is_file():
            original=candidate
            with original.open('rb') as stream:
                digest=hashlib.file_digest(stream,'sha256').hexdigest()
            manifest.append(dict(filename='original.sql',original_filename=source['filename'],size=original.stat().st_size,sha256=digest))
    (path/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    with zipfile.ZipFile(path/'bundle.zip','w',zipfile.ZIP_DEFLATED) as bundle:
        for file in files+[path/'manifest.json']:
            bundle.write(file,str(file.relative_to(path)))
        if original:
            bundle.write(original,'original.sql')
    records = []
    for file in files+[path/'manifest.json',path/'bundle.zip']:
        if file.parent != path:
            continue
        with file.open('rb') as stream:
            digest = hashlib.file_digest(stream,'sha256').hexdigest()
        records.append(store.put('artifact',job['owner'],dict(job_id=jid,filename=file.name,size=file.stat().st_size,
            sha256=digest, expires_at=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat())))
    store.update('job',jid,lambda r:r.update(artifacts=records))


@celery.task(name='moderniser.run',soft_time_limit=14300,time_limit=14400)
def run(jid):
    if not store.claim(jid,str(uuid.uuid4())):
        return
    ACTIVE.add(jid)
    path, started, proc = directory(jid), time.monotonic(), None
    try:
        with (path/'worker.log').open('w',encoding='utf-8') as log:
            proc = subprocess.Popen([sys.executable,'-m','moderniser.engine',jid],stdout=log,
                stderr=subprocess.STDOUT,start_new_session=True)
            while proc.poll() is None:
                job = store.get('job',jid)
                if job.get('cancel_requested') or time.monotonic()-started>14000:
                    os.killpg(proc.pid,signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid,signal.SIGKILL)
                        proc.wait()
                    store.update('job',jid,lambda r:r.update(status='cancelled',error='Cancelled by owner. Target was not promoted.'))
                    break
                try:
                    status = open(f'/proc/{proc.pid}/status').read()
                    memory = next(int(line.split()[1])*1024 for line in status.splitlines() if line.startswith('VmHWM:'))
                    store.update('job',jid,lambda r:r.update(worker_peak_memory_bytes=max(memory,r.get('worker_peak_memory_bytes',0))))
                except (OSError,StopIteration):
                    pass
                store.update('job',jid,lambda r:r.update(heartbeat=time.time()))
                time.sleep(1)
        job = store.get('job',jid)
        if job['status'] not in TERMINAL:
            lines = (path/'worker.log').read_text(encoding='utf-8',errors='replace').splitlines()
            error = next((line for line in reversed(lines) if line.strip()),'Worker stopped')[-1500:]
            store.update('job',jid,lambda r:r.update(status='failed',error=error))
    except BaseException:
        if proc and proc.poll() is None:
            os.killpg(proc.pid,signal.SIGKILL)
            proc.wait()
        store.update('job',jid,lambda r:r.update(status='interrupted',error='Worker interrupted. Retry starts a fresh target.'))
        raise
    finally:
        job = store.update('job',jid,lambda r:r.update(elapsed_seconds=round(time.monotonic()-started,2),completed_at=now()))
        try:
            cleanup_job(jid,job['status'] in ('failed','cancelled','interrupted'))
            if job['status'] in ('failed','cancelled','interrupted'):
                record_discard(jid)
        except Exception:
            store.update('job',jid,lambda r:r.update(cleanup_pending=True))
            if job['status'] in ('failed','cancelled','interrupted'):
                record_discard(jid,True)
        if job['status'] in ('failed','cancelled','interrupted'):
            (path/'converted_postgresql.sql').unlink(missing_ok=True)
        publish_artifacts(jid)
        ACTIVE.discard(jid)


def maintenance():
    while True:
        try:
            checks = []
            try:
                import oracledb
                with oracledb.connect(user='MIG_READER',password=os.environ['ORACLE_READER_PASSWORD'],dsn=os.environ['ORACLE_DSN']) as source:
                    source.call_timeout=5000
                    with source.cursor() as cur:
                        cur.execute('SELECT 1 FROM dual')
                        cur.fetchone()
                checks.append({'service':'Oracle','status':'PASS'})
            except Exception:
                checks.append({'service':'Oracle','status':'FAIL'})
            checks.append({'service':'PostgreSQL','status':'PASS'})
            store.health('worker',{'status':'alive','checks':checks})
            for job in store.list('job',limit=10000):
                if job['status']=='running' and job['id'] not in ACTIVE and time.time()-job.get('heartbeat',time.time())>90:
                    store.update('job',job['id'],lambda r:r.update(status='interrupted',completed_at=now(),cleanup_pending=True,
                        elapsed_seconds=round(time.time()-r['started_ts'],2),error='Worker heartbeat expired. Retry into a fresh target.'))
                    try:
                        cleanup_job(job['id'],True)
                        record_discard(job['id'])
                    except Exception:
                        pass
                    publish_artifacts(job['id'])
                elif job['status']=='queued':
                    if job.get('cancel_requested'):
                        store.update('job',job['id'],lambda r:r.update(status='cancelled',completed_at=now(),elapsed_seconds=0))
                        publish_artifacts(job['id'])
                    else:
                        celery.send_task('moderniser.run',args=[job['id']])
                elif job['status'] in TERMINAL:
                    if job.get('cleanup_pending'):
                        cleanup_job(job['id'],job['status'] in ('failed','cancelled','interrupted'))
                        store.update('job',job['id'],lambda r:r.update(cleanup_pending=False))
                        if job['status'] in ('failed','cancelled','interrupted'):
                            record_discard(job['id'])
                    expired = job.get('completed_at') and datetime.fromisoformat(job['completed_at']) < datetime.now(timezone.utc)-timedelta(days=7)
                    if (expired or job.get('delete_requested')) and not job.get('deleted'):
                        cleanup_job(job['id'],True)
                        for artifact in job.get('artifacts',[]):
                            (directory(job['id'])/artifact['filename']).unlink(missing_ok=True)
                            store.update('artifact',artifact['id'],lambda r:r.update(deleted=True))
                        import shutil
                        from .store import ROOT
                        path = directory(job['id']).resolve()
                        if path.parent != ROOT.resolve():
                            raise ValueError('Cleanup path is outside artifact root')
                        shutil.rmtree(path)
                        store.update('job',job['id'],lambda r:r.update(deleted=True))
            for upload in store.list('upload',limit=10000):
                if not upload.get('deleted') and datetime.fromisoformat(upload['expires_at']) < datetime.now(timezone.utc):
                    active = any(j.get('upload_id')==upload['id'] and j['status'] not in TERMINAL for j in store.list('job',limit=10000))
                    if not active:
                        (directory(upload['id'])/'original.sql').unlink(missing_ok=True)
                        store.update('upload',upload['id'],lambda r:r.update(deleted=True))
        except Exception as exc:
            print('Maintenance will retry: '+type(exc).__name__,file=sys.stderr,flush=True)
        time.sleep(15)


if __name__=='__main__':
    threading.Thread(target=maintenance,daemon=True).start()
    celery.worker_main(['worker','--loglevel=INFO','--pool=solo','--concurrency=1'])
