import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from app.db.connection import connect
from app.domain.crypto import EncryptionService
from app.repositories.subscriptions import StaleRecordError, SubscriptionRepository

class EditSecretTests(unittest.TestCase):
 def test_edit_non_term_fields_and_secret_are_versioned(self):
  with TemporaryDirectory() as tmp:
   con=connect(Path(tmp)/'x.db'); repo=SubscriptionRepository(con); now=datetime(2026,1,1,tzinfo=timezone.utc)
   sid=repo.create(owner_id=1,name='A',category='AI',end_at=now+timedelta(days=3),recurrence=None,secret_payload=None,now=now)
   row=repo.get(sid,1); key=EncryptionService(b'x'*32); cipher,nonce=key.encrypt({'login':'u','password':'p','api_key':None},f'subscription:{sid}:v1'.encode())
   repo.update(sid,1,row['record_version'],{'cost_minor':19900,'currency':'usd','note':'n','secret_ciphertext':cipher,'secret_nonce':nonce,'encryption_key_version':1},now)
   row=repo.get(sid,1); self.assertEqual((row['cost_minor'],row['currency'],row['note'],row['term_version']),(19900,'USD','n',1)); self.assertEqual(key.decrypt(row['secret_ciphertext'],row['secret_nonce'],f'subscription:{sid}:v1'.encode())['password'],'p')
   with self.assertRaises(StaleRecordError): repo.update(sid,1,1,{'note':'x'},now)
if __name__=='__main__': unittest.main()
