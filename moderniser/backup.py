"""Consistent control-plane backup. Run only when jobs are idle and API writes stopped."""
import argparse,json,os,subprocess,tarfile,uuid
from pathlib import Path
from .store import store,ROOT
p=argparse.ArgumentParser();p.add_argument('action',choices=['create','restore-test']);p.add_argument('--path',default='/app/logs/backups/latest');a=p.parse_args();backup=Path(a.path)
env=dict(os.environ,PGPASSWORD=os.environ['PG_PASSWORD'])
common=['-h',os.getenv('PG_HOST','postgres'),'-U',os.getenv('PG_USER','postgres')]
if a.action=='create':
 active=[j for j in store.list('job',limit=10000) if j['status'] in ('queued','running')]
 if active:raise SystemExit('Wait for active jobs before backing up. Stop API writes during this command.')
 backup.mkdir(parents=True,exist_ok=False)
 subprocess.run(['pg_dump',*common,'-Fc','-d','moderniser_meta','-f',str(backup/'metadata.dump')],env=env,check=True)
 with tarfile.open(backup/'artifacts.tar.gz','w:gz') as out:
  if ROOT.exists():
   for item in ROOT.iterdir():
    if item.is_dir():
     uuid.UUID(item.name)
     out.add(item,arcname=item.name)
 (backup/'backup.json').write_text(json.dumps({'metadata':'metadata.dump','artifacts':'artifacts.tar.gz','format':1},indent=2))
 print('Backup saved to',backup)
else:
 suffix=uuid.uuid4().hex[:16];database='moderniser_recovery_'+suffix;target=ROOT.parent/('recovery_'+suffix)
 import psycopg2
 conn=psycopg2.connect(host=os.getenv('PG_HOST','postgres'),user=os.getenv('PG_USER','postgres'),password=os.environ['PG_PASSWORD'],dbname='postgres');conn.autocommit=True
 with conn.cursor() as c:c.execute('CREATE DATABASE '+database+' OWNER moderniser')
 conn.close()
 subprocess.run(['pg_restore',*common,'--exit-on-error','-d',database,str(backup/'metadata.dump')],env=env,check=True)
 target.mkdir(parents=True,exist_ok=False)
 with tarfile.open(backup/'artifacts.tar.gz','r:gz') as archive:archive.extractall(target,filter='data')
 os.environ.update(META_DB=database,ARTIFACT_ROOT=str(target))
 with store.connection() as conn,conn.cursor() as cur:
  cur.execute('SELECT count(*) n FROM records');count=cur.fetchone()['n']
 result=dict(database=database,artifact_root=str(target),records=count,verified=False)
 # Check all recorded artifact bytes in the restored copy, including report and ZIP files.
 import hashlib
 checked=0
 for record in store.list('artifact',limit=100000):
  file=target/record['job_id']/record['filename']
  if record.get('deleted'):continue
  with file.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
  assert digest==record['sha256'],file
  checked+=1
 result.update(verified=True,artifacts_checked=checked)
 uploads_checked=0
 for record in store.list('upload',limit=100000):
  if record.get('deleted'):continue
  file=target/record['id']/'original.sql'
  with file.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
  assert digest==record['sha256'],file
  uploads_checked+=1
 result['original_uploads_checked']=uploads_checked
 (backup/'restore-test.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
