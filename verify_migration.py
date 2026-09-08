import psycopg2

connection = psycopg2.connect(
    host="localhost",
    port=5432,
    user="postgres",
    password="YourPgPassword123",
    dbname="modernized_db"
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