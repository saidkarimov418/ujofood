import sqlite3
from config import DB

conn = sqlite3.connect(DB, check_same_thread=False)
cursor = conn.cursor()

def init_db():
    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS users (...);
    CREATE TABLE IF NOT EXISTS orders (...);
    """)
    conn.commit()
