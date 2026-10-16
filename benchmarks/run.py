"""Reproducible headless benchmark. Includes exact verification and export restore."""
import argparse,hashlib,json,platform,statistics,subprocess,time
from pathlib import Path
import requests

p=argparse.ArgumentParser();p.add_argument('--runs',type=int,default=3);p.add_argument('--config',default='.env.stack');p.add_argument('--output',default='benchmarks/results/local.json');p.add_argument('--methods',nargs='+',default=['copy','insert']);p.add_argument('--batches',nargs='+',type=int,default=[5000]);args=p.parse_args()
cfg=dict(line.split('=',1) for line in Path(args.config).read_text().splitlines() if '=' in line and not line.startswith('#'))
base='http://localhost:'+cfg.get('STACK_API_PORT','18000');s=requests.Session()
s.post(base+'/v1/auth/login',json={'username':'admin','password':cfg['STACK_ADMIN_PASSWORD']},timeout=20).raise_for_status()
output={'hardware':{'host':platform.platform(),'docker':subprocess.check_output(['docker','info','--format','{{json .}}'],text=True)},'dataset':'Oracle Sales History','commit':'6660bad68c07bd143430ace58565b3f727e17263','expected_rows':1063396,'warmup_included':False,'runs':[],'scope':'Local Docker; base table data, approved constraints, restored export. Oracle-specific objects remain manual.'}
# Store only safe hardware fields, not Docker configuration or host paths.
docker=json.loads(output['hardware'].pop('docker'));output['hardware'].update({k:docker.get(k) for k in ['NCPU','MemTotal','Architecture','KernelVersion','ServerVersion']})
Path(args.output).parent.mkdir(parents=True,exist_ok=True)
for method in args.methods:
 for batch in args.batches:
  for repeat in range(args.runs+1):
   measured=repeat>0;latencies=[];samples=[];t=time.monotonic()
   r=s.post(base+'/v1/jobs',json={'source_schema':'SH','batch_size':batch,'load_method':method},timeout=30);r.raise_for_status();jid=r.json()['job_id'];print(method,batch,'warmup' if not measured else repeat,jid,flush=True)
   last_sample=0
   while True:
    before=time.monotonic();r=s.get(base+'/v1/jobs/'+jid,timeout=20);r.raise_for_status();latencies.append(time.monotonic()-before);j=r.json()
    if time.monotonic()-last_sample>10:
     raw=subprocess.check_output(['docker','stats','--no-stream','--format','{{json .}}','legacy-moderniser-worker-1','legacy-moderniser-api-1','legacy-moderniser-oracle-1','legacy-moderniser-postgres-1'],text=True)
     samples.append({'seconds':round(time.monotonic()-t,2),'containers':[json.loads(line) for line in raw.splitlines()]});last_sample=time.monotonic()
    if j['status'] in ['completed','completed_with_warnings','failed','needs_review','cancelled','interrupted']:break
    time.sleep(.5)
   elapsed=time.monotonic()-t
   record={'job_id':jid,'method':method,'batch_size':batch,'repeat':repeat,'measured':measured,'seconds':round(elapsed,3),'rows':j['rows_total'],'status':j['status'],'error':j.get('error'),'verification':j.get('verification'),'warnings':j['warnings'],'copy_summary':j.get('summary'),'phase_seconds':j.get('phase_seconds'),'worker_peak_memory_bytes':j.get('worker_peak_memory_bytes'),'api_p95_seconds':sorted(latencies)[min(len(latencies)-1,int(len(latencies)*.95))],'container_samples':samples}
   output['runs'].append(record);Path(args.output).write_text(json.dumps(output,indent=2))
   print(record['status'],record['rows'],record['seconds'],'seconds, API p95',record['api_p95_seconds'],flush=True)
   if not j.get('verification',{}).get('passed') or j['rows_total']!=1063396:raise SystemExit('Correctness gate failed: '+str(j.get('error')))
summary=[]
for method in args.methods:
 for batch in args.batches:
  runs=[r for r in output['runs'] if r['method']==method and r['batch_size']==batch and r['measured']]
  times=[r['seconds'] for r in runs]
  summary.append(dict(method=method,batch_size=batch,median_seconds=statistics.median(times),range_seconds=[min(times),max(times)],median_rows_per_second=1063396/statistics.median(times),api_p95_seconds=max(r['api_p95_seconds'] for r in runs)))
output['summary']=summary;Path(args.output).write_text(json.dumps(output,indent=2));print(json.dumps(summary,indent=2))
