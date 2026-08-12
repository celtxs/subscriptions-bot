from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone

NON_SECRET_SESSION_KEYS = frozenset({
    "name", "category", "date", "time", "end_at", "recurrence_value",
    "recurrence_unit", "cost", "currency", "url", "note",
    "subscription_id", "record_version", "field", "old_subscription_id",
})

class DialogSessionRepository:
 def __init__(self,con): self.con=con
 def save(self,owner_id,flow,step,payload,now,ttl=3600):
  if not isinstance(payload, dict) or not set(payload) <= NON_SECRET_SESSION_KEYS: raise ValueError("dialog session contains unsupported fields")
  with self.con:self.con.execute("INSERT INTO dialog_sessions(owner_telegram_id,flow_name,current_step,non_secret_payload,created_at,updated_at,expires_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner_telegram_id) DO UPDATE SET flow_name=excluded.flow_name,current_step=excluded.current_step,non_secret_payload=excluded.non_secret_payload,updated_at=excluded.updated_at,expires_at=excluded.expires_at",(owner_id,flow,step,json.dumps(payload),now.isoformat(),now.isoformat(),(now+timedelta(seconds=ttl)).isoformat()))
 def load(self,owner_id,now):
  row=self.con.execute("SELECT * FROM dialog_sessions WHERE owner_telegram_id=? AND expires_at>?",(owner_id,now.isoformat())).fetchone(); return row
 def clear(self,owner_id):
  with self.con:self.con.execute("DELETE FROM dialog_sessions WHERE owner_telegram_id=?",(owner_id,))
