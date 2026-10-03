import oracledb

from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN

# Connection details come from .env (see .env.example), not from this file
connection = oracledb.connect(
    user=ORACLE_USER,
    password=ORACLE_PASSWORD,
    dsn=ORACLE_DSN,
)

print("Connected successfully!")

cursor = connection.cursor()
cursor.execute("SELECT 'Hello from Oracle' FROM dual")
result = cursor.fetchone()
print(result[0])

cursor.close()
connection.close()