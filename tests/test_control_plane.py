"""Correctness tests for exact verification and authenticated API contracts."""
import datetime as dt
import decimal
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.testclient import TestClient
from moderniser.verify import canonical,row_hash,compare_cursors
from moderniser.service import app,validate_mappings,owned

class Cursor:
 def __init__(self,rows):self.rows=iter(rows)
 def fetchmany(self,n):
  result=[]
  for _ in range(n):
   try:result.append(next(self.rows))
   except StopIteration:break
  return result

class Verification(unittest.TestCase):
 def test_decimal_does_not_round_at_default_context_precision(self):
  value=decimal.Decimal('12345678901234567890123456789012345678.12')
  self.assertEqual(canonical(value),b'D12345678901234567890123456789012345678.12')
  self.assertEqual(canonical(decimal.Decimal('2.00')),canonical(2))
 def test_null_empty_and_length_framing_are_distinct(self):
  self.assertNotEqual(row_hash((None,'')),row_hash(('',None)))
  self.assertNotEqual(row_hash(('a','bc')),row_hash(('ab','c')))
 def test_unicode_binary_and_timezone(self):
  self.assertEqual(canonical(memoryview(b'\x00\xff')),canonical(b'\x00\xff'))
  self.assertNotEqual(canonical('é'),canonical('e'))
  self.assertEqual(canonical(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)),canonical(dt.datetime(2026,1,1,5,30,tzinfo=dt.timezone(dt.timedelta(hours=5,minutes=30)))))
 def test_reordered_duplicates_without_primary_keys(self):
  rows=[(1,None),(1,None),(2,'你好')]
  self.assertTrue(compare_cursors(Cursor(rows),Cursor(rows[::-1]),2,batch=1)['passed'])
  changed=[(1,None),(2,'你好'),(2,'你好')]
  self.assertFalse(compare_cursors(Cursor(rows),Cursor(changed),2)['passed'])
 def test_corruption_even_when_count_and_total_equal(self):
  self.assertFalse(compare_cursors(Cursor([(1,),(4,)]),Cursor([(2,),(3,)]),1)['passed'])

class APIContracts(unittest.TestCase):
 def test_private_routes_require_login(self):
  with patch('moderniser.service.store.user_for_token',return_value=None):
   client=TestClient(app)
   for url in ['/v1/jobs','/migrate/status/123','/v1/uploads/123/download','/v1/artifacts/123/download']:
    self.assertEqual(client.get(url).status_code,401)
 def test_missing_owner_record_returns_404(self):
  with patch('moderniser.service.store.get',return_value=None) as get:
   with self.assertRaises(HTTPException) as caught:owned('job','a156673e-a047-44c7-8fe6-aae9c8f40000','alice')
   self.assertEqual(caught.exception.status_code,404)
   self.assertEqual(get.call_args.args[-1],'alice')
 def test_type_overrides_reject_sql(self):
  validate_mappings({'orders.total':'NUMERIC(38,2)'})
  with self.assertRaises(HTTPException):validate_mappings({'orders.total':'TEXT);DROP DATABASE postgres;--'})

if __name__=='__main__':unittest.main()
