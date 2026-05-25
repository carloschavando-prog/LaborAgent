# Labor Agent — On Par Entertainment

Live report: https://labor-agent.vercel.app/api/labor_report?key=4464
Nightly fetch cron: https://labor-agent.vercel.app/api/labor_fetch (runs 6 AM ET daily)

---

## What It Does

- Pulls actual worked hours/cost and scheduled hours from 7Shifts nightly
- Stores one row per day in the `labor_daily` Supabase table
- Serves a live HTML report breaking labor into KIT (Kitchen) and FOH departments
- Shows labor cost as % of revenue (KIT% vs food sales, FOH% vs non-food sales, Total% vs total sales)
- Revenue is pulled live from `sales` (GoTab) and `ts_events` (Tripleseat) Supabase tables

---

## GitHub Repo

https://github.com/carloschavando-prog/LaborAgent

Files:
- `api/labor_fetch.py` — nightly 7Shifts data pull (Vercel cron)
- `api/labor_report.py` — live HTML report endpoint
- `vercel.json` — cron schedule config

---

## Vercel Deployment

Project: **labor-agent** (under Conquistador team)
Project ID: `prj_Clspqql3u4wqezSYOMfQe2hAIsmQ`
Team ID: `team_64jN19SrS25NZP9kvlqtn1iW`
Production URL: https://labor-agent.vercel.app
Connected to: `carloschavando-prog/LaborAgent` GitHub repo (branch: main)

### Environment Variables (set in Vercel → labor-agent → Settings → Environment Variables)

| Variable | Description |
|---|---|
| `SUPABASE_URL` | `https://jrzfczhsqshejnrxgmuq.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Service role JWT from Supabase Settings → API |
| `SEVEN_SHIFTS_TOKEN` | Raw hex Bearer token from 7Shifts Developer Tools |
| `CRON_SECRET` | Auto-generated secret for securing cron endpoint |
| `REPORT_KEY` | `4464` — query param key required to view report |

---

## 7Shifts API

### Authentication — CRITICAL

The Bearer token is the **raw hex string** copied from the 7Shifts Developer Tools page.
Do NOT decode it to UUID. Use it verbatim:

```
Authorization: Bearer 32323838303063312d653932642d343131652d613065632d343834636265646439306664
```

- Company ID: `286488`
- Location ID: `354876`
- No token exchange or OAuth flow needed — the hex string IS the token

### Hours & Wages Endpoint

```
GET https://api.7shifts.com/v2/reports/hours_and_wages
  ?company_id=286488
  &location_id=354876
  &from=YYYY-MM-DD
  &to=YYYY-MM-DD
  &punches=true      ← actual worked hours
  &punches=false     ← scheduled hours
```

Response structure: `users[] → weeks[] → shifts[]`
Each shift has: `date` (YYYY-MM-DD HH:MM:SS), `role_id`, `total.total_hours`, `total.total_pay`

All punches are counted including unapproved.

### Role → Department Mapping

| Role ID | Role Name | Department |
|---|---|---|
| 1760490 | Beer Wall Ambassador | FOH |
| 1760491 | Manager | **EXCLUDE** (salaried, $0 in 7Shifts) |
| 1760495 | Line Cook | KIT |
| 1760496 | Dishwasher | KIT |
| 1761419 | Security | FOH |
| 1780081 | EXPO | KIT |
| 2045831 | STAR BWA | FOH |
| 2103857 | Shift Supervisor | KIT |
| 2215217 | Marketing Assistant | FOH |
| 2332059 | Content Creation | KIT |
| 2686259 | Events | KIT |
| 2754779 | Kitchen | KIT |

**CLEANER dept (545687):** No staff assigned — external vendor, ignore entirely.
**Manager (1760491):** Always excluded — paid salary through external payroll, $0 in 7Shifts.

### Departments

- FOH: `535748`
- KITCHEN: `535749`
- CLEANER: `545687` (unused)

---

## Supabase

URL: `https://jrzfczhsqshejnrxgmuq.supabase.co`

### labor_daily Table Schema

```sql
CREATE TABLE IF NOT EXISTS labor_daily (
    date          DATE PRIMARY KEY,
    kit_hours     NUMERIC(8,2)  NOT NULL DEFAULT 0,
    kit_sched     NUMERIC(8,2)  NOT NULL DEFAULT 0,
    kit_cost      NUMERIC(10,2) NOT NULL DEFAULT 0,
    foh_hours     NUMERIC(8,2)  NOT NULL DEFAULT 0,
    foh_sched     NUMERIC(8,2)  NOT NULL DEFAULT 0,
    foh_cost      NUMERIC(10,2) NOT NULL DEFAULT 0,
    total_hours   NUMERIC(8,2)  NOT NULL DEFAULT 0,
    total_sched   NUMERIC(8,2)  NOT NULL DEFAULT 0,
    total_cost    NUMERIC(10,2) NOT NULL DEFAULT 0,
    fetched_at    TIMESTAMPTZ DEFAULT NOW()
);
```

Backfill was completed for Dec 29, 2025 → May 24, 2026 (147 days, 0 errors).

### Revenue Source Tables

The report reads revenue directly from:
- `sales` — GoTab POS data (row per product sale, has `report_date`, `category`, `net_sales`)
- `ts_events` — Tripleseat events (has `event_date`, `actual_amount`, filters `deleted_at=is.null`)

Food categories (used to calculate KIT% denominator):
`Chicken.`, `Extra Sauces and Cheese Dips`, `Fry Platters`, `Half Pound Burgers`,
`Mozzarella Sticks`, `Pizza and Flatbreads`, `Pretzels`, `Tater Kegs`, `Wraps`

---

## Labor % Formulas

- **KIT %** = KIT cost ÷ Food Sales (GoTab food categories only)
- **FOH %** = FOH cost ÷ (Total Sales − Food Sales)
- **Total %** = Total cost ÷ Total Sales

Color coding: green = on target, red = over threshold (KIT >20%, FOH >10%, Total >15%)

---

## Report Columns

Date | Day | Total Sales | KIT Hrs | KIT $ | KIT % | FOH Hrs | FOH $ | FOH % | Total Hrs | Total $ | Total %

Week subtotals show hours as `actual / scheduled` (e.g., `287.0 / 310.0`).
Periods shown newest-first. Year tabs FY2023–FY2026. Auto-refreshes every 5 minutes.

---

## Fiscal Calendar

FY2026 starts December 29, 2025. 5-4-4 quarter pattern:
- Periods 1, 4, 7, 10 = 5 weeks
- All other periods = 4 weeks

| FY | Start Date |
|---|---|
| 2023 | Jan 2, 2023 |
| 2024 | Jan 1, 2024 |
| 2025 | Dec 30, 2024 |
| 2026 | Dec 29, 2025 |

---

## Manual Backfill

Hit this URL for any missed date:
```
https://labor-agent.vercel.app/api/labor_fetch?date=YYYY-MM-DD
```

---

## Cron Schedule

| Cron | Time (UTC) | Time (ET) | What |
|---|---|---|---|
| `/api/labor_fetch` | 10:00 | 6:00 AM | Pull prior day labor from 7Shifts |

---

## Tech Notes

- Python stdlib only — `urllib.request` for all HTTP, no `requests` library
- Vercel serverless: `class handler(BaseHTTPRequestHandler)` pattern
- Supabase upsert: `Prefer: resolution=merge-duplicates`
- Rate limit: 7Shifts allows 10 requests/second per token
