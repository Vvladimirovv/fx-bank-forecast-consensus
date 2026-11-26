#!/usr/bin/env python3
"""
collect.py — find published bank FX forecasts and stage them for review.

Search -> allowlist filter -> fetch -> date check -> LLM extraction
       -> sanity bands -> cross-check -> `pending` sheet in your workbook.

Nothing is ever written to the `forecasts` sheet. You move rows across
yourself once you have glanced at the evidence column.

    export BRAVE_API_KEY=...
    export ANTHROPIC_API_KEY=...
    python collect.py --workbook consensus.xlsx

    python collect.py --bank Rabobank --dry-run   # try one desk, write nothing
"""

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CACHE_PATH = Path(".cache/pages.json")


# ----------------------------------------------------------------- config


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["allowlist"] = {k.lower(): int(v) for k, v in cfg["allowlist"].items()}
    return cfg


class Cache:
    """Page text and extractions, keyed by URL. Keeps reruns cheap."""

    def __init__(self, path, enabled=True):
        self.path, self.enabled = Path(path), enabled
        self.data = {}
        if enabled and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key):
        return self.data.get(key) if self.enabled else None

    def put(self, key, value):
        if not self.enabled:
            return
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


# ----------------------------------------------------------------- search


def domain_of(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower().removeprefix("www.") if m else ""


def weight_of(url, allowlist):
    """Most specific matching entry wins, so think.ing.com beats ing.com."""
    host = domain_of(url)
    matches = [(len(a), w) for a, w in allowlist.items()
               if host == a or host.endswith("." + a)]
    return max(matches)[1] if matches else 0


def search(query, cfg, api_key, pause=True):
    """One Brave query. Swap this function to move to Tavily or Serper."""
    days = cfg["settings"]["max_age_days"]
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    params = {
        "q": query,
        "count": cfg["settings"]["results_per_query"],
        "freshness": f"{since}to{dt.date.today().isoformat()}",
        "result_filter": "web",
    }
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    try:
        r = requests.get(BRAVE_URL, params=params, headers=headers, timeout=25)
        if r.status_code == 429:
            print("    rate limited, waiting 5s")
            time.sleep(5)
            r = requests.get(BRAVE_URL, params=params, headers=headers, timeout=25)
        r.raise_for_status()
        results = (r.json().get("web") or {}).get("results") or []
    except requests.RequestException as e:
        print(f"    search failed: {e}")
        return []
    finally:
        if pause:
            time.sleep(cfg["settings"]["request_pause"])

    return [
        {"url": x.get("url", ""), "title": x.get("title", ""), "age": x.get("page_age")}
        for x in results
        if x.get("url")
    ]


def queries_for(bank, cfg):
    """Phrased the way the source text reads, not the way you'd ask a person."""
    year = dt.date.today().year
    return [
        f'"{bank}" FX forecast EUR/USD {year}',
        f'"{bank}" raises OR lowers OR revises currency forecast',
        f'"{bank}" "we forecast" OR "we now see" exchange rate target',
    ]


# ------------------------------------------------------------------ fetch


def fetch_page(url, cache):
    """Returns (text, published_date_iso) or (None, None)."""
    hit = cache.get("page:" + url)
    if hit is not None:
        return hit.get("text"), hit.get("date")

    try:
        import trafilatura
    except ImportError:
        sys.exit("trafilatura is required:  pip install trafilatura")

    try:
        raw = trafilatura.fetch_url(url)
        if not raw:
            return None, None
        payload = trafilatura.extract(
            raw, output_format="json", with_metadata=True,
            include_comments=False, include_tables=True,
        )
        if not payload:
            return None, None
        meta = json.loads(payload)
        text, date = (meta.get("text") or "")[:18000], meta.get("date")
    except Exception as e:  # noqa: BLE001 - any parser failure is just a skip
        print(f"    fetch failed: {e}")
        return None, None

    cache.put("page:" + url, {"text": text, "date": date})
    return text, date


def age_days(date_iso):
    if not date_iso:
        return None
    try:
        d = dt.date.fromisoformat(str(date_iso)[:10])
    except ValueError:
        return None
    return (dt.date.today() - d).days


# --------------------------------------------------------------- extract


SYSTEM = """You extract currency and commodity forecasts from the text you are given.

Absolute rules:
- Use ONLY the supplied text. Never use anything you know about these banks.
- If a specific number is not stated in the text, return an empty list.
- Do not turn a qualitative view into a number. "we see the euro higher" is
  not a forecast. "fair value is drifting toward 1.20" is not a target.
- Only report a forecast attributed to the named bank. Ignore the article
  author's own view and any other institution mentioned in passing.
- A forecast for EUR-CHF or EUR-GBP is a different instrument, not EUR/USD.
  Report the instrument exactly as the text expresses it.

Return JSON only, no prose, no code fences:

{"forecasts": [
  {"bank": "...", "pair": "EUR/USD", "horizon": "12M", "target": 1.18,
   "bias": "bullish|bearish|neutral",
   "evidence": "the verbatim sentence the number came from"}
]}

`horizon` must be one of the allowed values listed in the user message, or
the string "other" if the text uses a period that is not on that list.
`bias` describes the bank's view on the base currency; use "neutral" if the
text does not say. `evidence` must be copied word for word from the text."""


def llm_extract(bank, text, cfg, api_key, cache, url):
    key = "x:" + hashlib.sha1((bank + url + text[:500]).encode()).hexdigest()
    hit = cache.get(key)
    if hit is not None:
        return hit

    prompt = (
        f"Bank to extract for: {bank}\n"
        f"Allowed instruments: {', '.join(cfg['instruments'])}\n"
        f"Allowed horizons: {', '.join(cfg['horizons'])}\n\n"
        f"--- TEXT ---\n{text}"
    )
    body = {
        "model": cfg["settings"]["model"],
        "max_tokens": 2000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=90)
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
    except requests.RequestException as e:
        print(f"    extraction failed: {e}")
        return []

    out = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
    try:
        rows = json.loads(out).get("forecasts", [])
    except json.JSONDecodeError:
        print("    model did not return valid JSON, skipping")
        return []

    cache.put(key, rows)
    return rows


# -------------------------------------------------------------- validate


def norm_pair(raw):
    s = re.sub(r"\s+", "", str(raw or "")).upper().replace("-", "/").replace("_", "/")
    if s in ("XAU", "GOLD", "XAUUSD"):
        return "XAU/USD"
    if s in ("CL", "CRUDE", "WTICRUDE", "USOIL"):
        return "WTI"
    if re.fullmatch(r"[A-Z]{6}", s):
        s = s[:3] + "/" + s[3:]
    return s


INVERT = {"USD/EUR": "EUR/USD", "USD/GBP": "GBP/USD",
          "JPY/USD": "USD/JPY", "USD/AUD": "AUD/USD", "USD/NZD": "NZD/USD"}


def sane(row, cfg):
    """Returns (ok, reason). Fixes inverted quoting where it is unambiguous."""
    pair = norm_pair(row.get("pair"))
    target = row.get("target")

    if not isinstance(target, (int, float)):
        try:
            target = float(str(target).replace(",", "."))
        except (TypeError, ValueError):
            return False, "target is not a number"

    if pair in INVERT and target:
        pair, target = INVERT[pair], round(1 / target, 6)

    band = cfg["instruments"].get(pair)
    if band is None:
        return False, f"instrument not tracked: {pair}"
    if not band[0] <= target <= band[1]:
        return False, f"{target} outside plausible band for {pair}"
    if row.get("horizon") not in cfg["horizons"]:
        return False, f"unmapped horizon: {row.get('horizon')}"
    if len(str(row.get("evidence", ""))) < 25:
        return False, "no usable evidence sentence"

    row["pair"], row["target"] = pair, target
    return True, ""


def cross_check(rows, cfg):
    """Group identical calls, then label rather than delete."""
    tol = cfg["settings"]["same_target_pct"] / 100
    groups = {}
    for r in rows:
        # log buckets so the tolerance stays proportional across
        # instruments quoted at 1.18 and at 4,400.
        bucket = round(math.log(r["target"]) / tol) if r["target"] > 0 else 0
        key = (r["bank"].lower(), r["pair"], r["horizon"], bucket)
        groups.setdefault(key, []).append(r)

    out = []
    for members in groups.values():
        best = max(members, key=lambda x: (x["weight"], -(x["age"] or 999)))
        domains = {d["domain"] for d in members}
        if best["weight"] >= 3:
            status = "primary source"
        elif len(domains) >= 2:
            status = f"confirmed by {len(domains)} domains"
        elif best["weight"] == 2:
            status = "single tier-1 source"
        else:
            status = "quarantine — weak source"
        best["status"] = status
        best["seen_on"] = ", ".join(sorted(domains))
        out.append(best)

    order = {"primary source": 0}
    return sorted(out, key=lambda r: (order.get(r["status"], 1), r["bank"], r["pair"]))


# ----------------------------------------------------------------- output


def existing_keys(workbook):
    """Rows already approved, so the same call is not staged twice."""
    if not Path(workbook).exists():
        return set()
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("openpyxl is required:  pip install openpyxl")

    wb = load_workbook(workbook, read_only=True, data_only=True)
    if "forecasts" not in wb.sheetnames:
        return set()
    ws = wb["forecasts"]
    rows = ws.iter_rows(values_only=True)
    header = [str(c or "").strip().lower() for c in next(rows, [])]
    try:
        i_bank, i_pair = header.index("bank"), header.index("pair")
        i_hor, i_tgt = header.index("horizon"), header.index("target")
    except ValueError:
        return set()

    keys = set()
    for row in rows:
        try:
            keys.add((str(row[i_bank]).lower(), norm_pair(row[i_pair]),
                      str(row[i_hor]), round(float(row[i_tgt]), 4)))
        except (TypeError, ValueError, IndexError):
            continue
    return keys


COLUMNS = ["collected", "bank", "date", "pair", "horizon", "target", "bias",
           "status", "source", "seen_on", "evidence", "note"]


def _iso_week(date_value):
    """(year, week) for a date-ish cell value, or None if unparseable/blank."""
    try:
        return dt.date.fromisoformat(str(date_value)[:10]).isocalendar()[:2]
    except (TypeError, ValueError):
        return None


def write_pending(workbook, rows):
    """Appends this run's staged rows to `pending`, stamped with today's date.

    `pending` is a running weekly log, not a snapshot: rows from earlier runs
    are kept so you can see how a bank's view moved over time, not just the
    latest read. The only guard is against re-staging the exact same call
    (bank, pair, horizon, target) again within the same ISO week, so running
    collect.py twice in one day doesn't duplicate rows. Never touches
    `forecasts` or `cot` — those stay owner-only.
    """
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font

    path = Path(workbook)
    if path.exists():
        wb = load_workbook(path)
    else:
        wb = Workbook()
        wb.remove(wb.active)
        wb.create_sheet("forecasts").append(
            ["bank", "date", "pair", "horizon", "target", "bias", "note"])

    today = dt.date.today()
    this_week = today.isocalendar()[:2]
    seen_this_week = set()

    if "pending" not in wb.sheetnames:
        ws = wb.create_sheet("pending")
        ws.append([c.replace("_", " ") for c in COLUMNS])
        for cell in ws[1]:
            cell.font = Font(bold=True)
    else:
        ws = wb["pending"]
        header = [str(c or "").strip().lower() for c in
                  next(ws.iter_rows(min_row=1, max_row=1, values_only=True), [])]
        if header[:1] != ["collected"]:
            # older pending sheet, predates this column — add it rather than
            # guess a collection date for rows already sitting there
            ws.insert_cols(1)
            ws.cell(row=1, column=1, value="collected").font = Font(bold=True)

        for row in ws.iter_rows(min_row=2, values_only=True):
            if len(row) < 6:
                continue
            collected, bank, pair, horizon, target = row[0], row[1], row[3], row[4], row[5]
            try:
                key = (str(bank).lower(), pair, horizon, round(float(target), 4))
            except (TypeError, ValueError):
                continue
            # a blank `collected` (pre-migration row) is dated unknown, so
            # always treat it as already-seen rather than risk a duplicate
            if not collected or _iso_week(collected) == this_week:
                seen_this_week.add(key)

    added = 0
    for r in rows:
        key = (r["bank"].lower(), r["pair"], r["horizon"], round(r["target"], 4))
        if key in seen_this_week:
            continue
        ws.append([
            today.isoformat(), r["bank"], r.get("date") or "", r["pair"], r["horizon"], r["target"],
            r.get("bias", ""), r["status"], r["url"], r["seen_on"],
            r.get("evidence", ""), "",
        ])
        added += 1

    for col, width in zip("ABCDEFGHIJKL", [12, 22, 12, 10, 10, 10, 10, 26, 46, 30, 90, 16]):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2, min_col=11, max_col=11):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")

    wb.save(path)
    return added


