"""Независимые от CLI проверки расчёта и формата отчёта (unittest)."""

import csv
import random
import subprocess
import sys
import shutil
import tempfile
import unittest
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

from src.finance import (
    Contract, DataError, Dataset, Payment, Work, build_report, load_data,
    parse_money, write_report,
)

ROOT = Path(__file__).resolve().parents[1]
DATE = date(2026, 11, 10)


def dec(value):
    return Decimal(value.replace(',', '.'))


def index_by(rows, key):
    return {row[key]: row for row in rows}


def sample(works, payments, amount='100.00'):
    return Dataset(
        (Contract('D-1', 'Test', Decimal(amount), date(2026, 1, 1)),),
        tuple(works), tuple(payments),
    )


def work(wid, price, status='done', accepted=date(2026, 1, 2), cancelled=None):
    return Work(wid, 'D-1', wid, Decimal(price), status,
                accepted if status == 'done' else None,
                cancelled if status == 'cancelled' else None)


def payment(pid, price, work_id='', status='paid',
            paid=date(2026, 1, 3), refunded=None):
    return Payment(pid, 'D-1', work_id, '', Decimal(price), status,
                   date(2026, 1, 1), paid if status != 'queued' else None,
                   refunded)


class OriginalFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_data(ROOT / 'data')
        cls.result = build_report(cls.dataset, DATE)
        cls.works = index_by(cls.result.work_rows, 'work_id')
        cls.contracts = index_by(cls.result.contract_rows, 'contract_id')
        cls.payments = index_by(cls.result.payment_rows, 'payment_id')

    def test_d1_direct_payment_and_fifo(self):
        expected = {
            'W-11': ('100000.00', '0.00', '0.00'),
            'W-12': ('100000.00', '0.00', '0.00'),
            'W-13': ('0.00', '0.00', '0.00'),
            'W-14': ('85000.00', '15000.00', '0.00'),
            'W-15': ('15000.00', '0.00', '85000.00'),
            'W-16': ('0.00', '0.00', '100000.00'),
        }
        for wid, (general, direct, remaining) in expected.items():
            with self.subTest(wid=wid):
                row = self.works[wid]
                self.assertEqual(dec(row['paid_general']), Decimal(general))
                self.assertEqual(dec(row['paid_direct']), Decimal(direct))
                self.assertEqual(dec(row['remaining_to_pay']), Decimal(remaining))
        self.assertEqual(self.works['W-13']['status_as_of'], 'cancelled')
        self.assertEqual(dec(self.works['W-13']['original_price']), Decimal('100000.00'))
        self.assertEqual(dec(self.works['W-13']['payable_price']), Decimal('0.00'))

    def test_d2_future_work_and_partial_prepayment(self):
        self.assertEqual(self.works['W-23']['source_status'], 'done')
        self.assertEqual(self.works['W-23']['status_as_of'], 'planned')
        self.assertEqual(dec(self.works['W-21']['paid_total']), Decimal('20000.00'))
        self.assertEqual(dec(self.works['W-22']['paid_total']), Decimal('30000.00'))
        self.assertEqual(dec(self.works['W-23']['paid_total']), Decimal('25000.00'))
        self.assertEqual(dec(self.works['W-23']['remaining_to_pay']), Decimal('15000.00'))

    def test_refunded_queued_and_future_payments(self):
        for pid in ('P-2', 'P-4', 'P-8'):
            with self.subTest(pid=pid):
                self.assertEqual(dec(self.payments[pid]['counted_cash']), Decimal('0.00'))
                self.assertEqual(dec(self.payments[pid]['allocated_to_works']), Decimal('0.00'))
        self.assertEqual(dec(self.payments['P-3']['allocated_to_works']), Decimal('15000.00'))
        self.assertIn('возвращён', self.payments['P-2']['treatment'])
        self.assertIn('очереди', self.payments['P-4']['treatment'])
        self.assertIn('после даты', self.payments['P-8']['treatment'])

    def test_contract_balances_and_contractual_discrepancy(self):
        expected = {
            'D-1': ('600000.00', '500000.00', '315000.00', '185000.00', '100000.00'),
            'D-2': ('90000.00', '90000.00', '75000.00', '15000.00', '0.00'),
        }
        for cid, values in expected.items():
            for field, value in zip(('contract_amount', 'active_works_amount',
                                     'cash_paid_net', 'remaining_active_works',
                                     'contract_minus_active_works'), values):
                with self.subTest(contract=cid, field=field):
                    self.assertEqual(dec(self.contracts[cid][field]), Decimal(value))
        self.assertEqual(sum(dec(r['cash_paid_net']) for r in self.result.contract_rows), Decimal('390000.00'))
        self.assertEqual(sum(dec(r['remaining_active_works']) for r in self.result.contract_rows), Decimal('200000.00'))

    def test_reconciliation_from_both_views_and_allocation_audit(self):
        for cid, c in self.contracts.items():
            wr = [r for r in self.result.work_rows if r['contract_id'] == cid]
            pr = [r for r in self.result.payment_rows if r['contract_id'] == cid]
            al = [r for r in self.result.allocation_rows if r['contract_id'] == cid]
            self.assertEqual(sum(dec(r['payable_price']) for r in wr), dec(c['active_works_amount']))
            self.assertEqual(sum(dec(r['paid_total']) for r in wr), dec(c['applied_total']))
            self.assertEqual(sum(dec(r['remaining_to_pay']) for r in wr), dec(c['remaining_active_works']))
            self.assertEqual(sum(dec(r['counted_cash']) for r in pr), dec(c['cash_paid_net']))
            self.assertEqual(sum(dec(r['allocated']) for r in al), dec(c['applied_total']))
            self.assertEqual(sum(dec(r['unallocated_cash']) for r in pr), dec(c['unallocated_total']))
            self.assertEqual(dec(c['cash_paid_net']), dec(c['applied_total']) + dec(c['unallocated_total']))

    def test_refund_is_retroactive_only_at_refund_date(self):
        earlier = build_report(self.dataset, date(2026, 9, 5))
        p = index_by(earlier.payment_rows, 'payment_id')
        self.assertEqual(dec(p['P-2']['counted_cash']), Decimal('50000.00'))
        self.assertEqual(dec(index_by(earlier.contract_rows, 'contract_id')['D-1']['cash_paid_net']), Decimal('350000.00'))
        later = build_report(self.dataset, date(2026, 9, 10))
        self.assertEqual(dec(index_by(later.payment_rows, 'payment_id')['P-2']['counted_cash']), Decimal('0.00'))
        self.assertEqual(dec(index_by(later.contract_rows, 'contract_id')['D-1']['cash_paid_net']), Decimal('300000.00'))

    def test_cancelled_work_not_retroactively_cancelled(self):
        before = build_report(self.dataset, date(2026, 10, 11))
        after = build_report(self.dataset, date(2026, 10, 12))
        self.assertEqual(index_by(before.work_rows, 'work_id')['W-13']['status_as_of'], 'planned')
        self.assertEqual(index_by(after.work_rows, 'work_id')['W-13']['status_as_of'], 'cancelled')
        self.assertEqual(dec(index_by(before.work_rows, 'work_id')['W-13']['payable_price']), Decimal('100000.00'))
        self.assertEqual(dec(index_by(after.work_rows, 'work_id')['W-13']['payable_price']), Decimal('0.00'))

    def test_cash_refund_is_visible_as_two_cash_flows(self):
        # Netting a fully refunded transfer must not erase the cash audit trail.
        before = build_report(self.dataset, date(2026, 9, 5))
        p2_before = index_by(before.payment_rows, 'payment_id')['P-2']
        self.assertEqual(p2_before['cash_sent_to_date'], '50000,00')
        self.assertEqual(p2_before['cash_refunded_to_date'], '0,00')
        self.assertEqual(p2_before['counted_cash'], '50000,00')
        after = build_report(self.dataset, DATE)
        p2_after = index_by(after.payment_rows, 'payment_id')['P-2']
        self.assertEqual(p2_after['cash_sent_to_date'], '50000,00')
        self.assertEqual(p2_after['cash_refunded_to_date'], '50000,00')
        self.assertEqual(p2_after['counted_cash'], '0,00')
        c1 = index_by(after.contract_rows, 'contract_id')['D-1']
        self.assertEqual(c1['cash_sent_gross'], '365000,00')
        self.assertEqual(c1['cash_refunded'], '50000,00')
        self.assertEqual(c1['cash_paid_net'], '315000,00')

    def test_remaining_accepted_separate_from_future(self):
        c = index_by(self.result.contract_rows, 'contract_id')
        for contract_id, expected_advance, expected_remaining in (
            ('D-1', '15000,00', '185000,00'),
            ('D-2', '25000,00', '15000,00'),
        ):
            self.assertEqual(c[contract_id]['remaining_accepted'], '0,00')
            self.assertEqual(c[contract_id]['applied_to_planned'], expected_advance)
            self.assertEqual(c[contract_id]['remaining_planned'], expected_remaining)
        self.assertEqual(c['D-1']['queued_not_paid'], '150000,00')
        self.assertEqual(c['D-2']['queued_not_paid'], '0,00')
        self.assertEqual(sum(dec(x['cash_sent_gross']) for x in c.values()), Decimal('440000.00'))
        self.assertEqual(sum(dec(x['cash_refunded']) for x in c.values()), Decimal('50000.00'))

    def test_temporal_reallocation_is_explicitly_not_a_historical_ledger(self):
        before = build_report(self.dataset, date(2026, 10, 11))
        after = build_report(self.dataset, date(2026, 10, 12))
        w_before = index_by(before.work_rows, 'work_id')
        w_after = index_by(after.work_rows, 'work_id')
        self.assertEqual(w_before['W-13']['paid_total'], '100000,00')
        self.assertEqual(w_after['W-13']['paid_total'], '0,00')
        # No money moved on 12 October: only the allocation assumption changed.
        self.assertEqual(index_by(before.contract_rows, 'contract_id')['D-1']['cash_paid_net'],
                         index_by(after.contract_rows, 'contract_id')['D-1']['cash_paid_net'])

    def test_future_payment_creates_detectable_overpayment(self):
        report = build_report(self.dataset, date(2026, 11, 12))
        c2 = index_by(report.contract_rows, 'contract_id')['D-2']
        self.assertEqual(dec(c2['cash_paid_net']), Decimal('95000.00'))
        self.assertEqual(dec(c2['applied_total']), Decimal('90000.00'))
        self.assertEqual(dec(c2['unallocated_general']), Decimal('5000.00'))
        self.assertEqual(dec(c2['cash_over_active_works']), Decimal('5000.00'))
        self.assertEqual(dec(c2['remaining_active_works']), Decimal('0.00'))
        self.assertEqual(index_by(report.work_rows, 'work_id')['W-23']['status_as_of'], 'done')

    def test_deterministic_and_excel_compatible_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            p1, p2 = Path(temp) / 'first', Path(temp) / 'second'
            write_report(self.result, p1)
            write_report(build_report(self.dataset, DATE), p2)
            for name in ('works_report.csv', 'contracts_report.csv', 'payments_audit.csv', 'allocation_audit.csv'):
                with self.subTest(file=name):
                    raw = (p1 / name).read_bytes()
                    self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
                    self.assertEqual(raw, (p2 / name).read_bytes())
                    with (p1 / name).open(encoding='utf-8-sig', newline='') as f:
                        rows = list(csv.DictReader(f, delimiter=';'))
                    self.assertGreater(len(rows), 0)
                    self.assertEqual(rows[0]['report_date'], '2026-11-10')
                    self.assertTrue(all(None not in r for r in rows))

    def test_payment_allocations_exact_by_id(self):
        actual = {}
        for r in self.result.allocation_rows:
            actual[(r['payment_id'], r['work_id'])] = dec(r['allocated'])
        expected = {
            ('P-1', 'W-11'): Decimal('100000.00'),
            ('P-1', 'W-12'): Decimal('100000.00'),
            ('P-1', 'W-14'): Decimal('85000.00'),
            ('P-1', 'W-15'): Decimal('15000.00'),
            ('P-3', 'W-14'): Decimal('15000.00'),
            ('P-5', 'W-21'): Decimal('20000.00'),
            ('P-5', 'W-22'): Decimal('25000.00'),
            ('P-6', 'W-22'): Decimal('5000.00'),
            ('P-6', 'W-23'): Decimal('20000.00'),
            ('P-7', 'W-23'): Decimal('5000.00'),
        }
        self.assertEqual(actual, expected)


