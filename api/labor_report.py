"""
Live HTML labor cost report.

GET /api/labor_report?key=4464&year=2026

Columns: Date | Day | Total Sales | KIT Hrs | KIT $ | KIT % |
         FOH Hrs | FOH $ | FOH % | Total Hrs | Total $ | Total %

KIT % = KIT cost / food sales
FOH % = FOH cost / (total sales - food sales)
Total % = total cost / total sales

Week subtotals show actual vs scheduled hours.
Newest period/week at top. Year tabs FY2023-FY2026.
Auto-refreshes every 5 minutes.

Required env vars:
  SUPABASE_URL, SUPABASE_SERVICE_KEY, REPORT_KEY
"""

import calendar
import json
import os
import re
import urllib.request
from datetime import date, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
REPORT_KEY   = os.environ.get("REPORT_KEY", "")

# ---------------------------------------------------------------------------
# Fiscal calendar (identical to daily_report.py)
# ---------------------------------------------------------------------------
_WEEKS_PER_PERIOD = {
    1: 5, 2: 4, 3: 4,
    4: 5, 5: 4, 6: 4,
    7: 5, 8: 4, 9: 4,
    10: 5, 11: 4, 12: 4,
}

_FY_STARTS = {
    2023: date(2023, 1, 2),
    2024: date(2024, 1, 1),
    2025: date(2024, 12, 30),
    2026: date(2025, 12, 29),
}

AVAILABLE_YEARS = sorted(_FY_STARTS.keys())

_WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
]
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12",
}

_FOOD_CATEGORIES = {
    "Chicken.", "Extra Sauces and Cheese Dips", "Fry Platters",
    "Half Pound Burgers", "Mozzarella Sticks", "Pizza and Flatbreads",
    "Pretzels", "Tater Kegs", "Wraps",
}


def _fy_start(fy: int) -> date:
    return _FY_STARTS[fy]


def _period_bounds(period: int, fy: int) -> tuple:
    week_offset = sum(_WEEKS_PER_PERIOD[p] for p in range(1, period))
    start = _fy_start(fy) + timedelta(weeks=week_offset)
    end   = start + timedelta(weeks=_WEEKS_PER_PERIOD[period]) - timedelta(days=1)
    return start, end


def _current_period(today: date, fy: int) -> int:
    delta = (today - _fy_start(fy)).days
    if delta < 0:
        return 1
    fy_week    = delta // 7
    cumulative = 0
    for p in range(1, 13):
        cumulative += _WEEKS_PER_PERIOD[p]
        if fy_week < cumulative:
            return p
    return 12


def _periods_to_show(fy: int, today: date) -> int:
    current_fy = max(y for y in _FY_STARTS if _fy_start(y) <= today)
    if fy == current_fy:
        return _current_period(today, fy)
    return 12


# ---------------------------------------------------------------------------
# Natural-language questions
# ---------------------------------------------------------------------------

def _normalize_question(question: str) -> str:
    normalized = question.lower().strip()
    for word, number in _NUMBER_WORDS.items():
        normalized = re.sub(rf"\b{word}\b", number, normalized)
    return re.sub(r"\s+", " ", normalized)


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _question_period(question: str, fy: int, today: date) -> tuple:
    q = _normalize_question(question)
    fy_start = _fy_start(fy)
    _, fy_end = _period_bounds(12, fy)
    report_end = min(fy_end, today - timedelta(days=1))
    report_end = max(report_end, fy_start)

    if "yesterday" in q:
        return report_end, report_end
    if "this week" in q or "week to date" in q:
        return max(fy_start, report_end - timedelta(days=report_end.weekday())), report_end
    if "this month" in q or "month to date" in q:
        return max(fy_start, report_end.replace(day=1)), report_end
    if "year to date" in q or "fiscal year to date" in q or "this fiscal year" in q:
        return fy_start, report_end

    duration = None
    patterns = (
        r"(?:last|past|previous|prior)\s+(\d+)\s*(day|week|month|year)s?",
        r"(?:going\s+back|back)\s+(\d+)\s*(day|week|month|year)s?",
        r"(\d+)\s*(day|week|month|year)s?\s*(?:back|ago)",
    )
    for pattern in patterns:
        match = re.search(pattern, q)
        if match:
            duration = (max(1, int(match.group(1))), match.group(2))
            break

    if duration is None:
        match = re.search(r"(?:last|past|previous|prior)\s+(day|week|month|year)\b", q)
        if match:
            duration = (1, match.group(1))

    if duration:
        amount, unit = duration
        if unit == "day":
            period_start = report_end - timedelta(days=amount - 1)
        elif unit == "week":
            period_start = report_end - timedelta(days=amount * 7 - 1)
        elif unit == "month":
            period_start = _subtract_months(report_end, amount) + timedelta(days=1)
        else:
            period_start = _subtract_months(report_end, amount * 12) + timedelta(days=1)
        return max(fy_start, period_start), report_end

    return fy_start, report_end


