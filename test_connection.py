import oracledb

# These match what we used when starting the Docker container
connection = oracledb.connect(
    user="system",
    password="YourPassword123",
    dsn="localhost:1521/XEPDB1"
)

print("Connected successfully!")

cursor = connection.cursor()
cursor.execute("SELECT 'Hello from Oracle' FROM dual")
result = cursor.fetchone()
print(result[0])

cursor.close()
connection.close()