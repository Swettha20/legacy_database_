"""Real API acceptance checks; requires a running disposable/trusted local stack."""
import argparse,hashlib,json,os,secrets,time,uuid,zipfile,io
from pathlib import Path
import requests
p=argparse.ArgumentParser();p.add_argument('--internal',action='store_true');p.add_argument('--output',default='/app/logs/integration.json');args=p.parse_args()
if args.internal:
 base='http://api:8000';password=os.environ['ADMIN_PASSWORD']
else:
 cfg=dict(line.split('=',1) for line in Path('.env.stack').read_text().splitlines() if '=' in line and not line.startswith('#'))
 base='http://localhost:'+cfg.get('STACK_API_PORT','18000');password=cfg['STACK_ADMIN_PASSWORD']
s=requests.Session();checks=[]
def check(name,condition):
 assert condition,name
 checks.append({'name':name,'passed':True});print('PASS',name,flush=True)
def post(url,body):
 r=s.post(base+url,json=body,timeout=30);r.raise_for_status();return r.json()
def wait(jid):
 for _ in range(900):
  r=s.get(base+'/v1/jobs/'+jid,timeout=30);r.raise_for_status();j=r.json()
  if j['status'] in ['completed','completed_with_warnings','needs_review','failed','cancelled','interrupted']:return j
  time.sleep(.4)
 raise TimeoutError(jid)
def upload(raw):
 r=s.post(base+'/v1/uploads',files={'file':('acceptance.sql',raw,'application/sql')},timeout=30);r.raise_for_status();return r.json()
check('private jobs require login',requests.get(base+'/v1/jobs').status_code==401)
post('/v1/auth/login',{'username':'admin','password':password})
raw=b"CREATE TABLE exact_values (id NUMBER(9) PRIMARY KEY, amount NUMBER(38,8), memo NVARCHAR2(120), stamp TIMESTAMP(6), payload RAW(16));\nINSERT INTO exact_values VALUES (1,123456789012345678901234567890.12345678,UNISTR('caf\\00e9 \\4f60\\597d'),TIMESTAMP '2026-01-12 10:11:12.123456',HEXTORAW('00FFAB'));\nINSERT INTO exact_values VALUES (2,NULL,NULL,NULL,NULL);\nCREATE INDEX exact_memo_idx ON exact_values(memo);\nCREATE VIEW exact_names AS SELECT id, memo FROM exact_values;\nCREATE TABLE duplicate_values (v NUMBER, memo VARCHAR2(20));\nINSERT INTO duplicate_values VALUES (7,'same');\nINSERT INTO duplicate_values VALUES (7,'same');\nCOMMIT;\n"
u=upload(raw);check('upload checksum preserves original bytes',u['sha256']==hashlib.sha256(raw).hexdigest())
r=s.get(base+'/v1/uploads/'+u['id']+'/download');check('original download byte identity',r.content==raw)
plan=post('/v1/plans',{'upload_id':u['id']});review=wait(plan['job_id']);check('inspection precedes target creation',review['status']=='needs_review' and not review.get('target'))
r=s.patch(base+'/v1/plans/'+plan['job_id']+'/mappings',json={'mappings':{'exact_values.amount':'TEXT); DROP DATABASE postgres;--'}});check('unsafe override rejected',r.status_code==422)
key=uuid.uuid4().hex;body={'plan_id':plan['job_id']}
a=s.post(base+'/v1/jobs',json=body,headers={'Idempotency-Key':key});a.raise_for_status()
b=s.post(base+'/v1/jobs',json=body,headers={'Idempotency-Key':key});b.raise_for_status();check('repeated request creates one job',a.json()['job_id']==b.json()['job_id'])
j=wait(a.json()['job_id']);check('exact decimals Unicode binary nulls and duplicates survive',j.get('verification',{}).get('passed',False));check('export restored and fully checked',j['verification']['export_restore']['passed']);check('approved structure verified',j['verification']['structure']['passed'])
files=s.get(base+'/v1/jobs/'+j['id']+'/artifacts').json()['items'];artifact=next(a for a in files if a['filename']=='converted_postgresql.sql')
r=s.get(base+'/v1/artifacts/'+artifact['id']+'/download');check('downloaded artifact checksum',hashlib.sha256(r.content).hexdigest()==artifact['sha256'])
partial=s.get(base+'/v1/artifacts/'+artifact['id']+'/download',headers={'Range':'bytes=0-31'})
check('download range retries preserve bytes',partial.status_code==206 and partial.content==r.content[:32])
bundle=s.get(base+'/migrate/download/'+j['id']);bundle.raise_for_status()
with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
 check('bundle contains untouched original',archive.read('original.sql')==raw)
events=s.get(base+'/v1/jobs/'+j['id']+'/events').json()['events']
check('persistent event IDs support reconnect',bool(events) and not s.get(base+'/v1/jobs/'+j['id']+'/events',params={'after':events[-1]['seq']}).json()['events'])
username='test_'+secrets.token_hex(5);other_password=secrets.token_urlsafe(18)
post('/v1/users',{'username':username,'password':other_password})
other=requests.Session();other.post(base+'/v1/auth/login',json={'username':username,'password':other_password}).raise_for_status()
for url in ['/v1/jobs/'+j['id'],'/v1/uploads/'+u['id']+'/download','/v1/artifacts/'+artifact['id']+'/download']:
 check('owner isolation '+url.split('/')[2],other.get(base+url).status_code==404)
check('ordinary users cannot create accounts',other.post(base+'/v1/users',json={'username':'denied_user','password':secrets.token_urlsafe(18)}).status_code==403)
bad=upload(b'CREATE TABLE bad_input (id NUMBER PRIMARY KEY);\nINSERT INTO bad_input VALUES (1);\nINSERT INTO bad_input VALUES (1);\n')
failure=wait(post('/v1/jobs',{'upload_id':bad['id']})['job_id']);check('failed source statement stops migration',failure['status']=='failed' and not failure['file_available']);check('failure still exposes original',s.get(base+'/v1/uploads/'+bad['id']+'/download').status_code==200)
check('failed job preserves previous artifact',hashlib.sha256(s.get(base+'/v1/artifacts/'+artifact['id']+'/download').content).hexdigest()==artifact['sha256'])
result={'checks':checks,'successful_job':j['id'],'failed_job':failure['id'],'verification':j['verification']}
Path(args.output).parent.mkdir(parents=True,exist_ok=True);Path(args.output).write_text(json.dumps(result,indent=2));print('ALL',len(checks),'CHECKS PASSED')
