"""
Response Quality Annotation App
===============================
Streamlit app for a human validation study. NOTE: user-visible strings and this
source are browsable on a public deployment — keep everything here generic while
the associated submission is under double-blind review (no project name, model
names, or author details).

  - Task 1: item-level judgment replication (human labels vs. automatic-judge labels)
  - Task 2: blinded before/after response pairs (failure category + severity)

Design goals: minimum annotator effort — one screen per item, one click per
question, auto-advance, resume from where you left off.

The app is fully data-driven: metrics, rubrics, questions, and per-annotator
block assignments all come from annotation_data.json (produced by the sampling
script). Adding metrics or changing tiers requires no code changes.

Setup (mirrors the HealthEdit annotation app):
  1. Google service account credentials in credentials.json
     (or env var GOOGLE_CREDENTIALS_JSON — e.g. a HuggingFace Space secret).
  2. Set ANNOTATION_SHEET_ID env var (Google Sheet shared with the service account).
  3. Optional APP_PASSWORD env var.
  4. Run: streamlit run app.py
Without Sheets credentials the app falls back to a local CSV (pilot mode).
"""

import hashlib
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

# ── Configuration ─────────────────────────────────────────────────────────────

DATA_FILE        = Path(__file__).parent / "annotation_data.json"
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
LOCAL_CSV        = Path(__file__).parent / "annotations_local.csv"


def _conf(key: str, default: str = "") -> str:
    """Read config from st.secrets (Streamlit Community Cloud / secrets.toml)
    falling back to environment variables (HF Spaces, local shell)."""
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, default)


GOOGLE_SHEET_ID = _conf("ANNOTATION_SHEET_ID")
APP_PASSWORD    = _conf("APP_PASSWORD")

_creds_env = _conf("GOOGLE_CREDENTIALS_JSON")
if _creds_env and not CREDENTIALS_FILE.exists():
    CREDENTIALS_FILE.write_text(_creds_env)

SHEET_COLUMNS = ["timestamp", "annotator", "item_id", "answers_json"]
WORKSHEET = "annotations"   # single shared tab: lets the app coordinate coverage


# ── Data loading ──────────────────────────────────────────────────────────────

@st.cache_data
def _load_data(_mtime: float):
    with open(DATA_FILE) as f:
        return json.load(f)


def load_data():
    # Key the cache on the file's mtime so a rebuilt data file is picked up
    # without restarting the server (avoids serving stale items).
    return _load_data(DATA_FILE.stat().st_mtime)


TYPE_ORDER = ["claim", "w1b", "h4", "pair"]


def _seed_of(name: str) -> int:
    return int(hashlib.sha256(name.encode()).hexdigest(), 16)


def _round_robin_by_condition(items: list, rng: random.Random) -> list:
    """Interleave across strata so any prefix stays balanced. `strat` is an
    opaque key set by the sampling script (the app never needs to know what the
    strata mean, and the deployed data file deliberately does not say)."""
    buckets = {}
    for it in items:
        buckets.setdefault(it.get("strat", "-"), []).append(it)
    for b in buckets.values():
        rng.shuffle(b)
    out, keys = [], sorted(buckets)
    while any(buckets[k] for k in keys):
        for k in keys:
            if buckets[k]:
                out.append(buckets[k].pop())
    return out


def _blocked_order(items: list, name: str) -> list:
    """Serve in priority order (set by the sampling/patch script, lower first),
    then group by task type so a rubric is read once per block, rotating the
    block order per annotator, and interleave strata within each block."""
    rng = random.Random(_seed_of(name) % (2**32))
    rot = _seed_of(name) % len(TYPE_ORDER)
    type_order = TYPE_ORDER[rot:] + TYPE_ORDER[:rot]
    out = []
    for prio in sorted({it.get("priority", 5) for it in items}):
        tier = [it for it in items if it.get("priority", 5) == prio]
        seen = set()
        for t in type_order + sorted({i["type"] for i in tier}):
            if t in seen:
                continue
            seen.add(t)
            out += _round_robin_by_condition([it for it in tier if it["type"] == t], rng)
    return out


def items_for_annotator(data: dict, name: str, done_by_others: set) -> list:
    """Serving order for a named annotator.

    Shared-link mode (`blocks` in the data file): everyone starts with the common
    block (multi-rater core), then works through the shared pool minus items
    other annotators already covered — so one link can be posted to a group chat
    and coverage self-coordinates. Falls back to fixed per-annotator sequences.
    """
    by_id = {it["id"]: it for it in data["items"]}
    if "blocks" in data:
        common = [by_id[i] for i in data["blocks"]["common"] if i in by_id]
        pool = [by_id[i] for i in data["blocks"]["pool"]
                if i in by_id and i not in done_by_others]
        return _blocked_order(common, name) + _blocked_order(pool, name)
    spec = data.get("annotator_codes", {}).get(name)
    if not spec:
        return []
    return [by_id[i] for i in spec.get("sequence", []) if i in by_id]


