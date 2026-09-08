import psycopg2

connection = psycopg2.connect(
    host="localhost",
    port=5432,
    user="postgres",
    password="YourPgPassword123",
    dbname="modernized_db"
)

print("Connected to PostgreSQL successfully!")

cursor = connection.cursor()
cursor.execute("SELECT version();")
print(cursor.fetchone()[0])

cursor.close()
connection.close()