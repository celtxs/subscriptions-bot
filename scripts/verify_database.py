from __future__ import annotations
import argparse, sqlite3
parser=argparse.ArgumentParser(); parser.add_argument("db_path"); args=parser.parse_args()
con=sqlite3.connect(args.db_path)
result=con.execute("PRAGMA integrity_check").fetchone()[0]
print(result)
raise SystemExit(0 if result == "ok" else 1)