def _question_weekday(question: str):
    q = _normalize_question(question)
    for index, name in enumerate(_WEEKDAY_NAMES):
        if re.search(rf"\b{name.lower()}s?\b", q):
            return index
    return None


def _question_aggregation(question: str) -> str:
    q = _normalize_question(question)
    if re.search(r"\b(average|avg|mean)\b", q) or "what do we average" in q:
        return "average"
    if re.search(r"\b(highest|best|maximum|max|most)\b", q):
        return "maximum"
    if re.search(r"\b(lowest|worst|minimum|min|least)\b", q):
        return "minimum"
    if re.search(r"\b(sum|total|combined|altogether)\b", q) or "how much" in q:
        return "total"
    return "summary"


def _labor_measure(question: str) -> tuple:
    q = _normalize_question(question)
    if re.search(r"\b(sales|revenue)\b", q):
        return "sales", "tot", "sales", "currency"

    if re.search(r"\b(kit|kitchen|boh|back of house)\b", q):
        department, department_label = "kit", "kitchen"
    elif re.search(r"\b(foh|front of house)\b", q):
        department, department_label = "foh", "FOH"
    else:
        department, department_label = "tot", "total"

    if "scheduled" in q or "schedule hours" in q:
        return "scheduled_hours", department, f"{department_label} scheduled hours", "hours"
    if re.search(r"\b(hours|hour|hrs|hrs\.)\b", q):
        return "hours", department, f"{department_label} labor hours", "hours"
    if re.search(r"\b(cost|costs|payroll|wages|dollars|spend|spent)\b", q):
        return "cost", department, f"{department_label} labor cost", "currency"
    return "percentage", department, f"{department_label} labor percentage", "percentage"


