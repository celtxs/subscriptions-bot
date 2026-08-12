import io
import json
import logging
import unittest
from app.logging import JsonFormatter

class LoggingTests(unittest.TestCase):
 def test_formatter_allowlists_fields_and_excludes_secret_extra(self):
  record=logging.LogRecord('x',logging.INFO,'',0,'event',(),None); record.event='event'; record.subscription_id='id'; record.password='must-not-log'; record.bot_token='must-not-log'
  payload=json.loads(JsonFormatter().format(record)); self.assertEqual(payload['subscription_id'],'id'); self.assertNotIn('password',payload); self.assertNotIn('bot_token',payload)
if __name__=='__main__':unittest.main()