# ── Storage backends ──────────────────────────────────────────────────────────

@st.cache_resource
def get_gspread_client():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=scopes)
        return gspread.authorize(creds)
    except FileNotFoundError:
        return None
    except Exception as e:
        st.error(f"Google Sheets auth error: {e}")
        return None


def get_or_create_worksheet(client, _annotator: str = ""):
    """One shared worksheet for all annotators (enables coverage coordination)."""
    import gspread
    sh = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(WORKSHEET)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET, rows=5000, cols=len(SHEET_COLUMNS))
        ws.append_row(SHEET_COLUMNS)
    return ws


def load_existing(ws=None, annotator: str = "") -> tuple[dict, set]:
    """Return (own answers {item_id: answers}, item_ids done by *other* people).

    Storage is append-only, so later rows win for the same (annotator, item).
    """
    own, others = {}, set()

    def consume(rows):
        for row in rows:
            iid, who = str(row.get("item_id", "")), str(row.get("annotator", ""))
            if not iid:
                continue
            if who == annotator:
                try:
                    own[iid] = json.loads(row.get("answers_json", "{}"))
                except json.JSONDecodeError:
                    pass
            else:
                others.add(iid)

    if ws is not None:
        consume(ws.get_all_records())
    elif LOCAL_CSV.exists():
        import csv
        with open(LOCAL_CSV, newline="") as f:
            consume(list(csv.DictReader(f)))
    return own, others


def save_answers(ws, annotator: str, item_id: str, answers: dict):
    """Append-only save (fast; duplicates resolved at analysis: last write wins)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    row = [ts, annotator, item_id, json.dumps(answers, ensure_ascii=False)]
    if ws is not None:
        ws.append_row(row, value_input_option="RAW")
    else:
        import csv
        new_file = not LOCAL_CSV.exists()
        with open(LOCAL_CSV, "a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(SHEET_COLUMNS)
            w.writerow(row)


# ── Question rendering ────────────────────────────────────────────────────────

def show_if_satisfied(q: dict, current: dict) -> bool:
    cond = q.get("show_if")
    if not cond:
        return True
    val = current.get(cond["key"])
    if "equals" in cond:
        return val == cond["equals"]
    if "not" in cond:
        return val is not None and val != cond["not"]
    return True


def render_questions(item: dict, prefill: dict) -> dict:
    """Render the item's questions; returns {key: answer} for answered ones."""
    answers = {}
    for q in item["questions"]:
        if not show_if_satisfied(q, answers):
            continue
        key = f"{item['id']}__{q['key']}"
        default = prefill.get(q["key"])
        if q["type"] == "choice":
            opts = q["options"]
            idx = opts.index(default) if default in opts else None
            val = st.radio(q["label"], opts, index=idx, key=key,
                           horizontal=q.get("horizontal", False))
        elif q["type"] == "scale":
            # Numeric rating rendered as a horizontal radio (no default ->
            # no anchoring bias); mirrors judge rating scales exactly.
            opts = list(range(q.get("min", 1), q.get("max", 10) + 1))
            idx = opts.index(default) if default in opts else None
            val = st.radio(q["label"], opts, index=idx, key=key, horizontal=True)
        elif q["type"] == "text":
            val = st.text_input(q["label"], value=default or "", key=key)
            val = val.strip() or None
        else:
            st.warning(f"Unknown question type: {q['type']}")
            val = None
        if val is not None:
            answers[q["key"]] = val
    return answers


def missing_required(item: dict, answers: dict) -> list:
    out = []
    for q in item["questions"]:
        if not show_if_satisfied(q, answers):
            continue
        if q.get("required", True) and q["type"] != "text" and q["key"] not in answers:
            out.append(q["label"])
    return out


