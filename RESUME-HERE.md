---
project: B-Stock Deal Scout
status: shadow mode, scraper login broken
last_updated: 2026-04-30
---

# RESUME-HERE — B-Stock Deal Scout

> Live on Railway in shadow mode. n8n cron + alert workflows active.

## Last session state

Live on Railway. n8n cron + alert workflows are active. Scraper login flow returns 0 listings (needs auth fix).

---

## Blockers

1. **Fix scraper login flow** — currently returns 0 listings, likely session/cookie auth broken
2. **Promote from shadow mode to production** — only after scraper returns real data
3. **Wire alerts to user-facing channel** — currently logging only

---

## Resume Prompt

```
Read RESUME-HERE.md in Entmarketingteam/bstock-deal-scout and tell me what's
outstanding. Then help me debug the scraper login flow.
```
