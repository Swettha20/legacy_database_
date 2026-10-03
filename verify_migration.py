import psycopg2

from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME

connection = psycopg2.connect(
    host=PG_HOST,
    port=PG_PORT,
    user=PG_USER,
    password=PG_PASSWORD,
    dbname=PG_DBNAME,
)
cursor = connection.cursor()

cursor.execute('SELECT id, name, email FROM "members"')
print("members:", cursor.fetchall())

cursor.execute('SELECT id, title, available_copies FROM "books"')
print("books:", cursor.fetchall())

cursor.execute('SELECT id, member_id, book_id, due_date FROM "loans"')
print("loans:", cursor.fetchall())

cursor.close()
connection.close()