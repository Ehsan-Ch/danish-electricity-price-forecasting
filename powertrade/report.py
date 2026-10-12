"""A portable HTML research report; no server, trackers or external assets."""
import html
import json


def line_chart(series, colors, width=1000, height=230):
    values = [v for s in series for v in s]
    if not values:
        return "<p>No observations.</p>"
    lo, hi = min(values), max(values)
    span = max(hi - lo, 1)
    svg = [f'<svg role="img" aria-label="Time series chart" viewBox="0 0 {width} {height}">']
    for ratio in (0, .5, 1):
        y = 15 + ratio * (height - 45)
        svg.append(f'<path d="M55 {y} H{width-12}" stroke="#dfe6ec"/><text x="0" y="{y+4}" fill="#536676" font-size="12">{hi-ratio*span:.0f}</text>')
    for values, color in zip(series, colors):
        points = " ".join(f"{55+i*(width-70)/max(1,len(values)-1):.1f},{15+(hi-v)*(height-45)/span:.1f}" for i, v in enumerate(values))
        svg.append(f'<polyline points="{points}" stroke="{color}" stroke-width="1.6" fill="none"/>')
    return "".join(svg) + "</svg>"


def render(metrics, predictions, daily, orders):
    cards = "".join(f'<article><small>{html.escape(area)} · MAE</small><strong>€{m["mae"]:.2f}</strong><span>per MWh · weekly baseline €{m["baseline_168h_mae"]:.2f}</span></article>' for area, m in metrics["areas"].items())
    sections = []
    for area, m in metrics["areas"].items():
        rows = [r for r in predictions if r["area"] == area][-168:]
        pnl = [r for r in daily if r["area"] == area]
        cumulative, total = [], 0
        for r in pnl:
            total += r["pnl_eur"]
            cumulative.append(total)
        sections.append(f'''<section><div class="section-head"><h2>{area} / Forecast quality</h2><span>Last seven test days · EUR/MWh</span></div>
        <p><b class="teal">Actual</b> &nbsp; <b class="blue">Ridge forecast</b></p>
        {line_chart([[r['actual'] for r in rows], [r['forecast'] for r in rows]], ['#087f79','#5b6ee1'])}
        <p class="muted">{rows[0]['delivery'][:10]} → {rows[-1]['delivery'][:10]} · RMSE €{m['rmse']:.2f}/MWh · 90% interval observed coverage {m['coverage']:.1%}</p>
        <h2>Battery cash flow</h2>{line_chart([cumulative], ['#087f79'])}
        <p>Net settled paper P&amp;L <b>€{m['paper_pnl_eur']:.2f}</b> · Maximum daily-close drawdown €{m['max_drawdown_eur']:.2f} · Filled orders {m['filled_orders']}</p></section>''')
    order_rows = "".join('<tr>' + ''.join(f'<td>{html.escape(str(v))}</td>' for v in [r['area'],r['delivery'][:16], 'BUY' if r['quantity']>0 else 'SELL',f"{abs(r['quantity']):.3f}",r['status'], '—' if r['fill'] is None else f"{r['fill']:.2f}"]) + '</tr>' for r in orders[-30:])
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>PowerTrade | Danish electricity research</title>
    <style>body{{margin:0;background:#f2f5f6;color:#132e3b;font:15px/1.6 system-ui,sans-serif}}main{{max-width:1080px;margin:auto;padding:40px 24px}}header{{border-top:5px solid #087f79;padding:26px 0}}.eyebrow{{color:#087f79;letter-spacing:2px;font-size:12px;font-weight:750}}h1{{font-size:44px;letter-spacing:-2px;margin:8px 0}}h2{{font-size:21px}}.muted,small,article span{{color:#536676}}.badge{{display:inline-block;background:#deeee9;padding:5px 12px;border-radius:20px;font-size:12px}}.cards{{display:grid;grid-template-columns:repeat(2,1fr);gap:18px}}article,section{{background:white;border:1px solid #dde5e9;border-radius:12px;padding:24px;margin:18px 0}}article strong{{display:block;font-size:36px}}article span{{font-size:13px}}.section-head{{display:flex;justify-content:space-between;align-items:center;gap:12px}}.section-head span{{font-size:12px;color:#536676}}svg{{width:100%;height:auto}}.teal{{color:#087f79}}.blue{{color:#5b6ee1}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:9px;border-bottom:1px solid #e6edef}}.table{{overflow:auto}}details{{margin-top:22px}}pre{{white-space:pre-wrap;font-size:12px}}footer{{font-size:13px;color:#536676}}@media(max-width:620px){{h1{{font-size:34px}}.cards{{grid-template-columns:1fr}}.section-head{{display:block}}main{{padding:20px 14px}}}}</style></head><body><main>
    <header><div class="eyebrow">POWERTRADE / RESEARCH DESK</div><h1>Electricity prices.<br>Accountable positions.</h1><p class="muted">Danish DK1 + DK2 · Forecasting, battery scheduling and a persistent trade ledger</p><span class="badge">Historical paper simulation</span> <span class="badge">{metrics['test_start']} — {metrics['test_end']}</span></header>
    <div class="cards">{cards}</div>{''.join(sections)}
    <section><h2>Trade management / latest 30 orders</h2><div class="table"><table><thead><tr><th>Area</th><th>Delivery UTC</th><th>Side</th><th>MWh</th><th>Status</th><th>Fill €/MWh</th></tr></thead><tbody>{order_rows}</tbody></table></div><p>All trades are simulated. Clearing-price fills assume full execution. Each area uses a separate 2 MW / 8 MWh battery.</p>
    <details><summary>Method, risk limits and evaluation details</summary><pre>{html.escape(json.dumps(metrics, indent=2))}</pre></details></section>
    <footer>Source: <a href="https://www.energidataservice.dk/tso-electricity/DayAheadPrices">Energinet / Energi Data Service</a>. Forecasts use conservatively delayed historical prices. Archived data is not point-in-time revision data. The battery simulation excludes capex, grid charges, taxes, liquidity constraints and imbalance settlement. Hourly blocks approximate four quarter-hour products. Coverage is measured, not guaranteed; temporal dependence weakens conformal assumptions. No live execution.</footer>
    </main></body></html>'''
