"""Local Docker recovery tests; touches only this project's API/worker and test jobs."""
import argparse,hashlib,json,subprocess,time
from pathlib import Path
import requests
p=argparse.ArgumentParser();p.add_argument('--output',default='benchmarks/results/recovery.json');args=p.parse_args()
cfg=dict(x.split('=',1) for x in Path('.env.stack').read_text().splitlines() if '=' in x and not x.startswith('#'))
base='http://localhost:'+cfg.get('STACK_API_PORT','18000');session=requests.Session();checks=[]
compose=['docker','compose','--env-file','.env.stack']
def dc(*args):subprocess.run(compose+list(args),check=True)
def check(name,value):
 assert value,name
 checks.append({'name':name,'passed':True});print('PASS',name,flush=True)
def post(path,body=None):
 r=session.post(base+path,json=body,timeout=30);r.raise_for_status();return r.json()
def get(jid):
 r=session.get(base+'/v1/jobs/'+jid,timeout=30);r.raise_for_status();return r.json()
def wait(jid,states,phase=None,seconds=1200):
 t=time.monotonic()
 while time.monotonic()-t<seconds:
  job=get(jid)
  if job['status'] in states and (phase is None or job.get('phase')==phase):return job
  time.sleep(.5)
 raise TimeoutError(str(job))
post('/v1/auth/login',{'username':'admin','password':cfg['STACK_ADMIN_PASSWORD']})
prior=next(j for j in session.get(base+'/v1/jobs?limit=100').json()['items'] if j.get('verification',{}).get('passed'))
artifact=next(a for a in prior['artifacts'] if a['filename']=='converted_postgresql.sql')
def old_file():
 r=session.get(base+'/v1/artifacts/'+artifact['id']+'/download',timeout=30);r.raise_for_status();return hashlib.sha256(r.content).hexdigest()
check('existing artifact starts intact',old_file()==artifact['sha256'])
# API restart while a real large job remains in the independent worker.
job=post('/v1/jobs',{'source_schema':'SH'})['job_id'];wait(job,['running'],phase='migrating_data')
dc('restart','api')
for _ in range(120):
 try:
  if session.get(base+'/health/ready',timeout=2).status_code==200:break
 except requests.RequestException:pass
 time.sleep(1)
check('API restart retains authenticated history',get(prior['id'])['id']==prior['id'])
check('API restart leaves worker job active',get(job)['status']=='running')
post('/v1/jobs/'+job+'/cancel');cancelled=wait(job,['cancelled']);check('cooperative cancellation confirmed',cancelled['status']=='cancelled' and not cancelled['file_available'])
check('cancel preserves previous artifact',old_file()==artifact['sha256'])
# Kill worker only while it is operating on a newly-created benchmark target.
crash=post('/v1/jobs',{'source_schema':'SH'})['job_id'];wait(crash,['running'],phase='migrating_data')
dc('kill','-s','SIGKILL','worker');dc('up','-d','worker');print('Waiting for lease expiry/reconciliation...',flush=True)
interrupted=wait(crash,['interrupted'],seconds=240);check('worker loss becomes explicit interrupted job',interrupted['status']=='interrupted')
check('worker loss preserves previous artifact',old_file()==artifact['sha256'])
retried=post('/v1/jobs/'+crash+'/retry')['job_id'];check('retry has a new job identity',retried!=crash)
result=wait(retried,['completed','completed_with_warnings','failed','needs_review']);check('fresh retry passes data and restored export',result.get('verification',{}).get('passed') and result['verification']['export_restore']['passed'])
check('fresh retry uses a separate target',result['target']['postgres_database']!='upload_'+crash.replace('-',''))
# A redelivered completed task must not write again.
before=len(session.get(base+'/v1/jobs/'+retried+'/events').json()['events'])
dc('exec','-T','worker','python','-c',"from moderniser.queue import celery; celery.send_task('moderniser.run',args=['"+retried+"'])")
time.sleep(2)
check('duplicate delivery does not repeat phases',len(session.get(base+'/v1/jobs/'+retried+'/events').json()['events'])==before)
check('recovery preserves original artifact',old_file()==artifact['sha256'])
report={'checks':checks,'cancelled_job':job,'interrupted_job':crash,'retried_job':retried,'final_job':result}
Path(args.output).parent.mkdir(parents=True,exist_ok=True);Path(args.output).write_text(json.dumps(report,indent=2));print('ALL',len(checks),'RECOVERY CHECKS PASSED')
