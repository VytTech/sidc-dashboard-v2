# SIDC Daily Overview – V2 (test)

Separate test version. The production dashboard in the parent folder (`build.py`, `fetch_ragic.py`,
`template.html`, `.github/workflows/daily.yml`) and the published GitHub Pages site are not used or changed by V2.

- `v2/fetch_ragic.py` – production fetch + Company / PASSER GROUP Company, request record ids and selected people,
  Human Rotation `Conf.` (name field id 1003165 is labelled `F`).
- `v2/build.py` – production cost formula, yard grouping, history encryption and worker codes unchanged; adds
  exclusion rules, position groups, timesheet FTE staffing (40 h = 1 FTE, max 1 FTE/person/week), request statuses,
  timesheet checks, monthly hours cube and categorised changes.
- `v2/template.html` – Classic / Visual layouts, Operational / Management modes, EN / LT, filters, sticky navigation,
  section PDF (browser print).

## Build

```
py -I v2/fetch_ragic.py v2/raw
py -I v2/build.py v2/raw v2/history v2/site/index.html
```

Environment as in production (`RAGIC_API_KEY`, `RAGIC_BASE`, `PAGE_PASSWORD` / `DATA_KEY`) plus
`MGMT_PASSWORD` – management-view password (only its SHA-256 is written into the page; local default `management`).
The Ragic service account additionally needs read access to Human Rotation (planing/33).

V2 is not deployed; publishing it would need its own workflow step and output path (for example `/v2/`).
