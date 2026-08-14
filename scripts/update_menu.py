import os
import re
import json
import sys
import html as html_lib
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF

MENU_URL = "https://superobed.sk/podnik/4m-restaurant/denne-menu"
SUPABASE_URL = "https://qbwrfortjvzqtdiupgva.supabase.co"
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
PRICE = 6.20
DAYS = ["PONDELOK", "UTOROK", "STREDA", "ŠTVRTOK", "PIATOK"]
DAY_LABELS = ["Pondelok", "Utorok", "Streda", "Štvrtok", "Piatok"]


def normalize_candidate(base_url, value):
    if not value:
        return None
    value = html_lib.unescape(value).strip().strip('"\'')
    value = value.replace('\\/', '/')
    if value.startswith('data:') or value.startswith('javascript:') or value.startswith('#'):
        return None
    return urljoin(base_url, value)


def extract_candidates(base_url, raw_html):
    soup = BeautifulSoup(raw_html, "html.parser")
    candidates = []

    for tag in soup.find_all(True):
        for attr in ("href", "src", "data", "poster"):
            value = tag.get(attr)
            if value:
                u = normalize_candidate(base_url, value)
                if u:
                    candidates.append(u)
        srcset = tag.get("srcset")
        if srcset:
            for part in srcset.split(','):
                value = part.strip().split(' ')[0]
                u = normalize_candidate(base_url, value)
                if u:
                    candidates.append(u)

    text = html_lib.unescape(raw_html).replace('\\/', '/')

    patterns = [
        r'https?://[^\s"\'<>]+',
        r'["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        r'["\']([^"\']*(?:menu|jedal|obed)[^"\']*)["\']',
        r'url\(([^)]+)\)',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            value = match if isinstance(match, str) else match[0]
            u = normalize_candidate(base_url, value)
            if u:
                candidates.append(u)

    seen = set()
    out = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def try_pdf(session, url):
    try:
        r = session.get(url, timeout=30, allow_redirects=True)
        ctype = r.headers.get("content-type", "").lower()
        if r.ok and ("application/pdf" in ctype or r.content[:4] == b"%PDF"):
            return r.url, r.content
    except requests.RequestException:
        pass
    return None


def fetch_pdf_url():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    })

    first = session.get(MENU_URL, timeout=30, allow_redirects=True)
    first.raise_for_status()

    pages_to_scan = [(first.url, first.text)]
    candidates = extract_candidates(first.url, first.text)

    # Also follow likely menu/detail links one level deep.
    for u in list(candidates):
        low = u.lower()
        if "superobed.sk" in low and ("denne-menu" in low or "menu" in low):
            try:
                r = session.get(u, timeout=30, allow_redirects=True)
                ctype = r.headers.get("content-type", "").lower()
                if "text/html" in ctype:
                    pages_to_scan.append((r.url, r.text))
            except requests.RequestException:
                pass

    for base, raw in pages_to_scan:
        candidates.extend(extract_candidates(base, raw))

    seen = set()
    candidates = [u for u in candidates if not (u in seen or seen.add(u))]

    # Prefer URLs that look like files/menu assets.
    ranked = sorted(
        candidates,
        key=lambda u: (
            0 if ".pdf" in u.lower() else 1,
            0 if any(k in u.lower() for k in ("menu", "obed", "upload", "file", "document")) else 1,
            len(u),
        ),
    )

    for u in ranked:
        result = try_pdf(session, u)
        if result:
            return result

    print("DEBUG: PDF sa nenašlo. Kandidáti:", file=sys.stderr)
    for u in ranked[:80]:
        print("  ", u, file=sys.stderr)

    raise RuntimeError("Nenašiel som aktuálne PDF menu na stránke Superobed.")


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


def parse_day(lines):
    menu_start = next((i for i, x in enumerate(lines) if re.match(r"^MENU\s*1\s*:", x, re.I)), None)
    if menu_start is None:
        raise ValueError("Chýba MENU 1")

    soup_lines = [
        x for x in lines[:menu_start]
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
                meals.append(current)
            current = f"MENU {m.group(1)} – {clean_line(m.group(2))}"
        elif current:
            if re.match(r"^(PRE VEGETARIÁNOV|PRE VEGETARIANOV)", x, re.I):
                break
            current += " " + x

    if current:
        meals.append(current)
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
