from __future__ import annotations
import argparse, sqlite3
from pathlib import Path
parser=argparse.ArgumentParser(description="Validate and restore SQLite copy")
parser.add_argument("backup"); parser.add_argument("destination"); args=parser.parse_args()
source=sqlite3.connect(args.backup); target=sqlite3.connect(args.destination)
with target: source.backup(target)
target.close(); source.close()
check=sqlite3.connect(args.destination).execute("PRAGMA integrity_check").fetchone()[0]
if check != "ok": raise SystemExit("restored database integrity check failed")
print("restored")
