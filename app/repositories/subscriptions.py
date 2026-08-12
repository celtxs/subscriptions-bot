from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.domain.calendar import Recurrence, add_recurrence


class StaleRecordError(RuntimeError):
    pass


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("aware datetime required")
    return value.astimezone(timezone.utc).isoformat()


def _reminders(con: sqlite3.Connection, subscription_id: str, term_version: int, end_at: datetime, now: datetime) -> None:
    for kind, hours in (("BEFORE_48H", 48), ("BEFORE_24H", 24)):
        con.execute("INSERT INTO reminder_deliveries(id,subscription_id,term_version,kind,scheduled_at_utc,status,attempt_count,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,'pending',0,?,?)", (str(uuid.uuid4()), subscription_id, term_version, kind, utc_iso(end_at-timedelta(hours=hours)), utc_iso(now), utc_iso(now)))


class SubscriptionRepository:
    def __init__(self, con: sqlite3.Connection) -> None: self.con = con

    def _audit(self, event: str, subscription_id: str | None, now: datetime, **metadata: Any) -> None:
        self.con.execute("INSERT INTO audit_events(id,subscription_id,event,created_at_utc,metadata_json) VALUES (?,?,?,?,?)", (str(uuid.uuid4()), subscription_id, event, utc_iso(now), json.dumps(metadata, separators=(",", ":"))))

    def create(self, *, owner_id: int, name: str, category: str, end_at: datetime, recurrence: Recurrence | None, secret_payload: tuple[bytes, bytes, int] | None, now: datetime, replaced_from_id: str | None = None, cost_minor: int | None = None, currency: str | None = None, service_url: str | None = None, note: str | None = None, subscription_id: str | None = None) -> str:
        if not name.strip() or category not in {"AI", "SERVER", "OTHER"} or (cost_minor is not None and cost_minor < 0) or (currency and (len(currency) != 3 or not currency.isalpha())):
            raise ValueError("invalid subscription fields")
        subscription_id = subscription_id or str(uuid.uuid4()); ciphertext, nonce, key_version = secret_payload or (None, None, None)
        with self.con:
            self.con.execute("INSERT INTO subscriptions(id,owner_telegram_id,name,category,status,end_at_utc,recurrence_value,recurrence_unit,cost_minor,currency,service_url,note,secret_ciphertext,secret_nonce,encryption_key_version,replaced_from_id,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (subscription_id, owner_id, name.strip(), category, "active", utc_iso(end_at), recurrence.value if recurrence else None, recurrence.unit if recurrence else "NONE", cost_minor, currency.upper() if currency else None, service_url, note, ciphertext, nonce, key_version, replaced_from_id, utc_iso(now), utc_iso(now)))
            _reminders(self.con, subscription_id, 1, end_at, now); self._audit("subscription_created", subscription_id, now)
        return subscription_id

    def get(self, subscription_id: str, owner_id: int) -> sqlite3.Row | None: return self.con.execute("SELECT * FROM subscriptions WHERE id=? AND owner_telegram_id=?", (subscription_id, owner_id)).fetchone()
    def list(self, owner_id: int, status: str, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]: return self.con.execute("SELECT * FROM subscriptions WHERE owner_telegram_id=? AND status=? ORDER BY end_at_utc LIMIT ? OFFSET ?", (owner_id, status, limit, offset)).fetchall()

    def update(self, subscription_id: str, owner_id: int, expected_record_version: int, changes: dict[str, Any], now: datetime) -> None:
        allowed = {"name", "category", "recurrence_value", "recurrence_unit", "cost_minor", "currency", "service_url", "note", "secret_ciphertext", "secret_nonce", "encryption_key_version"}
        if not changes or set(changes) - allowed: raise ValueError("invalid update fields")
        if "name" in changes and (not str(changes["name"]).strip() or len(str(changes["name"])) > 160): raise ValueError("invalid name")
        if "category" in changes and changes["category"] not in {"AI", "SERVER", "OTHER"}: raise ValueError("invalid category")
        if "cost_minor" in changes and changes["cost_minor"] is not None and changes["cost_minor"] < 0: raise ValueError("invalid cost")
        if "currency" in changes and changes["currency"] and (len(changes["currency"]) != 3 or not changes["currency"].isalpha()): raise ValueError("invalid currency")
        if "recurrence_value" in changes or "recurrence_unit" in changes:
            unit = changes.get("recurrence_unit")
            value = changes.get("recurrence_value")
            if unit not in {"DAYS", "MONTHS", "YEARS", "NONE"} or (unit == "NONE" and value is not None) or (unit != "NONE" and (not isinstance(value, int) or value <= 0)):
                raise ValueError("invalid recurrence")
        if "currency" in changes and changes["currency"]: changes["currency"] = changes["currency"].upper()
        assignments = ",".join(f"{field}=?" for field in changes) + ",record_version=record_version+1,updated_at_utc=?"
        with self.con:
            changed = self.con.execute(f"UPDATE subscriptions SET {assignments} WHERE id=? AND owner_telegram_id=? AND record_version=?", (*changes.values(), utc_iso(now), subscription_id, owner_id, expected_record_version)).rowcount
            if changed != 1: raise StaleRecordError("card is stale")
            self._audit("subscription_updated", subscription_id, now, fields=sorted(changes))

    def change_end(self, subscription_id: str, owner_id: int, expected_term: int, expected_record_version: int, end_at: datetime, now: datetime) -> None:
        if end_at <= now: raise ValueError("end date must be future")
        with self.con:
            changed=self.con.execute("UPDATE subscriptions SET end_at_utc=?,term_version=term_version+1,record_version=record_version+1,updated_at_utc=? WHERE id=? AND owner_telegram_id=? AND term_version=? AND record_version=?", (utc_iso(end_at),utc_iso(now),subscription_id,owner_id,expected_term,expected_record_version)).rowcount
            if changed != 1: raise StaleRecordError("card is stale")
            self.con.execute("UPDATE reminder_deliveries SET status='cancelled',updated_at_utc=? WHERE subscription_id=? AND term_version=? AND status IN ('pending','retryable','claimed')",(utc_iso(now),subscription_id,expected_term)); _reminders(self.con,subscription_id,expected_term+1,end_at,now); self._audit("subscription_updated",subscription_id,now,term_version=expected_term+1)

    def renew(self, subscription_id: str, owner_id: int, expected_term_version: int, recurrence: Recurrence, now: datetime) -> datetime:
        row=self.get(subscription_id,owner_id)
        if not row or row["term_version"] != expected_term_version: raise StaleRecordError("card is stale")
        new_end=add_recurrence(datetime.fromisoformat(row["end_at_utc"]), recurrence)
        if new_end <= now: raise ValueError("chosen recurrence does not move end date into the future")
        with self.con:
            changed=self.con.execute("UPDATE subscriptions SET end_at_utc=?,recurrence_value=?,recurrence_unit=?,term_version=term_version+1,record_version=record_version+1,status='active',deactivation_reason=NULL,deactivated_at_utc=NULL,updated_at_utc=? WHERE id=? AND owner_telegram_id=? AND term_version=?",(utc_iso(new_end),recurrence.value,recurrence.unit,utc_iso(now),subscription_id,owner_id,expected_term_version)).rowcount
            if changed != 1: raise StaleRecordError("card is stale")
            self.con.execute("UPDATE reminder_deliveries SET status='cancelled',updated_at_utc=? WHERE subscription_id=? AND term_version=? AND status IN ('pending','retryable','claimed')",(utc_iso(now),subscription_id,expected_term_version)); _reminders(self.con,subscription_id,expected_term_version+1,new_end,now); self._audit("subscription_renewed",subscription_id,now,term_version=expected_term_version+1)
        return new_end

    def deactivate(self, subscription_id: str, owner_id: int, expected_record_version: int, reason: str, now: datetime) -> bool:
        with self.con:
            row=self.get(subscription_id,owner_id)
            if not row or row["status"] == "inactive": return False
            if row["record_version"] != expected_record_version: raise StaleRecordError("card is stale")
            self.con.execute("UPDATE subscriptions SET status='inactive',deactivation_reason=?,deactivated_at_utc=?,record_version=record_version+1,updated_at_utc=? WHERE id=?",(reason,utc_iso(now),utc_iso(now),subscription_id)); self.con.execute("UPDATE reminder_deliveries SET status='cancelled',updated_at_utc=? WHERE subscription_id=? AND status IN ('pending','retryable','claimed')",(utc_iso(now),subscription_id)); self._audit("subscription_deactivated",subscription_id,now,reason=reason)
        return True

    def delete(self, subscription_id: str, owner_id: int, now: datetime) -> bool:
        with self.con:
            if not self.get(subscription_id, owner_id): return False
            if self.con.execute("SELECT 1 FROM subscriptions WHERE replaced_from_id=? LIMIT 1", (subscription_id,)).fetchone(): return False
            self.con.execute("DELETE FROM subscriptions WHERE id=? AND owner_telegram_id=?",(subscription_id,owner_id)); self._audit("subscription_deleted",subscription_id,now)
        return True

    def replace(self, old_id: str, owner_id: int, expected_record_version: int, *, name: str, category: str, end_at: datetime, recurrence: Recurrence | None, secret_payload: tuple[bytes, bytes, int] | None, now: datetime) -> str:
        with self.con:
            old=self.get(old_id,owner_id)
            if not old or old["status"] != "active" or old["record_version"] != expected_record_version: raise StaleRecordError("card is stale or replacement already completed")
            if self.con.execute("UPDATE subscriptions SET status='inactive',deactivation_reason='replaced',deactivated_at_utc=?,record_version=record_version+1,updated_at_utc=? WHERE id=? AND status='active' AND record_version=?",(utc_iso(now),utc_iso(now),old_id,expected_record_version)).rowcount != 1: raise StaleRecordError("card is stale")
            self.con.execute("UPDATE reminder_deliveries SET status='cancelled',updated_at_utc=? WHERE subscription_id=? AND status IN ('pending','retryable','claimed')",(utc_iso(now),old_id)); new_id=self.create(owner_id=owner_id,name=name,category=category,end_at=end_at,recurrence=recurrence,secret_payload=secret_payload,now=now,replaced_from_id=old_id); self._audit("subscription_replaced",old_id,now,replacement_id=new_id)
        return new_id

    def expire_and_cancel(self, now: datetime) -> None:
        with self.con:
            self.con.execute("UPDATE subscriptions SET status='inactive',deactivation_reason='expired',deactivated_at_utc=?,updated_at_utc=? WHERE status='active' AND end_at_utc<=?",(utc_iso(now),utc_iso(now),utc_iso(now))); self.con.execute("UPDATE reminder_deliveries SET status='cancelled',updated_at_utc=? WHERE status IN ('pending','retryable') AND subscription_id IN (SELECT id FROM subscriptions WHERE status='inactive')",(utc_iso(now),))

    def coalesce_due(self, now: datetime) -> None:
        with self.con:
            self.con.execute("UPDATE reminder_deliveries SET status='cancelled',last_error_code='SUPERSEDED_BY_24H',updated_at_utc=? WHERE kind='BEFORE_48H' AND status IN ('pending','retryable') AND scheduled_at_utc<=? AND subscription_id IN (SELECT subscription_id FROM reminder_deliveries WHERE kind='BEFORE_24H' AND status IN ('pending','retryable') AND scheduled_at_utc<=?)", (utc_iso(now), utc_iso(now), utc_iso(now)))

    def claim_one_due(self, now: datetime, lease_seconds: int, scheduler_holder: str | None = None) -> sqlite3.Row | None:
        if scheduler_holder is not None and not self.scheduler_lease_owned(scheduler_holder, now):
            return None
        row=self.con.execute("SELECT r.* FROM reminder_deliveries r JOIN subscriptions s ON s.id=r.subscription_id WHERE r.status IN ('pending','retryable') AND r.scheduled_at_utc<=? AND s.status='active' AND s.term_version=r.term_version ORDER BY CASE r.kind WHEN 'BEFORE_24H' THEN 0 ELSE 1 END, r.scheduled_at_utc LIMIT 1",(utc_iso(now),)).fetchone()
        if not row: return None
        if scheduler_holder is None:
            claim_sql = "UPDATE reminder_deliveries SET status='claimed',claimed_until_utc=?,attempt_count=attempt_count+1,updated_at_utc=? WHERE id=? AND status IN ('pending','retryable')"
            claim_params = (utc_iso(now+timedelta(seconds=lease_seconds)),utc_iso(now),row["id"])
        else:
            claim_sql = "UPDATE reminder_deliveries SET status='claimed',claimed_until_utc=?,attempt_count=attempt_count+1,updated_at_utc=? WHERE id=? AND status IN ('pending','retryable') AND EXISTS (SELECT 1 FROM scheduler_lease WHERE name='reminders' AND holder_id=? AND expires_at_utc>?)"
            claim_params = (utc_iso(now+timedelta(seconds=lease_seconds)),utc_iso(now),row["id"],scheduler_holder,utc_iso(now))
        with self.con: changed=self.con.execute(claim_sql,claim_params).rowcount
        return self.con.execute("SELECT * FROM reminder_deliveries WHERE id=?",(row["id"],)).fetchone() if changed else None

    def recover_expired_claims(self, now: datetime) -> None:
        with self.con: self.con.execute("UPDATE reminder_deliveries SET status='unknown',last_error_code='CLAIM_LEASE_EXPIRED',updated_at_utc=? WHERE status='claimed' AND claimed_until_utc<?",(utc_iso(now),utc_iso(now)))
    def mark_sent(self, reminder_id: str, message_id: int, now: datetime) -> None:
        with self.con: self.con.execute("UPDATE reminder_deliveries SET status='sent',sent_at_utc=?,telegram_message_id=?,claimed_until_utc=NULL,updated_at_utc=? WHERE id=? AND status='claimed'",(utc_iso(now),message_id,utc_iso(now),reminder_id))
    def mark_retryable(self, reminder_id: str, error_code: str, retry_at: datetime, now: datetime) -> None:
        with self.con: self.con.execute("UPDATE reminder_deliveries SET status='retryable',scheduled_at_utc=?,claimed_until_utc=NULL,last_error_code=?,updated_at_utc=? WHERE id=? AND status='claimed'",(utc_iso(retry_at),error_code,utc_iso(now),reminder_id))
    def mark_failed(self, reminder_id: str, error_code: str, now: datetime) -> None:
        with self.con: self.con.execute("UPDATE reminder_deliveries SET status='failed',claimed_until_utc=NULL,last_error_code=?,updated_at_utc=? WHERE id=? AND status='claimed'",(error_code,utc_iso(now),reminder_id))
    def mark_unknown(self, reminder_id: str, error_code: str, now: datetime) -> None:
        with self.con: self.con.execute("UPDATE reminder_deliveries SET status='unknown',claimed_until_utc=NULL,last_error_code=?,updated_at_utc=? WHERE id=? AND status='claimed'",(error_code,utc_iso(now),reminder_id))
    def set_setting(self, key: str, value: str, now: datetime) -> None:
        with self.con:
            self.con.execute("INSERT INTO settings(key,value,updated_at_utc) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_utc=excluded.updated_at_utc", (key, value, utc_iso(now)))

    def get_setting(self, key: str) -> sqlite3.Row | None:
        return self.con.execute("SELECT value,updated_at_utc FROM settings WHERE key=?", (key,)).fetchone()

    def acquire_scheduler_lease(self, holder: str, now: datetime, seconds: int) -> bool:
        with self.con:
            self.con.execute("INSERT OR IGNORE INTO scheduler_lease(name,holder_id,expires_at_utc,heartbeat_at_utc) VALUES ('reminders',NULL,NULL,NULL)"); changed=self.con.execute("UPDATE scheduler_lease SET holder_id=?,expires_at_utc=?,heartbeat_at_utc=? WHERE name='reminders' AND (holder_id=? OR expires_at_utc IS NULL OR expires_at_utc<?)",(holder,utc_iso(now+timedelta(seconds=seconds)),utc_iso(now),holder,utc_iso(now))).rowcount
        return changed == 1

    def renew_scheduler_lease(self, holder: str, now: datetime, seconds: int) -> bool:
        with self.con:
            changed = self.con.execute(
                "UPDATE scheduler_lease SET expires_at_utc=?,heartbeat_at_utc=? "
                "WHERE name='reminders' AND holder_id=? AND expires_at_utc>?",
                (utc_iso(now + timedelta(seconds=seconds)), utc_iso(now), holder, utc_iso(now)),
            ).rowcount
        return changed == 1

    def scheduler_lease_owned(self, holder: str, now: datetime) -> bool:
        row = self.con.execute("SELECT 1 FROM scheduler_lease WHERE name='reminders' AND holder_id=? AND expires_at_utc>?", (holder, utc_iso(now))).fetchone()
        return row is not None