def _format_question_range(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%B %-d, %Y")
    if start.year == end.year:
        return f"{start.strftime('%B %-d')}–{end.strftime('%B %-d, %Y')}"
    return f"{start.strftime('%B %-d, %Y')}–{end.strftime('%B %-d, %Y')}"


def _format_question_value(value: float, unit: str) -> str:
    if unit == "currency":
        return f"${value:,.2f}"
    if unit == "hours":
        return f"{value:,.1f} hours"
    return f"{value:,.1f}%"


def _labor_point(day: date, measure: str, department: str, labor: dict, revenue: dict):
    day_key = day.isoformat()
    rev = revenue.get(day_key, {})
    if measure == "sales":
        return float(rev.get("total", 0.0) or 0.0), None, None

    lab = labor.get(day_key)
    if not lab:
        return None
    prefix = {"kit": "kit", "foh": "foh", "tot": "total"}[department]
    if measure == "hours":
        return float(lab.get(f"{prefix}_hours", 0.0) or 0.0), None, None
    if measure == "scheduled_hours":
        return float(lab.get(f"{prefix}_sched", 0.0) or 0.0), None, None
    if measure == "cost":
        return float(lab.get(f"{prefix}_cost", 0.0) or 0.0), None, None

    cost = float(lab.get(f"{prefix}_cost", 0.0) or 0.0)
    if department == "kit":
        sales_basis = float(rev.get("food", 0.0) or 0.0)
    elif department == "foh":
        sales_basis = max(
            float(rev.get("total", 0.0) or 0.0) - float(rev.get("food", 0.0) or 0.0),
            0.0,
        )
    else:
        sales_basis = float(rev.get("total", 0.0) or 0.0)
    if not sales_basis:
        return None
    return cost / sales_basis * 100, cost, sales_basis


def _answer_labor_question(
    question: str,
    fy: int,
    today: date,
    labor: dict,
    revenue: dict,
) -> str:
    if not question.strip():
        return ""
    q = _normalize_question(question)
    if "why" in q or "forecast" in q or "predict" in q:
        return (
            "I can calculate historical sales and labor performance, but I cannot explain causes or forecast yet. "
            "Try: What was my average total labor percentage on Fridays over the last 8 weeks?"
        )

    measure, department, measure_label, unit = _labor_measure(q)
    source = revenue if measure == "sales" else labor
    if not source:
        return "I could not find the requested data for the selected fiscal year."

    start, end = _question_period(q, fy, today)
    data_dates = sorted(date.fromisoformat(value) for value in source)
    start = max(start, data_dates[0])
    end = min(end, data_dates[-1])
    if start > end:
        return "I could not find the requested data in that date range."

    weekday = _question_weekday(q)
    aggregation = _question_aggregation(q)
    points = []
    current = start
    while current <= end:
        if weekday is None or current.weekday() == weekday:
            point = _labor_point(current, measure, department, labor, revenue)
            if point is not None:
                value, numerator, denominator = point
                points.append((current, value, numerator, denominator))
        current += timedelta(days=1)

    if not points:
        day_name = _WEEKDAY_NAMES[weekday] if weekday is not None else "matching"
        return f"I could not find any {day_name} data for that question and date range."

    range_text = _format_question_range(start, end)
    day_text = f"{_WEEKDAY_NAMES[weekday]}s" if weekday is not None else "days"
    subject = f"{_WEEKDAY_NAMES[weekday]} {measure_label}" if weekday is not None else f"daily {measure_label}"
    values = [point[1] for point in points]
    verb = "were" if measure in ("sales", "hours", "scheduled_hours") else "was"

    if aggregation == "average":
        result = sum(values) / len(values)
        return (
            f"Average {subject} {verb} {_format_question_value(result, unit)} across "
            f"{len(points)} {day_text} from {range_text}."
        )
    if aggregation == "maximum":
        result_date, result, _, _ = max(points, key=lambda item: item[1])
        return (
            f"The highest {subject} {verb} {_format_question_value(result, unit)} on "
            f"{result_date.strftime('%A, %B %-d, %Y')} ({len(points)} {day_text} checked)."
        )
    if aggregation == "minimum":
        result_date, result, _, _ = min(points, key=lambda item: item[1])
        return (
            f"The lowest {subject} {verb} {_format_question_value(result, unit)} on "
            f"{result_date.strftime('%A, %B %-d, %Y')} ({len(points)} {day_text} checked)."
        )

    if unit == "percentage":
        numerator = sum(point[2] or 0.0 for point in points)
        denominator = sum(point[3] or 0.0 for point in points)
        result = numerator / denominator * 100 if denominator else 0.0
        return (
            f"Overall {measure_label} was {_format_question_value(result, unit)} across "
            f"{len(points)} {day_text} from {range_text}."
        )

    total = sum(values)
    if aggregation == "total":
        return (
            f"Combined {measure_label} {verb} {_format_question_value(total, unit)} across "
            f"{len(points)} {day_text} from {range_text}."
        )
    average = total / len(values)
    return (
        f"From {range_text}, {measure_label} totaled {_format_question_value(total, unit)} and "
        f"averaged {_format_question_value(average, unit)} across {len(points)} {day_text}."
    )


def _question_box_html(fy: int, key: str, question: str, answer: str) -> str:
    safe_form_key = escape(key, quote=True)
    safe_question = escape(question, quote=True)
    if answer:
        result_html = (
            '<div class="question-answer" role="status" aria-live="polite">'
            f'<span class="answer-label">Answer</span>{escape(answer)}</div>'
        )
    else:
        result_html = (
            '<p class="question-help">Try: “What are my average sales on Monday going back 3 months?”</p>'
        )
    return f"""<section class="question-box" aria-label="Ask a labor or sales question">
  <form class="question-form" method="get">
    <label class="question-label" for="labor-question">Ask this dashboard</label>
    <input type="hidden" name="key" value="{safe_form_key}">
    <input type="hidden" name="year" value="{fy}">
    <input class="question-input" id="labor-question" name="question" type="text" maxlength="300"
           value="{safe_question}" placeholder="Ask about sales, labor hours, cost, percentages, or date ranges" required>
    <button class="question-submit" type="submit">Ask</button>
  </form>
  {result_html}
</section>"""


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _supa_get(path: str) -> list:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept":        "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _supa_paged(base: str) -> list:
    rows, offset = [], 0
    while True:
        sep   = "&" if "?" in base else "?"
        batch = _supa_get(f"{base}{sep}limit=1000&offset={offset}")
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def _fetch_labor(start: date, end: date) -> dict:
    """Returns {date_str: {kit_hours, kit_sched, kit_cost, foh_hours, foh_sched, foh_cost,
                           total_hours, total_sched, total_cost}}"""
    rows = _supa_paged(
        f"/labor_daily?date=gte.{start}&date=lte.{end}"
        f"&select=date,kit_hours,kit_sched,kit_cost,foh_hours,foh_sched,foh_cost,"
        f"total_hours,total_sched,total_cost"
    )
    return {r["date"]: r for r in rows}


def _fetch_revenue(start: date, end: date) -> dict:
    """Returns {date_str: {food, total}} from sales (GoTab) + ts_events (Tripleseat)."""
    by_date: dict = {}

    # GoTab sales rows
    sales_rows = _supa_paged(
        f"/sales?report_date=gte.{start}&report_date=lte.{end}"
        f"&select=report_date,category,net_sales"
    )
    for row in sales_rows:
        d = row["report_date"]
        if d not in by_date:
            by_date[d] = {"food": 0.0, "total": 0.0}
        amt = float(row.get("net_sales") or 0)
        if row.get("category") in _FOOD_CATEGORIES:
            by_date[d]["food"] = round(by_date[d]["food"] + amt, 2)
        by_date[d]["total"] = round(by_date[d]["total"] + amt, 2)

    # Tripleseat events
    ts_rows = _supa_paged(
        f"/ts_events?event_date=gte.{start}&event_date=lte.{end}"
        f"&deleted_at=is.null&status=in.(DEFINITE,CLOSED,TENTATIVE)"
        f"&select=event_date,actual_amount,food_amount"
    )
    for row in ts_rows:
        d = row["event_date"]
        if d not in by_date:
            by_date[d] = {"food": 0.0, "total": 0.0}
        amt = float(row.get("actual_amount") or 0)
        food = float(row.get("food_amount") or 0)
        by_date[d]["food"] = round(by_date[d]["food"] + food, 2)
        by_date[d]["total"] = round(by_date[d]["total"] + amt, 2)

    return by_date


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_sales(val) -> str:
    if val is None or val == "":
        return ""
    return f"${val:,.0f}"


def _fmt_cost(val) -> str:
    if val is None or val == "":
        return ""
    return f"${val:,.2f}"


def _fmt_hrs(val) -> str:
    if val is None or val == "":
        return ""
    return f"{val:.1f}"


def _fmt_pct(val) -> str:
    if val is None or val == "":
        return ""
    return f"{val:.1f}%"


def _pct(cost, sales) -> str:
    if not sales:
        return ""
    return _fmt_pct(cost / sales * 100)


def _pct_val(cost, sales):
    if not sales:
        return None
    return cost / sales * 100


_PCT_THRESHOLD = {"kit": 20, "foh": 10, "tot": 15}

def _pct_class(val, dept: str) -> str:
    if val is None:
        return ""
    return "pct-high" if val >= _PCT_THRESHOLD[dept] else "pct-low"


def _hrs_display(actual, sched) -> str:
    """Format hours as '42.5 / 48 sched' for subtotal rows."""
    if actual is None:
        return ""
    if sched:
        return f"{actual:.1f} / {sched:.1f}"
    return f"{actual:.1f}"


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _build_rows(fy: int, today: date, labor: dict, revenue: dict) -> list:
    rows        = []
    num_periods = _periods_to_show(fy, today)

    for period in range(num_periods, 0, -1):
        p_start, p_end = _period_bounds(period, fy)
        rows.append({"_type": "period_hdr", "label": f"Period {period}"})

        period_data = []

        for week in range(_WEEKS_PER_PERIOD[period], 0, -1):
            w_start = p_start + timedelta(weeks=week - 1)
            w_end   = w_start + timedelta(days=6)
            rows.append({
                "_type": "week_hdr",
                "label": (
                    f"Period {period}  ·  Week {week}"
                    f"  —  {w_start.strftime('%-m/%-d/%Y')}"
                    f" to {w_end.strftime('%-m/%-d/%Y')}"
                ),
            })

            week_data = []
            for delta in range(7):
                d      = w_start + timedelta(days=delta)
                ds     = d.isoformat()
                is_fut = d > today

                lab = labor.get(ds, {})
                rev = revenue.get(ds, {})

                if is_fut or not lab:
                    row = {
                        "_type": "future" if is_fut else "nodata",
                        "date": ds, "day": _DAYS[d.weekday()],
                    }
                else:
                    food_sales  = rev.get("food",  0.0)
                    total_sales = rev.get("total", 0.0)
                    foh_sales   = max(total_sales - food_sales, 0.0)

                    kit_cost  = float(lab.get("kit_cost")  or 0)
                    foh_cost  = float(lab.get("foh_cost")  or 0)
                    tot_cost  = float(lab.get("total_cost") or 0)
                    kit_hours = float(lab.get("kit_hours") or 0)
                    foh_hours = float(lab.get("foh_hours") or 0)
                    tot_hours = float(lab.get("total_hours") or 0)

                    row = {
                        "_type":       "data",
                        "date":        ds,
                        "day":         _DAYS[d.weekday()],
                        "total_sales": total_sales,
                        "food_sales":  food_sales,
                        "foh_sales":   foh_sales,
                        "kit_hours":   kit_hours,
                        "kit_cost":    kit_cost,
                        "kit_pct":     _pct_val(kit_cost, food_sales),
                        "foh_hours":   foh_hours,
                        "foh_cost":    foh_cost,
                        "foh_pct":     _pct_val(foh_cost, foh_sales),
                        "tot_hours":   tot_hours,
                        "tot_cost":    tot_cost,
                        "tot_pct":     _pct_val(tot_cost, total_sales),
                        # sched for subtotal aggregation
                        "kit_sched":   float(lab.get("kit_sched")   or 0),
                        "foh_sched":   float(lab.get("foh_sched")   or 0),
                        "tot_sched":   float(lab.get("total_sched") or 0),
                    }

                rows.append(row)
                week_data.append(row)
                period_data.append(row)

            rows.append(_subtotal_row(week_data, f"Period {period}  ·  Week {week}  Total", "week_sub"))

        rows.append(_subtotal_row(period_data, f"Period {period}  Total", "period_tot"))

    return rows


def _subtotal_row(data_rows: list, label: str, row_type: str) -> dict:
    def _sum(key):
        return round(sum(float(r.get(key) or 0) for r in data_rows if r.get("_type") == "data"), 2)

    kit_cost  = _sum("kit_cost")
    foh_cost  = _sum("foh_cost")
    tot_cost  = _sum("tot_cost")
    food      = _sum("food_sales")
    foh_s     = _sum("foh_sales")
    total_s   = _sum("total_sales")
    kit_h     = _sum("kit_hours")
    kit_sch   = _sum("kit_sched")
    foh_h     = _sum("foh_hours")
    foh_sch   = _sum("foh_sched")
    tot_h     = _sum("tot_hours")
    tot_sch   = _sum("tot_sched")

    return {
        "_type":      row_type,
        "label":      label,
        "total_sales": total_s,
        "kit_hours":  kit_h,  "kit_sched":  kit_sch,
        "kit_cost":   kit_cost,
        "kit_pct":    _pct_val(kit_cost, food),
        "foh_hours":  foh_h,  "foh_sched":  foh_sch,
        "foh_cost":   foh_cost,
        "foh_pct":    _pct_val(foh_cost, foh_s),
        "tot_hours":  tot_h,  "tot_sched":  tot_sch,
        "tot_cost":   tot_cost,
        "tot_pct":    _pct_val(tot_cost, total_s),
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _tr(row: dict) -> str:
    t = row["_type"]

    if t == "period_hdr":
        return f'<tr class="period-hdr"><td colspan="12">{row["label"]}</td></tr>'

    if t == "week_hdr":
        return f'<tr class="week-hdr"><td colspan="12">{row["label"]}</td></tr>'

    if t in ("future", "nodata"):
        label = "—" if t == "nodata" else ""
        return (
            f'<tr class="{t}">'
            f'<td>{row["date"]}</td><td>{row["day"]}</td>'
            f'<td>{label}</td><td>{label}</td><td>{label}</td><td>{label}</td>'
            f'<td>{label}</td><td>{label}</td><td>{label}</td>'
            f'<td>{label}</td><td>{label}</td><td>{label}</td>'
            f'</tr>'
        )

    if t in ("week_sub", "period_tot"):
        css = "week-sub" if t == "week_sub" else "period-tot"
        kp  = row.get("kit_pct")
        fp  = row.get("foh_pct")
        tp  = row.get("tot_pct")
        return (
            f'<tr class="{css}">'
            f'<td colspan="2">{row["label"]}</td>'
            f'<td>{_fmt_sales(row.get("total_sales"))}</td>'
            f'<td class="hrs">{_hrs_display(row.get("kit_hours"), row.get("kit_sched"))}</td>'
            f'<td>{_fmt_cost(row.get("kit_cost"))}</td>'
            f'<td class="{_pct_class(kp, "kit")}">{_fmt_pct(kp)}</td>'
            f'<td class="hrs">{_hrs_display(row.get("foh_hours"), row.get("foh_sched"))}</td>'
            f'<td>{_fmt_cost(row.get("foh_cost"))}</td>'
            f'<td class="{_pct_class(fp, "foh")}">{_fmt_pct(fp)}</td>'
            f'<td class="hrs">{_hrs_display(row.get("tot_hours"), row.get("tot_sched"))}</td>'
            f'<td>{_fmt_cost(row.get("tot_cost"))}</td>'
            f'<td class="{_pct_class(tp, "tot")}">{_fmt_pct(tp)}</td>'
            f'</tr>'
        )

    # regular data row
    kp = row.get("kit_pct")
    fp = row.get("foh_pct")
    tp = row.get("tot_pct")
    return (
        f'<tr class="data">'
        f'<td>{row["date"]}</td><td>{row["day"]}</td>'
        f'<td>{_fmt_sales(row.get("total_sales"))}</td>'
        f'<td class="hrs">{_fmt_hrs(row.get("kit_hours"))}</td>'
        f'<td>{_fmt_cost(row.get("kit_cost"))}</td>'
        f'<td class="{_pct_class(kp, "kit")}">{_fmt_pct(kp)}</td>'
        f'<td class="hrs">{_fmt_hrs(row.get("foh_hours"))}</td>'
        f'<td>{_fmt_cost(row.get("foh_cost"))}</td>'
        f'<td class="{_pct_class(fp, "foh")}">{_fmt_pct(fp)}</td>'
        f'<td class="hrs">{_fmt_hrs(row.get("tot_hours"))}</td>'
        f'<td>{_fmt_cost(row.get("tot_cost"))}</td>'
        f'<td class="{_pct_class(tp, "tot")}">{_fmt_pct(tp)}</td>'
        f'</tr>'
    )


def _render_html(
    fy: int,
    rows: list,
    today: date,
    key: str,
    question: str = "",
    answer: str = "",
) -> str:
    table_rows = "\n".join(_tr(r) for r in rows)
    cur_period = _periods_to_show(fy, today)
    safe_key   = quote(key, safe="")

    sales_url = escape(
        f"https://sales-variance-agent.vercel.app/api/daily_report?key={safe_key}&year={fy}",
        quote=True,
    )
    labor_url = escape(
        f"https://labor-agent.vercel.app/api/labor_report?key={safe_key}&year={fy}",
        quote=True,
    )
    ids_weekly_meeting_url = "https://ids-weekly-meeting.vercel.app/"
    event_host_url = "https://eventhost-opal.vercel.app/"
    dashboard_options = (
        f'<option value="{sales_url}">Sales Variance</option>'
        f'<option value="{labor_url}" selected>Labor Report</option>'
        f'<option value="{ids_weekly_meeting_url}">IDS Weekly Meeting</option>'
        f'<option value="{event_host_url}">Event Host</option>'
    )

    tabs_html = "".join(
        f'<a href="?key={safe_key}&amp;year={y}" class="tab{"active" if y == fy else ""}">'
        f'FY{y}</a>'
        for y in reversed(AVAILABLE_YEARS)
    )
    question_box = _question_box_html(fy, key, question, answer)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>On Par — Labor Report FY{fy}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  html, body {{
    height: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 13px;
    background: #f0f2f5;
    color: #1a1a2e;
    overflow: hidden;
  }}

  body {{
    display: flex;
    flex-direction: column;
  }}

  header {{
    background: #1b4332;
    color: #fff;
    padding: 0 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 48px;
    flex-shrink: 0;
    box-shadow: 0 2px 8px rgba(0,0,0,.4);
  }}

  header h1 {{ font-size: 15px; font-weight: 600; letter-spacing: .4px; }}
  header span {{ font-size: 12px; opacity: .75; }}

  .header-left {{
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }}

  .dashboard-switcher {{
    min-height: 30px;
    border: 1px solid rgba(255,255,255,.35);
    border-radius: 4px;
    background: #0d2b1e;
    color: #fff;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    padding: 5px 8px;
  }}

  .dashboard-switcher:hover {{ border-color: #95d5b5; }}
  .dashboard-switcher:focus {{ outline: 2px solid #95d5b5; outline-offset: 2px; }}
  .dashboard-switcher option {{ background: #fff; color: #1a1a2e; }}

  .tab-bar {{
    background: #0d2b1e;
    display: flex;
    align-items: center;
    padding: 0 24px;
    height: 38px;
    flex-shrink: 0;
    gap: 4px;
  }}

  .tab {{
    color: #a8d5b5;
    text-decoration: none;
    font-size: 12px;
    font-weight: 600;
    padding: 5px 16px;
    border-radius: 4px;
    letter-spacing: .4px;
    transition: background .15s;
  }}

  .tab:hover {{ background: #1b4332; color: #fff; }}
  .tabactive {{ background: #2d6a4f; color: #fff; }}

  .question-box {{
    background: #fff;
    border-bottom: 1px solid #dfe5e1;
    padding: 12px 24px;
    flex-shrink: 0;
  }}

  .question-form {{
    display: grid;
    grid-template-columns: auto minmax(180px, 720px) auto;
    align-items: center;
    gap: 10px;
  }}

  .question-label {{ font-weight: 700; color: #1b4332; white-space: nowrap; }}

  .question-input {{
    width: 100%;
    min-height: 36px;
    border: 1px solid #aebdb4;
    border-radius: 5px;
    padding: 7px 10px;
    color: #1a1a2e;
    font: inherit;
  }}

  .question-input:focus {{ outline: 2px solid #74c69d; outline-offset: 1px; border-color: #2d6a4f; }}

  .question-submit {{
    min-height: 36px;
    border: 0;
    border-radius: 5px;
    background: #2d6a4f;
    color: #fff;
    cursor: pointer;
    font: inherit;
    font-weight: 700;
    padding: 7px 16px;
  }}

  .question-submit:hover {{ background: #1b4332; }}
  .question-submit:focus {{ outline: 2px solid #1b4332; outline-offset: 2px; }}
  .question-help {{ margin: 7px 0 0; color: #66736b; font-size: 11px; }}

  .question-answer {{
    margin-top: 9px;
    max-width: 1000px;
    border-left: 4px solid #40916c;
    border-radius: 3px;
    background: #edf8f1;
    color: #173c2a;
    padding: 9px 12px;
    line-height: 1.45;
  }}

  .answer-label {{
    display: inline-block;
    margin-right: 8px;
    color: #1b4332;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .5px;
    text-transform: uppercase;
  }}

  .table-scroll {{
    flex: 1;
    min-height: 0;
    overflow: auto;
  }}

  table {{
    width: 100%;
    border-collapse: collapse;
    background: #fff;
  }}

  thead th {{
    background: #2d6a4f;
    color: #fff;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .6px;
    padding: 10px 12px;
    text-align: right;
    white-space: nowrap;
    position: sticky;
    top: 0;
    z-index: 10;
  }}

  thead th.group-kit  {{ background: #1e5f3e; border-left: 2px solid #74c69d; }}
  thead th.group-foh  {{ background: #2a4858; border-left: 2px solid #74b9d4; }}
  thead th.group-tot  {{ background: #3d3d3d; border-left: 2px solid #aaa; }}
  thead th:first-child,
  thead th:nth-child(2) {{ text-align: left; }}

  td {{
    padding: 7px 12px;
    text-align: right;
    border-bottom: 1px solid #eef0f3;
    white-space: nowrap;
  }}

  td:first-child,
  td:nth-child(2) {{ text-align: left; }}

  td.hrs {{ color: #555; font-size: 12px; }}

  td.kit-sep  {{ border-left: 2px solid #d8f3dc; }}
  td.foh-sep  {{ border-left: 2px solid #cce5f0; }}
  td.tot-sep  {{ border-left: 2px solid #ddd; }}

  tr.data:hover {{ background: #f2faf5; }}

  tr.future td, tr.nodata td {{ color: #bbb; background: #fafafa; }}

  tr.period-hdr td {{
    background: #0d2b1e;
    color: #fff;
    font-weight: 700;
    font-size: 12px;
    letter-spacing: .4px;
    padding: 9px 12px;
  }}

  tr.week-hdr td {{
    background: #40916c;
    color: #fff;
    font-weight: 600;
    font-size: 11px;
    padding: 7px 12px;
  }}

  tr.week-sub td {{
    background: #d8f3dc;
    font-weight: 700;
    border-top: 1px solid #95d5a8;
  }}

  tr.period-tot td {{
    background: #b7e4c7;
    font-weight: 700;
    font-size: 13px;
    border-top: 2px solid #74c69d;
  }}

  .pct-low  {{ color: #006100; font-weight: 600; }}
  .pct-high {{ color: #9c0006; font-weight: 600; }}

  .legend {{
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    padding: 6px 24px;
    font-size: 11px;
    background: #f8f9fa;
    border-bottom: 1px solid #e0e0e0;
    flex-shrink: 0;
  }}
  .legend span {{ display: flex; align-items: center; gap: 4px; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  .dot-low  {{ background: #006100; }}
  .dot-mid  {{ background: #c67c00; }}
  .dot-high {{ background: #9c0006; }}

  .updated {{
    text-align: center;
    padding: 10px;
    font-size: 11px;
    color: #888;
    background: #fff;
  }}

  @media (max-width: 760px) {{
    header {{ padding: 0 12px; }}
    header h1 {{ font-size: 13px; }}
    header > span {{ display: none; }}
    .header-left {{ gap: 8px; }}
    .dashboard-switcher {{ max-width: 145px; }}
    .tab-bar {{ padding: 0 12px; }}
    .tab {{ padding: 5px 10px; }}
    .question-box {{ padding: 10px 12px; }}
    .question-form {{ grid-template-columns: 1fr auto; gap: 8px; }}
    .question-label {{ grid-column: 1 / -1; }}
  }}
</style>
</head>
<body>
<header>
  <div class="header-left">
    <select class="dashboard-switcher" aria-label="Switch dashboard" onchange="window.location.href=this.value">
      {dashboard_options}
    </select>
    <h1>On Par Entertainment — Labor Report</h1>
  </div>
  <span>FY{fy} &nbsp;·&nbsp; Period {cur_period} &nbsp;·&nbsp; {today.strftime('%B %-d, %Y')} &nbsp;·&nbsp; Refreshes every 5 min</span>
</header>
<div class="tab-bar">{tabs_html}</div>
{question_box}
<div class="legend">
  <span><span class="dot dot-low"></span> On target</span>
  <span><span class="dot dot-high"></span> Over threshold (KIT &gt;20% · FOH &gt;10% · Total &gt;15%)</span>
  <span style="margin-left:16px;color:#555">KIT % = KIT cost ÷ Food sales &nbsp;·&nbsp; FOH % = FOH cost ÷ (Total − Food) &nbsp;·&nbsp; Total % = Total cost ÷ Total sales</span>
  <span style="margin-left:16px;color:#555">Week subtotals: actual hrs / sched hrs</span>
</div>
<div class="table-scroll">
<table>
  <thead>
    <tr>
      <th rowspan="1">Date</th>
      <th>Day</th>
      <th>Total Sales</th>
      <th class="group-kit">KIT Hrs</th>
      <th class="group-kit">KIT $</th>
      <th class="group-kit">KIT %</th>
      <th class="group-foh">FOH Hrs</th>
      <th class="group-foh">FOH $</th>
      <th class="group-foh">FOH %</th>
      <th class="group-tot">Total Hrs</th>
      <th class="group-tot">Total $</th>
      <th class="group-tot">Total %</th>
    </tr>
  </thead>
  <tbody>
{table_rows}
  </tbody>
</table>
<p class="updated">Source: 7Shifts time punches &nbsp;·&nbsp; Sales from GoTab + Tripleseat &nbsp;·&nbsp; Data refreshes nightly at 6 AM ET</p>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main + Vercel handler
# ---------------------------------------------------------------------------

def run(fy: int, today: date) -> str:
    fy_start    = _fy_start(fy)
    num_periods = _periods_to_show(fy, today)
    _, fy_end   = _period_bounds(num_periods, fy)

    labor   = _fetch_labor(fy_start, fy_end)
    revenue = _fetch_revenue(fy_start, fy_end)
    rows    = _build_rows(fy, today, labor, revenue)
    return _render_html(fy, rows, today, "")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs  = parse_qs(urlparse(self.path).query)
        key = qs.get("key", [""])[0]

        if REPORT_KEY and key != REPORT_KEY:
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        today = date.today()
        try:
            fy_param = int(qs.get("year", ["2026"])[0])
        except ValueError:
            fy_param = 2026
        fy = fy_param if fy_param in _FY_STARTS else 2026

        try:
            fy_start    = _fy_start(fy)
            num_periods = _periods_to_show(fy, today)
            _, fy_end   = _period_bounds(num_periods, fy)

            labor   = _fetch_labor(fy_start, fy_end)
            revenue = _fetch_revenue(fy_start, fy_end)
            rows    = _build_rows(fy, today, labor, revenue)
            question = qs.get("question", [""])[0].strip()[:300]
            answer = (
                _answer_labor_question(question, fy, today, labor, revenue)
                if question else ""
            )
            html = _render_html(fy, rows, today, key, question, answer)

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, format, *args):
        pass
