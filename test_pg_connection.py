import psycopg2

from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME

connection = psycopg2.connect(
    host=PG_HOST,
    port=PG_PORT,
    user=PG_USER,
    password=PG_PASSWORD,
    dbname=PG_DBNAME,
)

print("Connected to PostgreSQL successfully!")

cursor = connection.cursor()
cursor.execute("SELECT version();")
print(cursor.fetchone()[0])

cursor.close()
connection.close()