class SyntheticScenarios(unittest.TestCase):
    def test_cents_exact_and_partial_payment(self):
        ds = sample([work('W1', '0.30')], [payment('P1', '0.10'), payment('P2', '0.20')], '0.30')
        result = build_report(ds, DATE)
        row = result.work_rows[0]
        self.assertEqual(row['paid_total'], '0,30')
        self.assertEqual(row['remaining_to_pay'], '0,00')
        partial = build_report(sample([work('W1', '0.30')], [payment('P1', '0.10')], '0.30'), DATE)
        self.assertEqual(partial.work_rows[0]['remaining_to_pay'], '0,20')
        self.assertEqual(partial.work_rows[0]['paid_total'], '0,10')

    def test_direct_reservation_before_general_allocation(self):
        ds = sample([work('W1', '10.00'), work('W2', '10.00', 'planned')],
                    [payment('P1', '12.00'), payment('P2', '7.00', 'W2')], '20.00')
        result = build_report(ds, DATE)
        w = index_by(result.work_rows, 'work_id')
        self.assertEqual((w['W2']['paid_direct'], w['W2']['paid_general']), ('7,00', '2,00'))
        self.assertEqual(w['W1']['paid_total'], '10,00')
        self.assertEqual(w['W2']['remaining_to_pay'], '1,00')

    def test_direct_overpayment_remains_restricted_not_reassigned(self):
        ds = sample([work('W1', '10.00'), work('W2', '10.00')],
                    [payment('P1', '15.00', 'W1')], '20.00')
        result = build_report(ds, DATE)
        c = result.contract_rows[0]
        self.assertEqual(c['unallocated_direct'], '5,00')
        self.assertEqual(c['remaining_active_works'], '10,00')
        self.assertEqual(c['cash_over_active_works'], '0,00')
        self.assertEqual(index_by(result.work_rows, 'work_id')['W2']['paid_total'], '0,00')

    def test_cancelled_direct_payment_not_hidden(self):
        ds = sample([work('W1', '10.00', 'cancelled', cancelled=date(2026, 1, 2)),
                     work('W2', '10.00')], [payment('P1', '6.00', 'W1')], '20.00')
        result = build_report(ds, DATE)
        self.assertEqual(result.contract_rows[0]['unallocated_direct'], '6,00')
        self.assertEqual(result.contract_rows[0]['applied_total'], '0,00')
        self.assertEqual(result.contract_rows[0]['remaining_active_works'], '10,00')

    def test_refunded_after_report_date_still_counted(self):
        ds = sample([work('W1', '15.00')], [payment('P1', '15.00', status='refunded',
                                                  refunded=date(2026, 12, 1))], '15.00')
        result = build_report(ds, DATE)
        self.assertEqual(result.contract_rows[0]['cash_paid_net'], '15,00')
        result2 = build_report(ds, date(2026, 12, 1))
        self.assertEqual(result2.contract_rows[0]['cash_paid_net'], '0,00')

    def test_queued_amount_never_counted(self):
        ds = sample([work('W1', '10.00')], [payment('P1', '10.00', status='queued')], '10.00')
        self.assertEqual(build_report(ds, DATE).contract_rows[0]['cash_paid_net'], '0,00')

    def test_future_work_becomes_accepted_at_event_date(self):
        ds = sample([work('W1', '10.00', 'done', date(2026, 11, 11))],
                    [payment('P1', '4.00')], '10.00')
        self.assertEqual(build_report(ds, DATE).work_rows[0]['status_as_of'], 'planned')
        self.assertEqual(build_report(ds, date(2026, 11, 11)).work_rows[0]['status_as_of'], 'done')

    def test_unsigned_contract_excluded_from_earlier_report(self):
        ds = Dataset((Contract('D', 'Future', Decimal('10'), date(2026, 12, 1)),), (), ())
        self.assertEqual(build_report(ds, DATE).contract_rows, [])

    def test_csv_formula_safety_without_negative_money_corruption(self):
        ds = Dataset((Contract('D-1', '=DANGEROUS()', Decimal('10.00'), date(2026, 1, 1)),),
                     (Work('W-1', 'D-1', '=CMD()', Decimal('20.00'), 'planned', None, None),), ())
        report = build_report(ds, DATE)
        with tempfile.TemporaryDirectory() as temp:
            write_report(report, Path(temp))
            with (Path(temp) / 'works_report.csv').open(encoding='utf-8-sig', newline='') as f:
                w = next(csv.DictReader(f, delimiter=';'))
            with (Path(temp) / 'contracts_report.csv').open(encoding='utf-8-sig', newline='') as f:
                c = next(csv.DictReader(f, delimiter=';'))
            self.assertEqual(w['title'], "'=CMD()")
            self.assertEqual(w['contractor'], "'=DANGEROUS()")
            self.assertEqual(c['contract_minus_active_works'], '-10,00')

    def test_csv_order_independent_for_allocation(self):
        a = [work('W2', '10.00'), work('W1', '10.00')]
        p = [payment('P2', '7.00'), payment('P1', '13.00')]
        first = build_report(sample(a, p, '20.00'), DATE)
        second = build_report(sample(list(reversed(a)), list(reversed(p)), '20.00'), DATE)
        self.assertEqual(first.work_rows, second.work_rows)
        self.assertEqual(first.payment_rows, second.payment_rows)
        self.assertEqual(first.allocation_rows, second.allocation_rows)


