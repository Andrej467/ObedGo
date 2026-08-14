import os
import re
import json
import sys
from datetime import date, datetime, timedelta

import requests
import fitz  # PyMuPDF

MENU_URL = "https://superobed.sk/podnik/4m-restaurant/denne-menu"
SUPABASE_URL = "https://qbwrfortjvzqtdiupgva.supabase.co"
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
PRICE = 6.20
DAYS = ["PONDELOK", "UTOROK", "STREDA", "ŠTVRTOK", "PIATOK"]
DAY_LABELS = ["Pondelok", "Utorok", "Streda", "Štvrtok", "Piatok"]


def fetch_pdf_url():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    })

    r = session.get(MENU_URL, timeout=30, allow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").lower()

    if "application/pdf" in ctype or r.content[:4] == b"%PDF":
        return r.url, r.content

    raise RuntimeError(
        f"Superobed endpoint nevrátil PDF. Content-Type: {ctype or 'unknown'}"
    )


def pdf_text(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return "\n".join(page.get_text("text") for page in doc)


def clean_line(value):
    return re.sub(r"\s+", " ", value).strip()


def split_sections(text):
    lines = [clean_line(x) for x in text.replace("\r", "\n").split("\n")]
    lines = [x for x in lines if x]
    sections = {}
    current = None

    for line in lines:
        m = re.match(r"^(PONDELOK|UTOROK|STREDA|ŠTVRTOK|PIATOK)\s*:\s*(.*)$", line, re.I)
        if m:
            current = m.group(1).upper()
            sections[current] = []
            rest = clean_line(m.group(2))
            if rest:
                sections[current].append(rest)
            continue

        if current:
            if re.match(r"^(ALERGÉNY|ALERGENY|MÄSO|MASO)\s*:", line, re.I):
                current = None
            else:
                sections[current].append(line)

    return sections


def strip_trailing_price(text):
    return re.sub(r"\s+\d+[\.,]\d{2}\s*€.*$", "", text).strip()


def parse_day(lines):
    menu_start = next((i for i, x in enumerate(lines) if re.match(r"^MENU\s*1\s*:", x, re.I)), None)
    if menu_start is None:
        raise ValueError("Chýba MENU 1")

    soup_lines = [
        strip_trailing_price(x)
        for x in lines[:menu_start]
        if not re.match(r"^(PRE VEGETARIÁNOV|PRE VEGETARIANOV)", x, re.I)
    ]
    if len(soup_lines) < 2:
        raise ValueError("Nenašli sa dve polievky")

    soup1, soup2 = soup_lines[0], soup_lines[1]

    meals = []
    current = None
    for x in lines[menu_start:]:
        m = re.match(r"^MENU\s*(\d+)\s*:\s*(.*)$", x, re.I)
        if m:
            if current:
                meals.append(strip_trailing_price(current))
            current = f"MENU {m.group(1)} – {clean_line(m.group(2))}"
        elif current:
            if re.match(r"^(PRE VEGETARIÁNOV|PRE VEGETARIANOV)", x, re.I):
                continue
            current += " " + x

    if current:
        meals.append(strip_trailing_price(current))
    if not meals:
        raise ValueError("Nenašli sa hlavné jedlá")

    return soup1, soup2, meals


def parse_week_range(text):
    compact = clean_line(text[:1500])
    m = re.search(
        r"(\d{1,2})\.(\d{1,2})\.?\s*[-–]\s*(\d{1,2})\.(\d{1,2})\.?(?:\s*(\d{4}))?",
        compact,
    )
    if not m:
        raise ValueError("Nenašiel som rozsah týždňa v hlavičke PDF")

    d1, m1, d2, m2, year = m.groups()
    year = int(year or date.today().year)
    return date(year, int(m1), int(d1)), date(year, int(m2), int(d2))


def validate_current_week(start, end):
    today = datetime.now().astimezone().date()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    if start != monday or end != friday:
        raise RuntimeError(
            f"PDF nie je pre aktuálny pracovný týždeň: {start}–{end}, očakávam {monday}–{friday}"
        )


def upsert_week(start, end, parsed, source_url):
    payload = []

    for idx, day in enumerate(DAYS):
        if day not in parsed:
            raise ValueError(f"Chýba sekcia {day}")

        soup1, soup2, meals = parse_day(parsed[day])
        payload.append({
            "menu_date": (start + timedelta(days=idx)).isoformat(),
            "weekday": DAY_LABELS[idx],
            "soup_1": soup1,
            "soup_2": soup2,
            "meals": meals,
            "price": PRICE,
        })

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/import_weekly_menu",
        headers={
            "apikey": SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "p_week_start": start.isoformat(),
            "p_week_end": end.isoformat(),
            "p_source_url": source_url,
            "p_rows": payload,
        },
        timeout=30,
    )

    if not r.ok:
        raise RuntimeError(f"Supabase import zlyhal: {r.status_code} {r.text}")

    print(json.dumps(r.json(), ensure_ascii=False, indent=2))


def main():
    url, pdf = fetch_pdf_url()
    text = pdf_text(pdf)
    start, end = parse_week_range(text)
    validate_current_week(start, end)
    sections = split_sections(text)

    print("Zdroj:", url)
    print("Týždeň:", start, "-", end)
    print("Nájdené dni:", sorted(sections))

    upsert_week(start, end, sections, url)
    print("ObedGo: týždenné menu bolo úspešne aktualizované.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
