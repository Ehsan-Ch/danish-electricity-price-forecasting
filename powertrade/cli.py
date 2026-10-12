import argparse
import json
from pathlib import Path
from .data import fetch, load, parse_time
from .forecast import predict_day
from .pipeline import run, save_csv
from .trading import Ledger, schedule, Limits


def main():
    parser = argparse.ArgumentParser(description="Danish electricity forecasts and battery paper-trade management")
    commands = parser.add_subparsers(dest='command', required=True)
    download = commands.add_parser('download', help='Cache public DK1/DK2 quarter-hour prices')
    download.add_argument('--start', required=True, help='Danish local date inclusive')
    download.add_argument('--end', required=True, help='Danish local date exclusive')
    download.add_argument('--output', default='data/prices.json')
    backtest = commands.add_parser('backtest', help='Evaluate forecasts and simulate risk-approved battery trades')
    backtest.add_argument('--data', default='data/prices.json')
    backtest.add_argument('--output', default='reports/run')
    backtest.add_argument('--days', type=int, default=30)
    forecast = commands.add_parser('forecast', help='Create a forecast and proposed schedule without executing')
    forecast.add_argument('--data', default='data/prices.json')
    forecast.add_argument('--day', required=True, help='Target UTC date')
    forecast.add_argument('--output', default='reports/forecast')
    inspect = commands.add_parser('ledger', help='Inspect persisted order states and battery positions')
    inspect.add_argument('path')
    args = parser.parse_args()
    try:
        if args.command == 'download':
            result = fetch(args.start, args.end, args.output)
        elif args.command == 'backtest':
            result = run(args.data, args.output, args.days)
        elif args.command == 'forecast':
            day = parse_time(args.day)
            if day.hour or day.minute or day.second or day.microsecond:
                raise ValueError('Use a midnight UTC target date')
            output = Path(args.output)
            output.mkdir(parents=True, exist_ok=True)
            predictions, proposals = [], []
            for area, prices in load(args.data).items():
                rows = predict_day(prices, day)
                predictions.extend({'area': area, **r} for r in rows)
                proposals.extend({'area': area, **r} for r in schedule(rows, Limits()))
            save_csv(output / 'forecast.csv', predictions)
            (output / 'proposals.json').write_text(json.dumps(proposals, indent=2), encoding='utf-8')
            result = {'forecast_rows': len(predictions), 'proposed_orders': len(proposals), 'output': str(output)}
        else:
            if not Path(args.path).is_file():
                raise ValueError('Ledger does not exist')
            # Read-only inspection must not create or alter a database.
            import sqlite3
            db = sqlite3.connect(Path(args.path).resolve().as_uri() + '?mode=ro', uri=True)
            db.row_factory = sqlite3.Row
            try:
                result = {'positions': [dict(r) for r in db.execute('SELECT * FROM batteries')],
                          'states': [dict(r) for r in db.execute('SELECT status, COUNT(*) AS count FROM orders GROUP BY status')],
                          'events': db.execute('SELECT COUNT(*) FROM events').fetchone()[0]}
            finally:
                db.close()
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
