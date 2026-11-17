"""Запуск: python -m src.cli --date 2026-11-10"""

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from .finance import DataError, build_report, load_data, money, parse_date, sum_money, write_report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Отчёт по оплате работ на заданную дату')
    parser.add_argument('--date', required=True, metavar='YYYY-MM-DD', help='Отчётная дата включительно')
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent.parent / 'data')
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent.parent / 'output')
    args = parser.parse_args(argv)
    try:
        as_of = parse_date(args.date, '--date')
        dataset = load_data(args.data_dir)
        report = build_report(dataset, as_of)
        write_report(report, args.output_dir)
    except (DataError, OSError) as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
        return 2
    def n(value: str) -> Decimal:
        return Decimal(value.replace(',', '.'))
    print(f'Отчёт на {as_of}: договоров — {len(report.contract_rows)}, работ — {len(report.work_rows)}')
    print(f'Фактически выплачено (нетто): {money(sum_money(n(r["cash_paid_net"]) for r in report.contract_rows))} руб.')
    print(f'Осталось по действующим работам: {money(sum_money(n(r["remaining_active_works"]) for r in report.contract_rows))} руб.')
    print(f'  из них по принятым: {money(sum_money(n(r["remaining_accepted"]) for r in report.contract_rows))} руб.')
    print(f'  по будущим/непринятым: {money(sum_money(n(r["remaining_planned"]) for r in report.contract_rows))} руб.')
    print(f'В очереди (не выплачено): {money(sum_money(n(r["queued_not_paid"]) for r in report.contract_rows))} руб.')
    print(f'CSV-файлы: {args.output_dir.resolve()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
