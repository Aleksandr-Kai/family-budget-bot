"""Экспериментальный сервер отчётов для Telegram Mini App.

Отдаёт по HTTP адаптивную HTML-страницу с расходами за отчётный период,
данные собираются заново из базы при каждом запросе. Без авторизации —
временное решение только для теста через туннель, видимое пока только
администратору. Не запускать публично без проверки Telegram initData.
"""

import html
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot

CATEGORY_COLORS = {
    "Продукты": "#3d8b5f",
    "Кафе": "#b06a2a",
    "Транспорт": "#3a6ea5",
    "Дом": "#7d5ba6",
    "Здоровье": "#c0392b",
    "Личное": "#8a6d3b",
    "Собака": "#4a7a8c",
    "Другое": "#6b6b6b",
}

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

STYLE = """
  :root {
    color-scheme: light;
    --bg: #f4f5f3;
    --card: #ffffff;
    --text: #1f2320;
    --muted: #6c7570;
    --border: #e4e6e2;
    --accent: #2f6d4f;
    --accent-bg: #eaf3ee;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    padding: 16px;
    line-height: 1.4;
  }
  .wrap { max-width: 720px; margin: 0 auto; }
  header { padding: 4px 4px 20px; }
  header h1 { font-size: 1.4rem; margin: 0 0 4px; }
  header .sub { color: var(--muted); font-size: 0.9rem; }
  .grand-total {
    margin-top: 16px;
    background: var(--accent-bg);
    border-radius: 14px;
    padding: 16px 20px;
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }
  .grand-total .label { color: var(--accent); font-weight: 600; font-size: 0.95rem; }
  .grand-total .value { color: var(--accent); font-weight: 700; font-size: 1.6rem; }
  .day {
    background: var(--card);
    border-radius: 14px;
    border: 1px solid var(--border);
    margin-bottom: 14px;
    overflow: hidden;
  }
  .day h2 {
    font-size: 1rem;
    margin: 0;
    padding: 14px 18px;
    display: flex;
    align-items: baseline;
    gap: 8px;
    border-bottom: 1px solid var(--border);
    text-transform: capitalize;
  }
  .day h2 .weekday { color: var(--muted); font-weight: 400; font-size: 0.85rem; }
  .day h2 .day-total { margin-left: auto; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; }
  thead th {
    text-align: left;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--muted);
    padding: 8px 18px 6px;
    font-weight: 600;
  }
  th.col-sum, td.col-sum { text-align: right; }
  th.col-who, td.col-who, th.col-time, td.col-time { text-align: right; white-space: nowrap; }
  tbody tr { border-top: 1px solid var(--border); }
  tbody tr:first-child { border-top: none; }
  td { padding: 10px 18px; vertical-align: top; }
  td.col-desc { display: flex; flex-direction: column; gap: 2px; }
  .desc { font-weight: 500; }
  .cat { font-size: 0.72rem; color: var(--cat-color); font-weight: 600; }
  td.col-sum { font-weight: 600; white-space: nowrap; }
  td.col-who, td.col-time { color: var(--muted); font-size: 0.85rem; }

  @media (max-width: 480px) {
    body { padding: 10px; font-size: 0.92rem; }
    header h1 { font-size: 1.2rem; }
    thead th { padding: 6px 10px 5px; font-size: 0.65rem; }
    td { padding: 8px 10px; }
    .cat { font-size: 0.68rem; }
    td.col-who, td.col-time { font-size: 0.8rem; }
  }
  @media (max-width: 360px) {
    th.col-time, td.col-time { display: none; }
  }
"""


def render_day_block(day, items):
    day_total = sum(r["amount"] for r in items)
    rows_html = []
    for r in items:
        category = r["category"] or "Другое"
        color = CATEGORY_COLORS.get(category, "#6b6b6b")
        t = datetime.fromisoformat(r["created_at"]).strftime("%H:%M")
        rows_html.append(f"""
        <tr>
          <td class="col-desc">
            <span class="desc">{html.escape(r['description'])}</span>
            <span class="cat" style="--cat-color:{color}">{html.escape(category)}</span>
          </td>
          <td class="col-who">{html.escape(r['added_by'] or '?')}</td>
          <td class="col-time">{t}</td>
          <td class="col-sum">{bot.fmt(r['amount'])}</td>
        </tr>""")
    weekday = WEEKDAYS[day.weekday()]
    return f"""
    <section class="day">
      <h2>{day.day} {bot.MONTHS_GEN[day.month - 1]} <span class="weekday">· {weekday}</span>
        <span class="day-total">{bot.fmt(day_total)}</span>
      </h2>
      <table>
        <thead>
          <tr><th class="col-desc">Описание</th><th class="col-who">Кто</th><th class="col-time">Время</th><th class="col-sum">Сумма</th></tr>
        </thead>
        <tbody>{''.join(rows_html)}</tbody>
      </table>
    </section>"""


def render_report() -> str:
    since = bot.current_period_start()
    rows = bot.get_expenses(since)
    period_start = datetime.fromisoformat(since)

    by_day = {}
    order = []
    for r in rows:
        day = datetime.fromisoformat(r["created_at"]).date()
        if day not in by_day:
            by_day[day] = []
            order.append(day)
        by_day[day].append(r)

    blocks = "\n".join(render_day_block(d, by_day[d]) for d in order)
    grand_total = sum(r["amount"] for r in rows)
    period_str = period_start.strftime("%d.%m.%Y")

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Расходы за отчётный период</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Расходы за отчётный период</h1>
    <div class="sub">С {period_str} · {len(rows)} записей</div>
  </header>

  {blocks if rows else '<p style="color:var(--muted)">Расходов за текущий период пока нет.</p>'}

  <div class="grand-total">
    <span class="label">Итого за период</span>
    <span class="value">{bot.fmt(grand_total)}</span>
  </div>
</div>
</body>
</html>"""


class ReportHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = render_report().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8792), ReportHandler)
    print("Report server listening on http://127.0.0.1:8792")
    server.serve_forever()
