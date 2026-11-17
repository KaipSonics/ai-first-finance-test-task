"""Independent integer-kopeck financial verification of the actual input CSVs.
The reference calculations use only integer kopecks and raw CSVs.
Candidate functions are imported solely to compare their reported output.
"""
from pathlib import Path
import unittest
from datetime import date
import csv, sys
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from src.finance import load_data, build_report

def rows(name):
 with (root/'data'/name).open(encoding='utf-8-sig',newline='') as stream:
  return list(csv.DictReader(stream))
contracts,works,payments=rows('contracts.csv'),rows('works.csv'),rows('payments.csv')
def cents(s):
 a,sep,b=s.partition('.')
 if not sep: b=''
 return int(a)*100+int(b.ljust(2,'0'))
def human(n):
 sign='-' if n<0 else ''
 n=abs(n)
 return f'{sign}{n//100},{n%100:02}'
def on_or_before(s, day): return bool(s) and s<=day

def reference(day):
 result={}
 for c in contracts:
  cid=c['contract_id']
  if c['signed_at']>day:continue
  w=[x for x in works if x['contract_id']==cid]
  p=[x for x in payments if x['contract_id']==cid]
  st={}
  for x in w:
   status=('cancelled' if x['status']=='cancelled' and on_or_before(x['cancelled_at'],day)
      else 'done' if x['status']=='done' and on_or_before(x['completed_at'],day)
      else 'planned')
   st[x['work_id']]={'status':status,'eligible':0 if status=='cancelled' else cents(x['price']),
                    'direct':0,'general':0}
  net={}
  sent=returned=queued=0
  for x in p:
   val=cents(x['amount'])
   if x['status']=='queued':
    net[x['payment_id']]=0
    if x['created_at']<=day:queued+=val
   elif x['paid_at']<=day:
    sent+=val
    r=val if x['status']=='refunded' and x['refunded_at']<=day else 0
    returned+=r
    net[x['payment_id']]=val-r
   else: net[x['payment_id']]=0
  ordered=sorted(p,key=lambda x: (x['paid_at'] or '9999-12-31',x['payment_id']))
  alloc={x['payment_id']:{} for x in ordered}
  for x in ordered:
   if x['work_id'] and net[x['payment_id']]:
    s=st[x['work_id']]
    a=min(net[x['payment_id']],s['eligible']-s['direct']-s['general'])
    s['direct']+=a
    if a:alloc[x['payment_id']][x['work_id']]=a
  priority=sorted((x for x in w if st[x['work_id']]['eligible']),
    key=lambda x:(0,x['completed_at'],x['work_id']) if st[x['work_id']]['status']=='done'
       else (1,'9999-12-31',x['work_id']))
  for x in ordered:
   if x['work_id'] or not net[x['payment_id']]:continue
   left=net[x['payment_id']]
   for target in priority:
    s=st[target['work_id']]
    a=min(left,s['eligible']-s['direct']-s['general'])
    s['general']+=a
    if a:alloc[x['payment_id']][target['work_id']]=a
    left-=a
    if not left:break
  active=sum(z['eligible'] for z in st.values())
  applied=sum(z['direct']+z['general'] for z in st.values())
  done=sum(z['eligible'] for z in st.values() if z['status']=='done')
  a_done=sum(z['direct']+z['general'] for z in st.values() if z['status']=='done')
  result[cid]={'total':{'cash_sent_gross':sent,'cash_refunded':returned,
   'cash_paid_net':sent-returned,'active_works_amount':active,
   'accepted_works_amount':done,'applied_to_accepted':a_done,
   'remaining_accepted':done-a_done,'remaining_planned':active-done-(applied-a_done),
   'queued_not_paid':queued,'applied_total':applied,'unallocated_total':sent-returned-applied,
   'remaining_active_works':active-applied},'works':st,'payments':net,'alloc':alloc}
 return result


class IndependentKopecksOracleTests(unittest.TestCase):
    def test_original_dataset_13_dates_933_independent_amount_checks(self):
        D=load_data(root/'data')
        checks=0
        for day in ['2026-08-24','2026-08-25','2026-09-01','2026-09-05','2026-09-09','2026-09-10',
                    '2026-10-11','2026-10-12','2026-10-25','2026-11-01','2026-11-10','2026-11-11','2026-11-12']:
         oracle=reference(day)
         report=build_report(D,date.fromisoformat(day))
         self.assertEqual(set(oracle), {r['contract_id'] for r in report.contract_rows})
         for c in report.contract_rows:
          r=oracle[c['contract_id']]
          for key,v in r['total'].items():
           self.assertEqual(c[key], human(v), (day, c['contract_id'], key))
           checks+=1
         for w in report.work_rows:
          z=oracle[w['contract_id']]['works'][w['work_id']]
          for key,v in [('status_as_of',z['status']),('payable_price',human(z['eligible'])),
                        ('paid_direct',human(z['direct'])),('paid_general',human(z['general'])),
                        ('paid_total',human(z['direct']+z['general'])),
                        ('remaining_to_pay',human(z['eligible']-z['direct']-z['general']))]:
           self.assertEqual(w[key], v, (day, w['work_id'], key))
           checks+=1
         for p in report.payment_rows:
          v=oracle[p['contract_id']]['payments'][p['payment_id']]
          self.assertEqual(p['counted_cash'], human(v), (day, p['payment_id']))
          checks+=1
         for a in report.allocation_rows:
          v=oracle[a['contract_id']]['alloc'][a['payment_id']][a['work_id']]
          self.assertEqual(a['allocated'], human(v), (day, a))
          checks+=1
 
        self.assertEqual(checks, 933)