class AdversarialReconciliationTests(unittest.TestCase):
    def test_many_cents_and_status_combinations(self):
        rng = random.Random(712026)
        for case in range(180):
            def cents(upper):
                return Decimal(rng.randint(1, upper)).scaleb(-2)

            works = [
                work('A', str(cents(1500))),
                work('B', str(cents(2000)), 'done', date(2026, 1, 5)),
                work('C', str(cents(2500)), 'planned'),
                work('D', str(cents(1800)), 'cancelled', cancelled=date(2026, 1, 10)),
            ]
            payments = [
                payment('X', str(cents(3500))),
                payment('Y', str(cents(3000)), 'B'),
                payment('Z', str(cents(1500)), status='refunded', refunded=date(2026, 1, 9)),
                payment('Q', str(cents(900)), status='queued'),
                payment('R', str(cents(1100)), 'D'),
            ]
            ds = sample(works, payments, '100.00')
            for as_of in (date(2026, 1, 6), date(2026, 1, 9), date(2026, 1, 10), DATE):
                r = build_report(ds, as_of)
                c = r.contract_rows[0]
                m = dec
                with self.subTest(case=case, as_of=as_of):
                    self.assertEqual(m(c['cash_sent_gross']) - m(c['cash_refunded']), m(c['cash_paid_net']))
                    self.assertEqual(m(c['cash_paid_net']), m(c['applied_total']) + m(c['unallocated_total']))
                    self.assertEqual(m(c['active_works_amount']), m(c['applied_total']) + m(c['remaining_active_works']))
                    self.assertEqual(m(c['remaining_active_works']), m(c['remaining_accepted']) + m(c['remaining_planned']))
                    self.assertEqual(m(c['applied_total']), m(c['applied_to_accepted']) + m(c['applied_to_planned']))
                    self.assertEqual(m(c['cash_paid_net']), sum((m(row['counted_cash']) for row in r.payment_rows), Decimal('0')))
                    self.assertEqual(m(c['applied_total']), sum((m(row['allocated']) for row in r.allocation_rows), Decimal('0')))
                    for row in r.work_rows:
                        self.assertEqual(m(row['payable_price']), m(row['paid_total']) + m(row['remaining_to_pay']))
                        self.assertGreaterEqual(m(row['remaining_to_pay']), Decimal('0'))
                    for row in r.payment_rows:
                        self.assertEqual(m(row['cash_sent_to_date']) - m(row['cash_refunded_to_date']), m(row['counted_cash']))
                        self.assertEqual(m(row['counted_cash']), m(row['allocated_to_works']) + m(row['unallocated_cash']))



