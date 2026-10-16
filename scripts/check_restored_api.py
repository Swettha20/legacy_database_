"""Serve an isolated restored control-plane copy and verify authenticated downloads."""
import hashlib,json,re,socket,subprocess,time,uuid
from pathlib import Path
import requests
report_path=Path('benchmarks/results/backup-restore.json');report=json.loads(report_path.read_text())
assert re.fullmatch(r'moderniser_recovery_[a-f0-9]{16}',report['database'])
assert re.fullmatch(r'/app/uploads/recovery_[a-f0-9]{16}',report['artifact_root'])
cfg=dict(line.split('=',1) for line in Path('.env.stack').read_text().splitlines() if '=' in line and not line.startswith('#'))
with socket.socket() as probe:
 probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
name='moderniser-restore-check-'+uuid.uuid4().hex[:10]
base='http://127.0.0.1:'+str(port)
command=['docker','compose','--env-file','.env.stack','run','--rm','-d','--no-deps','--name',name,'--publish',f'127.0.0.1:{port}:8000','--env','META_DB='+report['database'],'--env','ARTIFACT_ROOT='+report['artifact_root'],'api']
try:
 subprocess.run(command,check=True,stdout=subprocess.DEVNULL)
 for _ in range(60):
  try:
   if requests.get(base+'/health/live',timeout=2).status_code==200:break
  except requests.RequestException:pass
  time.sleep(.5)
 session=requests.Session();session.post(base+'/v1/auth/login',json={'username':'admin','password':cfg['STACK_ADMIN_PASSWORD']},timeout=15).raise_for_status()
 response=session.get(base+'/v1/jobs',timeout=15);response.raise_for_status();jobs=response.json()['items']
 job=next(j for j in jobs if any(a['filename']=='report.txt' for a in j.get('artifacts',[])))
 artifact=next(a for a in job['artifacts'] if a['filename']=='report.txt')
 downloaded=session.get(base+'/v1/artifacts/'+artifact['id']+'/download',timeout=30);downloaded.raise_for_status()
 assert hashlib.sha256(downloaded.content).hexdigest()==artifact['sha256']
 report.update(restored_api_login=True,restored_history=True,restored_artifact_download=True)
 job=next(j for j in jobs if j.get('upload_id'))
 original=session.get(base+'/v1/uploads/'+job['upload_id'],timeout=15);original.raise_for_status()
 downloaded=session.get(base+'/v1/uploads/'+job['upload_id']+'/download',timeout=30);downloaded.raise_for_status()
 assert hashlib.sha256(downloaded.content).hexdigest()==original.json()['sha256']
 report['restored_original_download']=True
 report_path.write_text(json.dumps(report,indent=2));print('PASS Restored API login, history, artifact download and original byte identity')
finally:
 subprocess.run(['docker','stop',name],check=False,stdout=subprocess.DEVNULL)