# ── Main app ──────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Response Annotation", page_icon="📝",
                       layout="centered", initial_sidebar_state="expanded")

    data = load_data()

    for key, default in [("authenticated", not APP_PASSWORD), ("code", ""),
                         ("idx", 0), ("ws", None), ("ws_tried", False),
                         ("saved", {}), ("others", set()), ("order", None)]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ── Password gate ──
    if not st.session_state.authenticated:
        st.markdown("## 📝 Response Annotation")
        pw = st.text_input("Password", type="password")
        if st.button("Login", type="primary"):
            if pw == APP_PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        return

    # ── Sidebar: identity + progress ──
    with st.sidebar:
        st.title("📝 Response Annotation")
        shared_mode = "blocks" in data
        if shared_mode:
            raw = st.text_input(
                "Your name or initials",
                value=st.query_params.get("name", ""),
                placeholder="e.g. Alex",
                help="Used only to keep track of your progress — pick anything "
                     "and reuse it if you come back later.")
            code = re.sub(r"[^A-Za-z0-9 _.-]", "", raw).strip()[:40]
        else:
            codes = sorted(data.get("annotator_codes", {}))
            qp = st.query_params.get("code", "")
            if "code_select" not in st.session_state and qp in codes:
                st.session_state.code_select = qp
            code = st.selectbox("Your annotator code", [""] + codes, key="code_select")

        if code != st.session_state.code:
            st.session_state.code = code
            st.session_state.idx = 0
            st.session_state.ws = None
            st.session_state.ws_tried = False
            st.session_state.saved = {}
            st.session_state.others = set()
            st.session_state.order = None

        if not code:
            st.info("👋 Enter your name to begin." if shared_mode
                    else "Select your annotator code to begin.")
            return

        # Storage connection (once per annotator)
        if not st.session_state.ws_tried:
            st.session_state.ws_tried = True
            client = get_gspread_client() if GOOGLE_SHEET_ID else None
            if client is not None:
                try:
                    with st.spinner("Connecting…"):
                        st.session_state.ws = get_or_create_worksheet(client, code)
                except Exception as e:
                    st.error(f"Sheets error: {e}")
            st.session_state.saved, st.session_state.others = load_existing(
                st.session_state.ws, code)
            st.session_state.order = None

        if st.session_state.ws is not None:
            st.success(f"Signed in as **{code}**")
        else:
            st.error("⚠️ No Google Sheet connected — answers are saved to a local "
                     "file and **will be lost** if the app restarts. Fine for "
                     "testing; fix the credentials before running the real study.")

        # Work list: computed once per session so the list doesn't shift while
        # someone is working (others' saves are picked up on reload).
        if st.session_state.order is None:
            st.session_state.order = items_for_annotator(
                data, code, st.session_state.others)
        items = st.session_state.order
        total = len(items)
        n_done = sum(1 for it in items if it["id"] in st.session_state.saved)
        st.markdown(f"**You: {n_done} / {total} on your list**")
        st.progress(n_done / max(total, 1))
        team = len(st.session_state.others) + len(st.session_state.saved)
        st.caption(f"Team coverage: {team} of {len(data['items'])} items annotated. "
                   "Every item you add helps — stop whenever you like.")
        if st.button("🔄 Refresh list", use_container_width=True,
                     help="Pull in what others have done since you started"):
            st.session_state.ws_tried = False
            st.rerun()

        col1, col2 = st.columns(2)
        with col1:
            if st.button("⬅ Prev", use_container_width=True) and st.session_state.idx > 0:
                st.session_state.idx -= 1
                st.rerun()
        with col2:
            if st.button("Next ➡", use_container_width=True) and st.session_state.idx < total - 1:
                st.session_state.idx += 1
                st.rerun()
        if st.button("⏭ First unanswered", use_container_width=True):
            for i, it in enumerate(items):
                if it["id"] not in st.session_state.saved:
                    st.session_state.idx = i
                    st.rerun()
            st.success("All done — thank you! 🎉")

        st.markdown("---")
        st.caption("Answers are saved when you click **Save & Next**. "
                   "You can close the app and continue later — progress is restored.")

    # ── Main pane: one item ──
    items = st.session_state.order or []
    total = len(items)
    if total == 0:
        st.success("Nothing left to annotate — everything is covered. Thank you! 🎉")
        return
    st.session_state.idx = min(st.session_state.idx, total - 1)
    item = items[st.session_state.idx]
    done = item["id"] in st.session_state.saved

    badge = "✅ answered" if done else "⬜ unanswered"
    st.markdown(f"#### Item {st.session_state.idx + 1} / {total} · "
                f"{item.get('metric_label', item['task'])} · {badge}")

    # Rubric: auto-expand the first time this metric appears in the sequence
    first_of_metric = next(i for i, it in enumerate(items)
                           if it.get("metric") == item.get("metric"))
    with st.expander("📖 Instructions for this item type",
                     expanded=(st.session_state.idx == first_of_metric)):
        st.markdown(item["instructions_md"])

    for block in item["context"]:
        st.markdown(f"**{block['label']}**")
        with st.container(border=True, height=min(420, 90 + 22 * block["text"].count("\n") + len(block["text"]) // 4)):
            st.markdown(block["text"])

    st.markdown("---")
    prefill = st.session_state.saved.get(item["id"], {})
    answers = render_questions(item, prefill)

    missing = missing_required(item, answers)
    label = "💾 Save & Next" if not done else "💾 Re-save & Next"
    if st.button(label, type="primary", use_container_width=True):
        if missing:
            st.error("Please answer: " + "; ".join(missing))
        else:
            try:
                save_answers(st.session_state.ws, st.session_state.code,
                             item["id"], answers)
                st.session_state.saved[item["id"]] = answers
                # Auto-advance to next unanswered item
                nxt = next((i for i in range(st.session_state.idx + 1, total)
                            if items[i]["id"] not in st.session_state.saved),
                           None)
                if nxt is None:
                    nxt = next((i for i in range(total)
                                if items[i]["id"] not in st.session_state.saved),
                               st.session_state.idx)
                st.session_state.idx = nxt
                st.rerun()
            except Exception as e:
                st.error(f"Failed to save: {e}")


if __name__ == "__main__":
    main()