class LateLedgerEntryTests(unittest.TestCase):
    def test_cash_counts_even_when_booked_after_reporting_date(self):
        # Bookkeeping date is not the date money actually left the bank.
        p = Payment('P-X', 'D-1', '', '', Decimal('17.03'), 'paid',
                    date(2026, 11, 12), date(2026, 11, 5), None)
        ds = sample([work('W-X', '20.00')], [p], '20.00')
        report = build_report(ds, DATE)
        self.assertEqual(report.contract_rows[0]['cash_paid_net'], '17,03')
        self.assertEqual(report.work_rows[0]['paid_total'], '17,03')
        self.assertEqual(report.work_rows[0]['remaining_to_pay'], '2,97')
        self.assertIn('внесён в учёт позже', report.payment_rows[0]['treatment'])

    def test_late_booked_refund_uses_refund_date(self):
        p = Payment('P-X', 'D-1', '', '', Decimal('17.03'), 'refunded',
                    date(2026, 11, 12), date(2026, 11, 5), date(2026, 11, 9))
        ds = sample([work('W-X', '20.00')], [p], '20.00')
        before = build_report(ds, date(2026, 11, 8))
        after = build_report(ds, DATE)
        self.assertEqual(before.contract_rows[0]['cash_paid_net'], '17,03')
        self.assertEqual(after.contract_rows[0]['cash_paid_net'], '0,00')
        self.assertEqual(after.contract_rows[0]['cash_sent_gross'], '17,03')
        self.assertEqual(after.contract_rows[0]['cash_refunded'], '17,03')

    def test_late_booked_queued_payment_not_counted_as_cash(self):
        p = Payment('P-X', 'D-1', '', '', Decimal('17.03'), 'queued',
                    date(2026, 11, 12), None, None)
        ds = sample([work('W-X', '20.00')], [p], '20.00')
        before = build_report(ds, DATE)
        after = build_report(ds, date(2026, 11, 13))
        self.assertEqual(before.contract_rows[0]['queued_not_paid'], '0,00')
        self.assertEqual(after.contract_rows[0]['queued_not_paid'], '17,03')
        self.assertEqual(after.contract_rows[0]['cash_paid_net'], '0,00')


