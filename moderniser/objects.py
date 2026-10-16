"""Conservative ordinary-index and single-table projection-view support."""
import re


def safe_name(value):
    return bool(re.fullmatch(r'[A-Z][A-Z0-9_$#]{0,62}',value))


def discover(admin, owner, mapped):
    tables = {table['name']:table for table in mapped['tables']}
    indexes, views = [], []
    with admin.cursor() as cur:
        cur.execute("""SELECT index_name,table_name,uniqueness FROM dba_indexes i
            WHERE owner=:owner AND index_type='NORMAL'
            AND NOT EXISTS (SELECT 1 FROM dba_constraints c WHERE c.owner=i.owner AND c.index_name=i.index_name)
            ORDER BY index_name""",owner=owner)
        for name,table,uniqueness in cur.fetchall():
            if not safe_name(name) or table.lower() not in tables:
                continue
            cur.execute('SELECT column_name,descend FROM dba_ind_columns WHERE index_owner=:owner AND index_name=:name ORDER BY column_position',owner=owner,name=name)
            columns = cur.fetchall()
            known = {c['name'] for c in tables[table.lower()]['columns']}
            if columns and all(safe_name(c) and c.lower() in known and direction in ('ASC','DESC') for c,direction in columns):
                indexes.append(dict(name=name.lower(),table=table.lower(),unique=uniqueness=='UNIQUE',columns=[dict(name=c.lower(),direction=d) for c,d in columns]))
        cur.execute('SELECT view_name,text FROM dba_views WHERE owner=:owner ORDER BY view_name',owner=owner)
        for name,text in cur.fetchall():
            match = re.fullmatch(r'\s*SELECT\s+(\*|[A-Z0-9_$#",\s]+)\s+FROM\s+"?([A-Z][A-Z0-9_$#]*)"?\s*',text or '',re.I)
            if not match or not safe_name(name) or match[2].lower() not in tables:
                continue
            table = match[2].lower()
            known = {c['name'] for c in tables[table]['columns']}
            columns = list(known) if match[1]=='*' else [c.strip().strip('"').lower() for c in match[1].split(',')]
            if columns and all(c in known for c in columns):
                # Preserve source order for SELECT *.
                if match[1]=='*':
                    columns=[c['name'] for c in tables[table]['columns']]
                views.append(dict(name=name.lower(),table=table,columns=columns))
    return dict(indexes=indexes,views=views)


def statements(mapped):
    result = []
    for index in mapped.get('indexes',[]):
        columns=','.join('"'+c['name']+'" '+c['direction'] for c in index['columns'])
        unique='UNIQUE ' if index.get('unique') else ''
        result.append(f'CREATE {unique}INDEX "{index["name"]}" ON "{index["table"]}" ({columns});')
    for view in mapped.get('views',[]):
        columns=','.join('"'+c+'"' for c in view['columns'])
        result.append(f'CREATE VIEW "{view["name"]}" AS SELECT {columns} FROM "{view["table"]}";')
    return result
