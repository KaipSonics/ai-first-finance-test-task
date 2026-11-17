"""Точный, воспроизводимый расчёт платежей по договорным работам.

Входные файлы — снимок учёта с историческими датами событий. Решения,
не содержащиеся в спецификации входных данных, описаны в DECISIONS.md.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import List, Optional, Tuple

CENT = Decimal('0.01')
ZERO = Decimal('0.00')
MONEY_PATTERN = re.compile(r'^\d+(?:\.\d{1,2})?$')
DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')

COLUMNS = {
    'contracts.csv': ('contract_id', 'contractor', 'amount', 'signed_at'),
    'works.csv': ('work_id', 'contract_id', 'title', 'price', 'status',
                  'completed_at', 'cancelled_at'),
    'payments.csv': ('payment_id', 'contract_id', 'work_id', 'purpose',
                     'amount', 'status', 'created_at', 'paid_at', 'refunded_at'),
}


class DataError(ValueError):
    """Ошибочный формат или противоречивые исходные сведения."""


def parse_date(value: str, source: str, *, optional: bool = False) -> Optional[date]:
    if not value and optional:
        return None
    if not DATE_PATTERN.fullmatch(value):
        raise DataError(f'{source}: дата должна иметь вид ГГГГ-ММ-ДД: {value!r}')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DataError(f'{source}: невозможная дата: {value!r}') from exc


def parse_money(value: str, source: str) -> Decimal:
    if not MONEY_PATTERN.fullmatch(value):
        raise DataError(f'{source}: сумма должна быть неотрицательным числом с точностью до копейки: {value!r}')
    try:
        raw = Decimal(value)
        # CSV amounts have at most two fractional digits, so normalization
        # must not be constrained by Decimal's default 28-digit context.
        with localcontext() as context:
            context.prec = max(28, len(raw.as_tuple().digits) + 2)
            return raw.quantize(CENT)
    except InvalidOperation as exc:
        raise DataError(f'{source}: некорректная сумма: {value!r}') from exc


def require(row: dict, field: str, source: str) -> str:
    value = row[field]
    if not value:
        raise DataError(f'{source}: пустое обязательное поле {field}')
    return value


def load_rows(data_dir: Path, filename: str, id_column: str) -> List[dict]:
    path = data_dir / filename
    try:
        with path.open('r', encoding='utf-8-sig', newline='') as file:
            reader = csv.DictReader(file, dialect='excel', strict=True)
            if reader.fieldnames is None:
                raise DataError(f'{path}: отсутствует заголовок')
            if tuple(reader.fieldnames) != COLUMNS[filename]:
                raise DataError(f'{path}: ожидаются колонки {", ".join(COLUMNS[filename])}')
            results: List[dict] = []
            seen = set()
            for number, original in enumerate(reader, start=2):
                source = f'{filename}:{number}'
                if None in original or any(value is None for value in original.values()):
                    raise DataError(f'{source}: неверное число полей')
                row = {key: value.strip() for key, value in original.items()}
                identifier = require(row, id_column, source)
                if identifier in seen:
                    raise DataError(f'{source}: повторяющийся {id_column}: {identifier}')
                seen.add(identifier)
                results.append(row)
            return results
    except FileNotFoundError as exc:
        raise DataError(f'Не найден входной файл: {path}') from exc
    except (UnicodeError, csv.Error) as exc:
        raise DataError(f'Ошибка чтения CSV {path}: {exc}') from exc


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: Optional[date]
    cancelled_at: Optional[date]

    def status_on(self, as_of: date) -> str:
        if self.status == 'cancelled' and self.cancelled_at <= as_of:
            return 'cancelled'
        if self.status == 'done' and self.completed_at <= as_of:
            return 'done'
        return 'planned'


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str
    purpose: str
    amount: Decimal
    status: str
    created_at: date
    paid_at: Optional[date]
    refunded_at: Optional[date]

    def cash_flows_as_of(self, as_of: date) -> Tuple[Decimal, Decimal, Decimal, str]:
        """Перечислено, возвращено, нетто и причина на дату (без двойного счёта)."""
        # created_at is the bookkeeping entry date, NOT the cash-flow date.
        # Late entry must not erase a transfer that happened before as_of.
        if self.status == 'queued':
            reason = 'в очереди; не выплачен'
            if self.created_at > as_of:
                reason = 'поставлен в очередь после даты отчёта'
            return ZERO, ZERO, ZERO, reason
        if self.paid_at > as_of:
            return ZERO, ZERO, ZERO, 'выплачен после даты отчёта'
        late_entry = '; внесён в учёт позже' if self.created_at > as_of else ''
        if self.status == 'refunded':
            if self.refunded_at <= as_of:
                return self.amount, self.amount, ZERO, 'полностью возвращён' + late_entry
            return self.amount, ZERO, self.amount, 'выплачен; возврат после даты отчёта' + late_entry
        return self.amount, ZERO, self.amount, 'выплачен' + late_entry

    def cash_as_of(self, as_of: date) -> Tuple[Decimal, str]:
        """Совместимый интерфейс для чистой выплаты."""
        _, _, net, reason = self.cash_flows_as_of(as_of)
        return net, reason


@dataclass(frozen=True)
class Dataset:
    contracts: Tuple[Contract, ...]
    works: Tuple[Work, ...]
    payments: Tuple[Payment, ...]


def load_data(data_dir: Path) -> Dataset:
    """Чтение + проверка схемы, ключей, сумм, дат, связей и статусов."""
    raw_c = load_rows(data_dir, 'contracts.csv', 'contract_id')
    raw_w = load_rows(data_dir, 'works.csv', 'work_id')
    raw_p = load_rows(data_dir, 'payments.csv', 'payment_id')
    contracts: List[Contract] = []
    for r in raw_c:
        ident = r['contract_id']
        src = f'contract {ident}'
        amount = parse_money(require(r, 'amount', src), src)
        if amount <= ZERO:
            raise DataError(f'{src}: сумма договора должна быть положительной')
        contracts.append(Contract(ident, require(r, 'contractor', src), amount,
                                  parse_date(r['signed_at'], src)))
    by_contract = {c.contract_id: c for c in contracts}
    works: List[Work] = []
    for r in raw_w:
        ident = r['work_id']
        src = f'work {ident}'
        cid = require(r, 'contract_id', src)
        if cid not in by_contract:
            raise DataError(f'{src}: неизвестный договор {cid}')
        status = r['status']
        if status not in ('planned', 'done', 'cancelled'):
            raise DataError(f'{src}: неизвестный статус работы {status!r}')
        completed = parse_date(r['completed_at'], src + '.completed_at', optional=True)
        cancelled = parse_date(r['cancelled_at'], src + '.cancelled_at', optional=True)
        if ((status == 'done' and (completed is None or cancelled is not None)) or
            (status == 'cancelled' and (cancelled is None or completed is not None)) or
            (status == 'planned' and (completed is not None or cancelled is not None))):
            raise DataError(f'{src}: статус противоречит датам принятия/отмены')
        event_at = completed or cancelled
        if event_at and event_at < by_contract[cid].signed_at:
            raise DataError(f'{src}: событие раньше даты договора')
        works.append(Work(ident, cid, require(r, 'title', src),
                          parse_money(require(r, 'price', src), src), status,
                          completed, cancelled))
    by_work = {w.work_id: w for w in works}
    payments: List[Payment] = []
    for r in raw_p:
        ident = r['payment_id']
        src = f'payment {ident}'
        cid = require(r, 'contract_id', src)
        if cid not in by_contract:
            raise DataError(f'{src}: неизвестный договор {cid}')
        wid = r['work_id']
        if wid and (wid not in by_work or by_work[wid].contract_id != cid):
            raise DataError(f'{src}: работа {wid} отсутствует в договоре {cid}')
        status = r['status']
        if status not in ('paid', 'queued', 'refunded'):
            raise DataError(f'{src}: неизвестный статус платежа {status!r}')
        created = parse_date(r['created_at'], src + '.created_at')
        paid = parse_date(r['paid_at'], src + '.paid_at', optional=True)
        refunded = parse_date(r['refunded_at'], src + '.refunded_at', optional=True)
        if created < by_contract[cid].signed_at:
            raise DataError(f'{src}: платёж зарегистрирован раньше подписания договора')
        if ((status == 'queued' and (paid is not None or refunded is not None)) or
            (status == 'paid' and (paid is None or refunded is not None)) or
            (status == 'refunded' and (paid is None or refunded is None))):
            raise DataError(f'{src}: статус противоречит датам оплаты/возврата')
        # A bank transfer may be entered into the ledger after it happened;
        # comparing paid_at with created_at would reject legitimate backfills.
        if refunded and refunded < paid:
            raise DataError(f'{src}: возврат раньше оплаты')
        amount = parse_money(require(r, 'amount', src), src)
        if amount <= ZERO:
            raise DataError(f'{src}: сумма платежа должна быть положительной')
        payments.append(Payment(ident, cid, wid, r['purpose'], amount, status,
                                created, paid, refunded))
    return Dataset(tuple(contracts), tuple(works), tuple(payments))


def money(value: Decimal) -> str:
    """Разделитель десятичной части — запятая (русская Excel-локаль)."""
    return format(value, '.2f').replace('.', ',')


def plain_date(value: Optional[date]) -> str:
    return value.isoformat() if value else ''


def sum_money(items) -> Decimal:
    return sum(items, ZERO)


@dataclass
class Report:
    work_rows: List[dict]
    contract_rows: List[dict]
    payment_rows: List[dict]
    allocation_rows: List[dict]


def build_report(dataset: Dataset, as_of: date) -> Report:
    """Точный расчёт независимо от размера сумм и контекста Decimal вызывающего кода."""
    amounts = ([c.amount for c in dataset.contracts] +
               [w.price for w in dataset.works] +
               [p.amount for p in dataset.payments])
    # Sums may be longer than every input, so allow room for the number
    # of entries and interim operations, without altering the global context.
    magnitude = max((max(v.adjusted() + 1, 1) for v in amounts if v), default=1)
    elements = len(dataset.contracts) + len(dataset.works) + len(dataset.payments)
    precision = max(28, magnitude + len(str(max(1, elements))) + 10)
    with localcontext() as context:
        context.prec = precision
        return _build_report(dataset, as_of)


def _build_report(dataset: Dataset, as_of: date) -> Report:
    """Прямые платежи первыми, общие — по очереди принятия и ID будущей работы."""
    work_rows: List[dict] = []
    contract_rows: List[dict] = []
    payment_rows: List[dict] = []
    allocation_rows: List[dict] = []
    for contract in sorted(dataset.contracts, key=lambda c: c.contract_id):
        if contract.signed_at > as_of:
            continue
        works = sorted((w for w in dataset.works if w.contract_id == contract.contract_id),
                       key=lambda w: w.work_id)
        payments = sorted((p for p in dataset.payments if p.contract_id == contract.contract_id),
                          key=lambda p: (p.paid_at or date.max, p.payment_id))
        state = {w.work_id: {
            'status': w.status_on(as_of), 'eligible': w.price if w.status_on(as_of) != 'cancelled' else ZERO,
            'direct': ZERO, 'advance': ZERO,
        } for w in works}
        flows = {p.payment_id: p.cash_flows_as_of(as_of) for p in payments}
        cash = {pid: (result[2], result[3]) for pid, result in flows.items()}
        allocated_by_payment = {p.payment_id: ZERO for p in payments}

        # Explicit designation prevails; its excess does not move to other works.
        for p in payments:
            amount, _ = cash[p.payment_id]
            if not p.work_id or amount == ZERO:
                continue
            target = state[p.work_id]
            already = target['direct'] + target['advance']
            applied = min(amount, target['eligible'] - already)
            if applied:
                target['direct'] += applied
                allocated_by_payment[p.payment_id] += applied
                allocation_rows.append({
                    'report_date': as_of.isoformat(), 'contract_id': contract.contract_id,
                    'payment_id': p.payment_id, 'work_id': p.work_id,
                    'allocation_type': 'direct', 'allocated': money(applied)})

        # Accepted first (oldest acceptance); other works by ID, regardless of CSV row order.
        priority_works = sorted((w for w in works if state[w.work_id]['eligible'] > ZERO),
            key=lambda w: (0, w.completed_at, w.work_id) if state[w.work_id]['status'] == 'done'
                          else (1, date.max, w.work_id))
        for p in payments:
            amount, _ = cash[p.payment_id]
            if p.work_id or amount == ZERO:
                continue
            remaining = amount
            for w in priority_works:
                st = state[w.work_id]
                capacity = st['eligible'] - st['direct'] - st['advance']
                applied = min(remaining, capacity)
                if applied > ZERO:
                    st['advance'] += applied
                    allocated_by_payment[p.payment_id] += applied
                    allocation_rows.append({
                        'report_date': as_of.isoformat(), 'contract_id': contract.contract_id,
                        'payment_id': p.payment_id, 'work_id': w.work_id,
                        'allocation_type': 'general', 'allocated': money(applied)})
                    remaining -= applied
                if remaining == ZERO:
                    break

        contract_cash = sum_money(cash[p.payment_id][0] for p in payments)
        gross_sent = sum_money(flows[p.payment_id][0] for p in payments)
        gross_returned = sum_money(flows[p.payment_id][1] for p in payments)
        queued_not_paid = sum_money(p.amount for p in payments
                                    if p.status == 'queued' and p.created_at <= as_of)
        if gross_sent - gross_returned != contract_cash:
            raise AssertionError('Ошибка внутренней сверки: валовые платежи и возвраты')
        direct_cash = sum_money(cash[p.payment_id][0] for p in payments if p.work_id)
        general_cash = contract_cash - direct_cash
        direct_applied = sum_money(state[w.work_id]['direct'] for w in works)
        general_applied = sum_money(state[w.work_id]['advance'] for w in works)
        total_applied = direct_applied + general_applied
        total_eligible = sum_money(state[w.work_id]['eligible'] for w in works)
        nominal = sum_money(w.price for w in works)
        cancelled_nominal = sum_money(w.price for w in works if state[w.work_id]['status'] == 'cancelled')
        done = sum_money(w.price for w in works if state[w.work_id]['status'] == 'done')
        planned = total_eligible - done
        paid_on_accepted = sum_money(state[w.work_id]['direct'] + state[w.work_id]['advance']
                                     for w in works if state[w.work_id]['status'] == 'done')
        paid_on_planned = total_applied - paid_on_accepted
        remaining_accepted = done - paid_on_accepted
        remaining_planned = planned - paid_on_planned
        if remaining_accepted < ZERO or remaining_planned < ZERO or remaining_accepted + remaining_planned != total_eligible - total_applied:
            raise AssertionError('Ошибка внутренней сверки: принятые и будущие работы')
        if total_applied > total_eligible or contract_cash < total_applied:
            raise AssertionError('Ошибка внутренней сверки: платежи/работы')
        if sum_money(allocated_by_payment.values()) != total_applied:
            raise AssertionError('Ошибка внутренней сверки: распределение по платежам')
        for w in works:
            st = state[w.work_id]
            paid_total = st['direct'] + st['advance']
            work_rows.append({
                'report_date': as_of.isoformat(), 'contract_id': contract.contract_id,
                'contractor': contract.contractor, 'work_id': w.work_id, 'title': w.title,
                'source_status': w.status, 'status_as_of': st['status'],
                'completed_at': plain_date(w.completed_at),
                'cancelled_at': plain_date(w.cancelled_at),
                'original_price': money(w.price), 'payable_price': money(st['eligible']),
                'paid_direct': money(st['direct']), 'paid_general': money(st['advance']),
                'paid_total': money(paid_total), 'remaining_to_pay': money(st['eligible'] - paid_total),
            })
        for p in payments:
            amount, reason = cash[p.payment_id]
            applied = allocated_by_payment[p.payment_id]
            payment_rows.append({
                'report_date': as_of.isoformat(), 'payment_id': p.payment_id,
                'contract_id': p.contract_id, 'work_id': p.work_id,
                'purpose': p.purpose, 'source_status': p.status,
                'created_at': p.created_at.isoformat(), 'paid_at': plain_date(p.paid_at),
                'refunded_at': plain_date(p.refunded_at), 'nominal_amount': money(p.amount),
                'cash_sent_to_date': money(flows[p.payment_id][0]),
                'cash_refunded_to_date': money(flows[p.payment_id][1]),
                'counted_cash': money(amount), 'allocated_to_works': money(applied),
                'unallocated_cash': money(amount - applied), 'treatment': reason,
            })
        unalloc_direct = direct_cash - direct_applied
        unalloc_general = general_cash - general_applied
        outstanding = total_eligible - total_applied
        if unalloc_direct < ZERO or unalloc_general < ZERO:
            raise AssertionError('Ошибка внутренней сверки: нераспределённые суммы')
        if outstanding + total_applied != total_eligible or total_applied + unalloc_direct + unalloc_general != contract_cash:
            raise AssertionError('Ошибка внутренней сверки: баланс договора')
        flags = []
        if contract.amount != total_eligible:
            flags.append('сумма договора не равна действующим работам')
        if unalloc_direct:
            flags.append('остаток адресного платежа требует проверки')
        if unalloc_general:
            flags.append('нераспределённый общий платёж')
        if contract_cash > contract.amount:
            flags.append('выплаты превысили сумму договора')
        contract_rows.append({
            'report_date': as_of.isoformat(), 'contract_id': contract.contract_id,
            'contractor': contract.contractor, 'contract_amount': money(contract.amount),
            'all_works_nominal': money(nominal),
            'cancelled_works_nominal': money(cancelled_nominal),
            'active_works_amount': money(total_eligible),
            'accepted_works_amount': money(done), 'planned_works_amount': money(planned),
            'applied_to_accepted': money(paid_on_accepted),
            'applied_to_planned': money(paid_on_planned),
            'remaining_accepted': money(remaining_accepted),
            'remaining_planned': money(remaining_planned),
            'cash_sent_gross': money(gross_sent), 'cash_refunded': money(gross_returned),
            'cash_paid_net': money(contract_cash), 'queued_not_paid': money(queued_not_paid),
            'cash_direct': money(direct_cash), 'cash_general': money(general_cash),
            'applied_direct': money(direct_applied), 'applied_general': money(general_applied),
            'applied_total': money(total_applied), 'unallocated_direct': money(unalloc_direct),
            'unallocated_general': money(unalloc_general),
            'unallocated_total': money(unalloc_direct + unalloc_general),
            'remaining_active_works': money(outstanding),
            'contract_minus_active_works': money(contract.amount - total_eligible),
            'contract_minus_cash_reference': money(contract.amount - contract_cash),
            'cash_over_active_works': money(max(contract_cash - total_eligible, ZERO)),
            'review_flags': '; '.join(flags),
        })
    return Report(work_rows, contract_rows, payment_rows, allocation_rows)


# Semicolon CSV with UTF-8 BOM and comma decimals for common Russian-language Excel.
WORK_FIELDS = (
    'report_date', 'contract_id', 'contractor', 'work_id', 'title',
    'source_status', 'status_as_of', 'completed_at', 'cancelled_at',
    'original_price', 'payable_price', 'paid_direct', 'paid_general',
    'paid_total', 'remaining_to_pay',
)
CONTRACT_FIELDS = (
    'report_date', 'contract_id', 'contractor', 'contract_amount',
    'all_works_nominal', 'cancelled_works_nominal', 'active_works_amount',
    'accepted_works_amount', 'planned_works_amount',
    'applied_to_accepted', 'applied_to_planned', 'remaining_accepted',
    'remaining_planned', 'cash_sent_gross', 'cash_refunded', 'cash_paid_net', 'queued_not_paid',
    'cash_direct', 'cash_general', 'applied_direct', 'applied_general',
    'applied_total', 'unallocated_direct', 'unallocated_general',
    'unallocated_total', 'remaining_active_works', 'contract_minus_active_works',
    'contract_minus_cash_reference', 'cash_over_active_works', 'review_flags',
)
PAYMENT_FIELDS = (
    'report_date', 'payment_id', 'contract_id', 'work_id', 'purpose',
    'source_status', 'created_at', 'paid_at', 'refunded_at',
    'nominal_amount', 'cash_sent_to_date', 'cash_refunded_to_date',
    'counted_cash', 'allocated_to_works',
    'unallocated_cash', 'treatment',
)
ALLOCATION_FIELDS = (
    'report_date', 'contract_id', 'payment_id', 'work_id',
    'allocation_type', 'allocated',
)


def excel_safe(value: str) -> str:
    """Prevent CSV formula execution but preserve legitimate negative money."""
    if re.fullmatch(r'-?\d+,\d{2}', value):
        return value
    if value and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def write_report(report: Report, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows, fields in (
        ('works_report.csv', report.work_rows, WORK_FIELDS),
        ('contracts_report.csv', report.contract_rows, CONTRACT_FIELDS),
        ('payments_audit.csv', report.payment_rows, PAYMENT_FIELDS),
        ('allocation_audit.csv', report.allocation_rows, ALLOCATION_FIELDS),
    ):
        with (out_dir / name).open('w', encoding='utf-8-sig', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=fields, delimiter=';',
                                    lineterminator='\r\n', extrasaction='raise')
            writer.writeheader()
            writer.writerows({key: excel_safe(value) for key, value in row.items()} for row in rows)