# ------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description="Stage published bank FX forecasts for review.")
    ap.add_argument("--config", default="sources.yaml")
    ap.add_argument("--workbook", default="consensus.xlsx")
    ap.add_argument("--bank", action="append", help="limit to one desk; repeatable")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    brave = os.environ.get("BRAVE_API_KEY")
    claude = os.environ.get("ANTHROPIC_API_KEY")
    if not brave or not claude:
        sys.exit("Set BRAVE_API_KEY and ANTHROPIC_API_KEY first.")

    cfg = load_config(args.config)
    cache = Cache(CACHE_PATH, enabled=not args.no_cache)
    banks = args.bank or cfg["banks"]
    known = existing_keys(args.workbook)
    max_age = cfg["settings"]["max_age_days"]

    candidates, rejected, seen_urls = [], [], set()

    for bank in banks:
        print(f"\n{bank}")
        hits = []
        for q in queries_for(bank, cfg):
            for h in search(q, cfg, brave):
                w = weight_of(h["url"], cfg["allowlist"])
                if w and h["url"] not in seen_urls:
                    seen_urls.add(h["url"])
                    hits.append({**h, "weight": w})

        hits.sort(key=lambda h: -h["weight"])
        hits = hits[: cfg["settings"]["pages_per_bank"]]
        if not hits:
            print("  nothing on the allowlist")
            continue

        for h in hits:
            host = domain_of(h["url"])
            text, date = fetch_page(h["url"], cache)
            if not text or len(text) < 400:
                print(f"  {host} — no readable text")
                continue

            age = age_days(date)
            if age is None:
                print(f"  {host} — no publish date, skipped")
                rejected.append((h["url"], "no publish date"))
                continue
            if age > max_age:
                print(f"  {host} — {age} days old, skipped")
                continue

            rows = llm_extract(bank, text, cfg, claude, cache, h["url"])
            kept = 0
            for r in rows:
                if str(r.get("bank", "")).lower()[:6] not in bank.lower():
                    if bank.lower()[:6] not in str(r.get("bank", "")).lower():
                        continue
                ok, why = sane(r, cfg)
                if not ok:
                    rejected.append((h["url"], f"{r.get('pair')} {r.get('target')}: {why}"))
                    continue
                key = (bank.lower(), r["pair"], r["horizon"], round(r["target"], 4))
                if key in known:
                    continue
                r.update(bank=bank, url=h["url"], domain=host,
                         weight=h["weight"], age=age, date=str(date)[:10])
                candidates.append(r)
                kept += 1
            print(f"  {host} — {age}d old, {kept} kept of {len(rows)}")

    final = cross_check(candidates, cfg)

    print(f"\n{'-' * 62}")
    print(f"{len(final)} forecasts staged, {len(rejected)} rejected")
    for r in final:
        print(f"  {r['bank']:<18} {r['pair']:<9} {r['horizon']:<7} "
              f"{r['target']:<9} {r['status']}")
    if rejected:
        print("\nrejected:")
        for url, why in rejected[:15]:
            print(f"  {domain_of(url):<28} {why}")

    if args.dry_run:
        print("\ndry run, workbook untouched")
        return
    if final:
        added = write_pending(args.workbook, final)
        skipped = len(final) - added
        note = f" ({skipped} already recorded this week, skipped)" if skipped else ""
        print(f"\n{added} new row(s) appended to the `pending` sheet of {args.workbook}{note}.")
        print("Read the evidence column, then move the rows you trust into `forecasts`.")


if __name__ == "__main__":
    main()