class MonetaryPrecisionRegressionTests(unittest.TestCase):
    def test_parsing_very_large_valid_amount_never_loses_kopecks(self):
        raw = '12345678901234567890123456789012345678901234567890.99'
        self.assertEqual(format(parse_money(raw, 'precision'), '.2f'), raw)

    def test_sums_preserve_kopecks_even_above_default_decimal_precision(self):
        raw = '99999999999999999999999999.99'
        with localcontext() as ctx:
            ctx.prec = 100
            expected = Decimal(raw) + Decimal(raw)
        ds = sample([work('W1', raw), work('W2', raw)],
                    [payment('P1', raw, 'W1'), payment('P2', raw, 'W2')],
                    format(expected, '.2f'))
        with localcontext() as ctx:
            ctx.prec = 7  # Caller context must not change the financial result.
            r = build_report(ds, DATE)
        row = r.contract_rows[0]
        self.assertEqual(row['cash_paid_net'], format(expected, '.2f').replace('.', ','))
        self.assertEqual(row['active_works_amount'], format(expected, '.2f').replace('.', ','))
        self.assertEqual(row['remaining_active_works'], '0,00')
        self.assertEqual(row['unallocated_total'], '0,00')


class CommandLineSmokeTests(unittest.TestCase):
    def test_cli_runs_end_to_end_and_produces_four_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            run = subprocess.run([sys.executable, '-m', 'src.cli', '--date', '2026-11-10',
                                  '--output-dir', temp], cwd=ROOT, capture_output=True,
                                 text=True, timeout=10, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('390000,00', run.stdout)
            self.assertIn('по принятым: 0,00', run.stdout)
            self.assertIn('В очереди (не выплачено): 150000,00', run.stdout)
            generated = sorted(x.name for x in Path(temp).iterdir())
            self.assertEqual(generated, ['allocation_audit.csv', 'contracts_report.csv',
                                         'payments_audit.csv', 'works_report.csv'])
            with (Path(temp) / 'contracts_report.csv').open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f, delimiter=';'))
            self.assertEqual(rows[0]['remaining_accepted'], '0,00')
            self.assertEqual(rows[0]['cash_refunded'], '50000,00')

    def test_cli_rejects_bad_date_without_writing_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            run = subprocess.run([sys.executable, '-m', 'src.cli', '--date', '2026-02-30',
                                  '--output-dir', temp], cwd=ROOT, capture_output=True,
                                 text=True, timeout=10, check=False)
            self.assertEqual(run.returncode, 2)
            self.assertIn('невозможная дата', run.stderr)
            self.assertEqual(list(Path(temp).iterdir()), [])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        for name in ('contracts.csv', 'works.csv', 'payments.csv'):
            shutil.copyfile(ROOT / 'data' / name, self.folder / name)

    def edit(self, filename, before, after):
        path = self.folder / filename
        text = path.read_text(encoding='utf-8')
        self.assertIn(before, text)
        path.write_text(text.replace(before, after, 1), encoding='utf-8')

    def test_original_data_valid(self):
        self.assertEqual(len(load_data(self.folder).payments), 8)

    def test_unknown_contract_and_cross_contract_work(self):
        self.edit('works.csv', 'W-11,D-1,', 'W-11,D-404,')
        with self.assertRaisesRegex(DataError, 'неизвестный договор'):
            load_data(self.folder)
        shutil.copyfile(ROOT / 'data' / 'works.csv', self.folder / 'works.csv')
        self.edit('payments.csv', 'P-3,D-1,W-14,', 'P-3,D-2,W-14,')
        with self.assertRaisesRegex(DataError, 'отсутствует в договоре'):
            load_data(self.folder)

    def test_duplicate_primary_key(self):
        self.edit('works.csv', 'W-12,D-1,', 'W-11,D-1,')
        with self.assertRaisesRegex(DataError, 'повторяющийся'):
            load_data(self.folder)

    def test_bad_amounts_and_more_than_two_decimals(self):
        for value in ('15000.001', 'nan', '-1.00'):
            with self.subTest(value=value):
                self.edit('payments.csv', '15000.00,paid,', f'{value},paid,')
                with self.assertRaises(DataError):
                    load_data(self.folder)
                shutil.copyfile(ROOT / 'data' / 'payments.csv', self.folder / 'payments.csv')
        self.assertEqual(parse_money('0.01', 'test'), Decimal('0.01'))

    def test_invalid_dates_status_and_inconsistent_refund(self):
        self.edit('works.csv', '2026-10-28,', '2026-02-30,')
        with self.assertRaisesRegex(DataError, 'невозможная дата'):
            load_data(self.folder)
        shutil.copyfile(ROOT / 'data' / 'works.csv', self.folder / 'works.csv')
        self.edit('payments.csv', 'refunded,2026-09-02,2026-09-02,2026-09-10',
                  'refunded,2026-09-02,2026-09-02,')
        with self.assertRaisesRegex(DataError, 'противоречит датам'):
            load_data(self.folder)
        shutil.copyfile(ROOT / 'data' / 'payments.csv', self.folder / 'payments.csv')
        self.edit('works.csv', '100000.00,planned,,', '100000.00,pending,,')
        with self.assertRaisesRegex(DataError, 'неизвестный статус'):
            load_data(self.folder)

    def test_missing_column_or_missing_file(self):
        self.edit('works.csv', 'cancelled_at', 'cancel_date')
        with self.assertRaisesRegex(DataError, 'ожидаются колонки'):
            load_data(self.folder)
        (self.folder / 'works.csv').unlink()
        with self.assertRaisesRegex(DataError, 'Не найден'):
            load_data(self.folder)

    def test_nonpositive_payment_and_invalid_refund_chronology(self):
        self.edit('payments.csv', '15000.00,paid,', '0.00,paid,')
        with self.assertRaisesRegex(DataError, 'положительной'):
            load_data(self.folder)
        shutil.copyfile(ROOT / 'data' / 'payments.csv', self.folder / 'payments.csv')
        self.edit('payments.csv', '2026-10-24,2026-10-25', '2026-10-28,2026-10-25')
        # Posting after the cash transfer is allowed. Monetary reporting is
        # based on the transfer date, not the bookkeeping-entry date.
        ds = load_data(self.folder)
        p3 = next(p for p in ds.payments if p.payment_id == 'P-3')
        self.assertEqual(p3.cash_as_of(date(2026, 10, 26))[0], Decimal('15000.00'))


if __name__ == '__main__':
    unittest.main()