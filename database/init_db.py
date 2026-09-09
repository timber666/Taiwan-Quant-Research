import sqlite3

# connect to database file
conn = sqlite3.connect("database/quant.db")

# read schema.sql , create table
with open("database/schema.sql") as f:
    conn.executescript(f.read())
    
# insert fake data
conn.execute("INSERT INTO stocks VALUES ('2330', '台積電', '半導體')")
conn.execute("INSERT INTO prices VALUES ('2026-09-08','2330',900,910,895,905,30000000)")
conn.commit()

for row in conn.execute("SELECT * FROM prices"):
    print(row)

conn.close()