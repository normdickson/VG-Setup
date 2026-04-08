"""
test_db.py — Quick Latitude SQL Server connection test.

Usage:
    # With env var:
    set LATITUDE_CONNECTION_STRING=Driver={ODBC Driver 17 for SQL Server};Server=VELOCITY-LAT01\SQLEXPRESS;Database=LatidataSQL;UID=sa;PWD=L9gd%!c
    python test_db.py

    # Or just run it directly (connection string is hardcoded below for testing only)
"""

import sys
import os

# ---------------------------------------------------------------------------
# Edit this if you're not using the env var
# ---------------------------------------------------------------------------
CONN_STR = os.getenv(
    "LATITUDE_CONNECTION_STRING",
    r"Driver={ODBC Driver 17 for SQL Server};"
    r"Server=VELOCITY-LAT01\SQLEXPRESS;"
    r"Database=LatidataSQL;"
    r"UID=sa;"
    r"PWD=L9gd%!c"
)

try:
    import pyodbc
except ImportError:
    print("ERROR: pyodbc is not installed. Run:  pip install pyodbc")
    sys.exit(1)


def run():
    print(f"Connecting to: {CONN_STR.split(';')[1]}")   # print Server only
    print("-" * 50)

    try:
        conn = pyodbc.connect(CONN_STR, timeout=10)
        print("✓  Connection established")
    except pyodbc.OperationalError as e:
        print(f"✗  Connection failed: {e}")
        sys.exit(1)

    cursor = conn.cursor()

    # 1. Verify database
    cursor.execute("SELECT DB_NAME()")
    print(f"✓  Database : {cursor.fetchone()[0]}")

    # 2. Row count
    cursor.execute("SELECT COUNT(*) FROM dbo.tblJobs")
    count = cursor.fetchone()[0]
    print(f"✓  tblJobs  : {count:,} rows")

    # 3. Sample rows
    cursor.execute("""
        SELECT TOP 5
            [Job Number],
            [Job Date],
            Client,
            Locality,
            [Work Status]
        FROM dbo.tblJobs
        ORDER BY [Job Date] DESC
    """)
    rows = cursor.fetchall()
    print(f"\n  5 most recent jobs:")
    print(f"  {'Job Number':<15} {'Date':<14} {'Client':<12} {'Location':<20} {'Status'}")
    print(f"  {'-'*14} {'-'*13} {'-'*11} {'-'*19} {'-'*10}")
    for r in rows:
        date_str = r[1].strftime("%Y-%m-%d") if r[1] else "N/A"
        print(f"  {str(r[0] or ''):<15} {date_str:<14} {str(r[2] or ''):<12} {str(r[3] or ''):<20} {r[4] or ''}")

    # 4. Check txtJobName and JobType exist
    cursor.execute("""
        SELECT TOP 1 txtJobName, JobType FROM dbo.tblJobs
    """)
    row = cursor.fetchone()
    if row is not None:
        print(f"\n✓  txtJobName / JobType columns present")
    else:
        print(f"\n⚠  txtJobName / JobType columns present but no data returned")

    # 5. tblClient check
    try:
        cursor.execute("SELECT COUNT(*) FROM dbo.tblClient")
        client_count = cursor.fetchone()[0]
        print(f"✓  tblClient : {client_count:,} rows")
    except pyodbc.Error as e:
        print(f"⚠  tblClient query failed: {e}")

    # 6. Distinct work statuses
    cursor.execute("""
        SELECT DISTINCT [Work Status]
        FROM dbo.tblJobs
        WHERE [Work Status] IS NOT NULL
        ORDER BY [Work Status]
    """)
    statuses = [r[0] for r in cursor.fetchall()]
    print(f"\n  Work Status values: {statuses}")

    conn.close()
    print("\n✓  All checks passed — DB is ready.")


if __name__ == "__main__":
    run()
