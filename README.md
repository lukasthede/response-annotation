---
title: Response Quality Annotation
emoji: 📝
colorFrom: indigo
colorTo: gray
sdk: streamlit
sdk_version: 1.40.0
app_file: app.py
pinned: false
short_description: Short annotation task for a research study
---

# Annotation tool

Streamlit app for a human validation study. Annotators open one shared
link, enter a name, and work through as many items as they like; progress is stored
in a Google Sheet so people can stop and resume, and the app hands out work so two
annotators don't duplicate each other outside the shared core block.

> **Anonymity note (double-blind review):** this Space is intentionally generic —
> no paper title, model names, or author/institution names in the UI, the Space
> title, or the data file. Keep it that way while the submission is under review.

## Files

| File | Purpose |
|---|---|
| `app.py` | the app (no changes needed between studies) |
| `annotation_data.json` | items, rubrics, questions, common block + pool (built by `../scripts/build_annotation_data.py`) |
| `requirements.txt` | streamlit, gspread, google-auth |

## Space secrets (Settings → Variables and secrets)

| Secret | Value |
|---|---|
| `GOOGLE_CREDENTIALS_JSON` | full service-account key JSON (paste the file contents) |
| `ANNOTATION_SHEET_ID` | the ID from the Sheet URL (`/spreadsheets/d/<ID>/edit`) |
| `APP_PASSWORD` | any short password; share it alongside the link |

Without the first two the app still runs but writes to a local CSV that is **lost
on restart** — fine for a pilot, never for the real study.

## Annotator flow

1. Open link → enter password → type a name (any name; reuse it to resume).
2. **Common block first** (~30 min, everyone annotates the same items → gives
   inter-annotator agreement).
3. Then the shared pool: items nobody has done yet, ordered so each stretch keeps
   one rubric and stays balanced across the conditions being compared.
4. Stop at any time — every saved item counts. "🔄 Refresh list" pulls in what
   others have finished since you started.

## Storage format

One shared worksheet named `annotations`, append-only, columns:
`timestamp, annotator, item_id, answers_json`. Later rows win for the same
(annotator, item_id). Item metadata (condition, judge label, pair mapping) lives
only in `annotation_data.json` and is joined offline at analysis time — annotators
never see it.

## Local development

```bash
pip install -r requirements.txt
streamlit run app.py            # add ?name=Test to the URL to skip typing
```
`python ../scripts/build_annotation_data.py` rebuilds the item file; the app picks
up changes automatically (cache keyed on file mtime).
