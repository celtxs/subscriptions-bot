from __future__ import annotations
import argparse, hashlib, sqlite3
from pathlib import Path
parser=argparse.ArgumentParser(description="Create consistent SQLite backup")
parser.add_argument("source"); parser.add_argument("destination"); args=parser.parse_args()
source=sqlite3.connect(args.source); destination=sqlite3.connect(args.destination)
with destination: source.backup(destination)
destination.close(); source.close()
check=sqlite3.connect(args.destination).execute("PRAGMA integrity_check").fetchone()[0]
if check != "ok": raise SystemExit("integrity check failed")
print(hashlib.sha256(Path(args.destination).read_bytes()).hexdigest())
