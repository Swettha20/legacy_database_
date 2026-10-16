"""One job per process: database module configuration cannot leak between jobs."""
import json
import os
import re
import shutil
import subprocess
import sys
import time


def run(job_id):
    from .store import store, directory
    import upload_pipeline as upload
    job = store.get('job', job_id)
    provision_password = os.environ['ORACLE_PASSWORD']
    path = directory(job_id)
    os.environ['CONSERVATIVE_TYPES'] = '1'
    os.environ['MIGRATION_BATCH_SIZE'] = str(job.get('batch_size',5000))
    os.environ['MIGRATION_LOAD_METHOD'] = job.get('load_method','copy')
    os.environ['PG_DBNAME'] = 'upload_' + job_id.replace('-', '')
    os.environ['ORACLE_TABLES'] = ''
    schema = 'UPL_' + job_id.replace('-', '')[:24].upper()
    admin = None
    created = False
    def emit(**event):
        def change(record):
            record['heartbeat'] = time.time()
            if event['event'] == 'step':
                moment = time.time()
                if record.get('phase_started'):
                    timings = record.setdefault('phase_seconds',{})
                    timings[record['phase']] = round(moment-record['phase_started'],3)
                record['phase_started'] = moment
                record['phase'] = event['key']
                keys = [s['key'] for s in record['steps']]
                record['step'] = keys.index(event['key']) + 1 if event['key'] in keys else record['step']
            elif event['event'] == 'warning':
                record['warnings'].append(event['message'])
            elif event['event'] == 'plan':
                record['table_order'] = event['tables']
                record['tables'] = {n: dict(rows=0, state='pending', seconds=None) for n in event['tables']}
            elif event['event'] in ('table_start', 'rows', 'table_done'):
                table = record['tables'][event['table']]
                table.update(rows=event.get('rows', table['rows']),
                             state='loaded' if event['event'] == 'table_done' else 'loading',
                             seconds=event.get('seconds'))
                record['rows_total'] = sum(t['rows'] for t in record['tables'].values())
            elif event['event'] == 'committed':
                record['committed'] = True
                for table in record['tables'].values():
                    table['state'] = 'committed'
            elif event['event']=='procedure_start':
                record['procedures'].append(dict(name=event['name'],status='translating'))
            elif event['event']=='procedure_done':
                entry=next((p for p in record['procedures'] if p['name']==event['name']),None)
                if entry is not None:
                    entry.update({k:v for k,v in event.items() if k!='event'})
            elif event['event']=='script_progress':
                record['script_progress']={'done':event['done'],'total':event['total']}
        store.update('job', job_id, change)
        store.event(job, event)
    try:
        free = shutil.disk_usage(path).free
        if free < 512 * 1024 * 1024:
            raise ValueError('Less than 512 MiB free for artifacts and verification scratch files.')
        store.update('job',job_id,lambda r:r.update(artifact_disk_free_bytes=free))
        import oracledb
        oracledb.defaults.fetch_decimals = True
        if job.get('upload_id'):
            emit(event='step', key='loading_script')
            source = store.get('upload', job['upload_id'], job['owner'])
            raw = (directory(source['id']) / 'original.sql').read_bytes()
            from progress_view import decode_upload
            text, note = decode_upload(raw)
            admin = oracledb.connect(user='sys', password=os.environ['ORACLE_PASSWORD'], dsn=os.environ['ORACLE_DSN'], mode=oracledb.AUTH_MODE_SYSDBA)
            admin.call_timeout = 120000
            import secrets
            password = secrets.token_hex(12)
            upload.create_scratch_schema(admin, schema, password)
            created = True
            with oracledb.connect(user=schema, password=password, dsn=os.environ['ORACLE_DSN']) as conn:
                conn.call_timeout = 120000
                load = upload.run_script(conn, text,progress=lambda done,total:emit(event='script_progress',done=done,total=total))
            store.update('job', job_id, lambda r: r.update(script=vars(load)))
            os.environ.update(ORACLE_USER=schema, ORACLE_PASSWORD=password, ORACLE_SCHEMA=schema)
            if note:
                emit(event='warning', message=note)
        else:
            os.environ.update(ORACLE_USER='MIG_READER', ORACLE_PASSWORD=os.environ['ORACLE_READER_PASSWORD'],
                              ORACLE_SCHEMA=job.get('source_schema', 'DEMO'))
        # Import only after setting the per-process source and fresh target.
        from read_schema import read_schema, read_unmigrated_objects
        from build_ir import build_ir
        from type_mapper import map_schema
        from migrate_data import get_oracle_connection, get_pg_connection, migrate_all_data
        from write_postgres_schema import write_schema_to_postgres
        from .verify import verify_database
        emit(event='step', key='reading_schema')
        raw_schema = read_schema()
        if not raw_schema:
            raise ValueError('Source has no accessible tables.')
        for table, details in raw_schema.items():
            from .objects import safe_name
            if not safe_name(table) or any(not safe_name(c['name']) for c in details['columns']):
                raise ValueError('Mixed-case, special or longer-than-63-byte identifiers require manual mapping.')
            for kind in ('unique_constraints','check_constraints'):
                if any(not safe_name(c['name']) for c in details.get(kind,[])):
                    raise ValueError('Constraint identifier requires manual mapping.')
        inventory = read_unmigrated_objects()
        # The table-only reader cannot see routine source or partitions. Inventory via
        # the provisioner, then read every business row with the restricted account.
        from config import ORACLE_SCHEMA
        if not admin:
            admin = oracledb.connect(user='sys',password=provision_password,
                dsn=os.environ['ORACLE_DSN'],mode=oracledb.AUTH_MODE_SYSDBA)
        with admin.cursor() as cur:
            cur.execute("SELECT object_type,object_name FROM dba_objects WHERE owner=:owner AND object_type NOT IN ('TABLE','INDEX','LOB','TABLE PARTITION','INDEX PARTITION') ORDER BY 1,2",owner=ORACLE_SCHEMA)
            for kind,name in cur:
                inventory.setdefault(kind,[])
                if name not in inventory[kind]:
                    inventory[kind].append(name)
            cur.execute('SELECT table_name FROM dba_part_tables WHERE owner=:owner ORDER BY table_name',owner=ORACLE_SCHEMA)
            inventory['partitioned tables']=[row[0] for row in cur]
            cur.execute("""SELECT index_name || ' on ' || table_name || ' (' || index_type || ')' FROM dba_indexes i
                WHERE owner=:owner AND index_type NOT IN ('LOB','IOT - TOP')
                AND NOT EXISTS (SELECT 1 FROM dba_constraints c WHERE c.owner=i.owner AND c.index_name=i.index_name)
                ORDER BY index_name""",owner=ORACLE_SCHEMA)
            inventory.pop('indexes (non-unique, not backing a constraint)',None)
            inventory['indexes']=[row[0] for row in cur]
        if 'inventory could not be read' in inventory:
            raise ValueError('Source object inventory could not be read; inspection is required.')
        emit(event='step', key='mapping_types')
        mapped = map_schema(build_ir(raw_schema, inventory))
        from .objects import discover,statements
        mapped.update(discover(admin,ORACLE_SCHEMA,mapped))
        supported={obj['name'].upper() for key in ('indexes','views') for obj in mapped.get(key,[])}
        for kind,names in list(inventory.items()):
            inventory[kind]=[name for name in names if str(name).split(' on ')[0] not in supported]
        mapped['not_migrated']=inventory
        overrides = job.get('mappings', {})
        issues = []
        for table in mapped['tables']:
            for column in table['columns']:
                key = table['name'] + '.' + column['name']
                if key in overrides:
                    column.update(pg_type=overrides[key], confidence='needs_review', note='Explicit reviewed mapping override')
                if column['pg_type'] is None:
                    issues.append(key + ': unsupported ' + column['source_type'])
                if 'TIMESTAMP' in column['source_type'] and any('(' + str(n) + ')' in column['source_type'] for n in (7, 8, 9)):
                    issues.append(key + ': PostgreSQL cannot preserve nanosecond precision')
                if column.get('oracle_default') and not column.get('pg_default'):
                    emit(event='warning', message=key + ': source default requires manual work')
            for check in table.get('check_constraints', []):
                if not check.get('pg_condition'):
                    emit(event='warning', message=table['name'] + ': check constraint needs manual work: ' + check['oracle_condition'])
        for kind, names in inventory.items():
            if names:
                emit(event='warning', message=f'Objects requiring manual review ({kind}): ' + ', '.join(map(str, names)))
        (path / 'mapped_schema.json').write_text(json.dumps(mapped, indent=2, default=str), encoding='utf-8')
        inspection = dict(mapped=mapped, blockers=issues, inventory=inventory,
                          source_consistency=('Read-only isolated scratch tables' if job.get('upload_id') else 'Common Oracle SCN; snapshot loss fails the job'),
                          procedures='Syntax translation is optional; behavioural equivalence is not certified.')
        profiles = {}
        with get_oracle_connection() as connection, connection.cursor() as cursor:
            for table, details in raw_schema.items():
                columns = [c for c in details['columns'] if c['type'] == 'NUMBER']
                if not columns:
                    continue
                cursor.execute('SELECT '+','.join('"'+c['name']+'"' for c in columns)+f' FROM "{ORACLE_SCHEMA}"."{table}" FETCH FIRST 1000 ROWS ONLY')
                rows = cursor.fetchall()
                for i,column in enumerate(columns):
                    values = [row[i] for row in rows if row[i] is not None]
                    profiles[table+'.'+column['name']] = dict(sampled_rows=len(rows),nulls=len(rows)-len(values),
                        minimum=str(min(values)) if values else None, maximum=str(max(values)) if values else None,
                        fractional_values=sum(v!=int(v) for v in values), scope='Bounded sample; type safety uses declared rules and full post-load verification')
        inspection['profiles'] = profiles
        store.update('job', job_id, lambda r: r.update(inspection=inspection))
        if job.get('inspect_only') or issues:
            store.update('job', job_id, lambda r: r.update(status='needs_review'))
            return
        if job.get('upload_id'):
            # Newly created Oracle segments can reject Flashback Query with ORA-01466.
            # Make this private source immutable instead; never fall back from a live snapshot.
            with get_oracle_connection() as conn,conn.cursor() as cur:
                for table in raw_schema:
                    cur.execute(f'ALTER TABLE "{table}" READ ONLY')
            os.environ.pop('ORACLE_SNAPSHOT_SCN',None)
        else:
            with get_oracle_connection() as conn, conn.cursor() as cur:
                cur.execute('SELECT DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER FROM dual')
                os.environ['ORACLE_SNAPSHOT_SCN'] = str(cur.fetchone()[0])
        emit(event='step', key='writing_postgres_schema')
        database = os.environ['PG_DBNAME']
        upload.create_pg_database(database)
        store.update('job', job_id, lambda r: r.update(target={'postgres_database': database},
            snapshot_scn=os.getenv('ORACLE_SNAPSHOT_SCN'),source_consistency=inspection['source_consistency']))
        with get_pg_connection() as conn:
            write_schema_to_postgres(mapped, conn)
            with conn.cursor() as cur:
                cur.execute('SELECT pg_current_wal_lsn()')
                wal_start = cur.fetchone()[0]
        emit(event='step', key='migrating_data')
        started = time.monotonic()
        summary = migrate_all_data(progress=lambda e: emit(**e))
        with get_pg_connection() as conn,conn.cursor() as cur:
            for statement in statements(mapped):
                cur.execute(statement)
            conn.commit()
        emit(event='step', key='verifying_data')
        verification = verify_database(mapped)
        if not verification['passed']:
            (path / 'verification.json').write_text(json.dumps(verification, indent=2, default=str), encoding='utf-8')
            raise ValueError('Exact data verification failed. Target is not ready.')
        functions = []
        if job.get('translate_procedures') and not job.get('upload_id'):
            emit(event='warning',message='The configured source reader accesses tables only. Supply routines in an upload for optional AI translation; live-source routines remain manual.')
        elif job.get('translate_procedures'):
            emit(event='warning', message='AI translation is syntax checked only. Run isolated behavioural tests before using routines.')
            functions = upload.translate_procedures(str(path), emit)
        procedure_evidence={'status':'not_tested','scope':'No behavioural test cases were supplied.'}
        if job.get('procedure_tests'):
            from compare_procedures import run_cases,choose_oracle_runner,PostgresRunner,format_report
            from read_schema import read_source_objects
            from dataclasses import asdict
            spec=job['procedure_tests']
            sources={obj['name'].upper():obj['source'] for obj in read_source_objects()}
            with get_oracle_connection() as source,source.cursor() as cursor:
                for table in raw_schema:
                    cursor.execute(f'ALTER TABLE "{table}" READ WRITE')
            try:
                with get_oracle_connection() as source,get_pg_connection() as target:
                    # No COMMIT or autonomous routines run against either side.
                    results=run_cases(spec['cases'],choose_oracle_runner(False,source,None,get_oracle_connection),
                        PostgresRunner(target),sources,False,min(5,max(0,float(spec.get('time_tolerance_seconds',0)))))
            finally:
                with get_oracle_connection() as source,source.cursor() as cursor:
                    for table in raw_schema:
                        cursor.execute(f'ALTER TABLE "{table}" READ ONLY')
            procedure_evidence={'status':'passed' if results and all(r.status=='SAME' for r in results) else 'needs_review',
                'cases':[asdict(r) for r in results], 'scope':'Only supplied calls; rollback-isolated upload copies. COMMIT/autonomous routines are skipped.'}
            (path/'procedure-tests.txt').write_text(format_report(results),encoding='utf-8')
            if procedure_evidence['status']!='passed':
                emit(event='warning',message='Routine behavioural tests require review. Read procedure-tests.txt; table data verification is separate.')
        verification['procedures']=procedure_evidence
        emit(event='step', key='exporting_files')
        with get_pg_connection() as conn:
            upload.export_dump(conn, mapped, functions, str(path / 'converted_postgresql.sql'))
        emit(event='step', key='restoring_export')
        restore = 'upload_' + job_id.replace('-', '') + 'a'
        upload.create_pg_database(restore)
        try:
            env = dict(os.environ, PGPASSWORD=os.environ['PG_PASSWORD'])
            result = subprocess.run(['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-h', os.environ['PG_HOST'],
                '-U', os.environ['PG_USER'], '-d', restore, '-f', str(path / 'converted_postgresql.sql')],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=3600)
            if result.returncode:
                raise ValueError('Export restore failed: ' + result.stderr[-1000:])
            verification['export_restore'] = verify_database(mapped, restore)
            if not verification['export_restore']['passed']:
                raise ValueError('Restored export values do not match the source.')
        finally:
            upload.drop_pg_database(restore)
        verification['scope'] = 'All supported table values, duplicate multiplicities, nulls and numeric aggregates. Routines/manual objects are not certified.'
        (path / 'verification.json').write_text(json.dumps(verification, indent=2, default=str), encoding='utf-8')
        current = store.get('job', job_id)
        with get_pg_connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT pg_database_size(current_database()),pg_wal_lsn_diff(pg_current_wal_lsn(),%s)',(wal_start,))
            target_size,wal_bytes = cur.fetchone()
        emit(event='step',key='finalizing_files')
        store.update('job', job_id, lambda r: r.update(status='completed_with_warnings' if current['warnings'] else 'completed',
            summary=summary, verification=verification, target_bytes=target_size, cluster_wal_bytes=int(wal_bytes),
            duration_seconds=round(time.monotonic()-started, 3)))
    finally:
        if admin:
            try:
                if created:
                    upload.drop_scratch_schema(admin, schema)
            finally:
                admin.close()


if __name__ == '__main__':
    run(sys.argv[1])
