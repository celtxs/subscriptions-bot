from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

SAFE_FIELDS = ("subscription_id", "term_version", "record_version", "error_code", "duration_ms", "update_id_hash")

class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname, "event": getattr(record, "event", record.getMessage())}
        for key in SAFE_FIELDS:
            if hasattr(record, key): payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)

def configure(level: str):
    handler=logging.StreamHandler(sys.stdout); handler.setFormatter(JsonFormatter())
    root=logging.getLogger(); root.handlers[:]=[handler]; root.setLevel(level)
