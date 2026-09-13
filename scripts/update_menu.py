import os
import re
import json
import sys
from datetime import date, datetime, timedelta

import requests
import fitz

MENU_URL = "https://superobed.sk/podnik/4m-restaurant/denne-menu"
SUPABASE_URL = "https://qbwrfortjvzqtdiupgva.supabase.co"
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
PRICE = 6.20
DAYS = ["PONDELOK", "UTOROK", "STREDA", "ŠTVRTOK", "PIATOK"]
DAY_LABELS = ["Pondelok", "Utorok", "Streda", "Štvrtok", "Piatok"]

def fetch_pdf_url():
    session=requests.Session();session.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36","Accept-Language":"sk-SK,sk;q=0.9,en;q=0.8"})
    r=session.get(MENU_URL,timeout=30,allow_redirects=True);r.raise_for_status();ctype=r.headers.get("content-type","").lower()
    if "application/pdf" in ctype or r.content[:4]==b"%PDF": return r.url,r.content
    raise RuntimeError(f"Superobed endpoint nevrátil PDF. Content-Type: {ctype or 'unknown'}")

def pdf_text(pdf_bytes):
    doc=fitz.open(stream=pdf_bytes,filetype="pdf");return "\n".join(page.get_text("text") for page in doc)
def clean_line(value): return re.sub(r"\s+"," ",value).strip()
def split_sections(text):
    lines=[clean_line(x) for x in text.replace("\r","\n").split("\n")];lines=[x for x in lines if x];sections={};current=None
    for line in lines:
        m=re.match(r"^(PONDELOK|UTOROK|STREDA|ŠTVRTOK|PIATOK)\s*:\s*(.*)$",line,re.I)
        if m:
            current=m.group(1).upper();sections[current]=[];rest=clean_line(m.group(2));
            if rest: sections[current].append(rest)
            continue
        if current:
            if re.match(r"^(ALERGÉNY|ALERGENY|MÄSO|MASO)\s*:",line,re.I): current=None
            else: sections[current].append(line)
    return sections
def strip_trailing_price(text): return re.sub(r"\s+\d+[\.,]\d{2}\s*€.*$","",text).strip()
def parse_day(lines):
    menu_start=next((i for i,x in enumerate(lines) if re.match(r"^MENU\s*1\b",x,re.I)),None)
    if menu_start is None: raise ValueError("Chýba MENU 1")
    soup_lines=[strip_trailing_price(x) for x in lines[:menu_start] if not re.match(r"^(PRE VEGETARIÁNOV|PRE VEGETARIANOV)",x,re.I)]
    soup_lines=[x for x in soup_lines if x]
    if len(soup_lines)<2: raise ValueError("Nenašli sa dve polievky")
    soup1,soup2=soup_lines[0],soup_lines[1];meals=[];current=None
    for x in lines[menu_start:]:
        m=re.match(r"^MENU\s*(\d+)\s*(?::|[-–])?\s*(.*)$",x,re.I)
        if m:
            if current: meals.append(strip_trailing_price(current))
            current=f"MENU {m.group(1)} – {clean_line(m.group(2))}"
        elif current:
            if re.match(r"^(PRE VEGETARIÁNOV|PRE VEGETARIANOV)",x,re.I): continue
            current+=" "+x
    if current: meals.append(strip_trailing_price(current))
    if not meals: raise ValueError("Nenašli sa hlavné jedlá")
    return soup1,soup2,meals

def expected_week():
    today=datetime.now().astimezone().date()
    monday=today+timedelta(days=(7-today.weekday())) if today.weekday()>=5 else today-timedelta(days=today.weekday())
    return monday,monday+timedelta(days=4)

def parse_week_range(text):
    compact=clean_line(text[:2500])
    pattern=r"(?<!\d)(\d{1,2})\.(\d{1,2})\.?\s*[-–]\s*(\d{1,2})\.(\d{1,2})\.?(?:\s*(\d{2,4}))?"
    current_year=date.today().year
    candidates=[]
    for m in re.finditer(pattern,compact):
        d1,m1,d2,m2,year=m.groups()
        if year:
            year=int(year)
            if year<100: year+=2000
        else:
            year=current_year
        try:
            start=date(year,int(m1),int(d1))
            end_year=year+1 if int(m2)<int(m1) else year
            end=date(end_year,int(m2),int(d2))
        except ValueError:
            continue
        if end < start or (end-start).days > 7:
            continue
        candidates.append((start,end))
    exp_start,exp_end=expected_week()
    if not candidates:
        print(f"WARN: rozsah dátumov v PDF sa nepodarilo spoľahlivo prečítať; používam očakávaný týždeň {exp_start}–{exp_end}")
        return exp_start,exp_end
    return min(candidates,key=lambda x:abs((x[0]-exp_start).days))

def validate_expected_week(start,end):
    monday,friday=expected_week()
    if not(monday<=start<=friday and start<=end<=friday): raise RuntimeError(f"PDF nie je pre očakávaný pracovný týždeň: {start}–{end}, očakávam rozsah v {monday}–{friday}")
def upsert_week(start,end,parsed,source_url):
    monday=start-timedelta(days=start.weekday());friday=monday+timedelta(days=4);payload=[]
    for idx,day in enumerate(DAYS):
        if day not in parsed: continue
        menu_date=monday+timedelta(days=idx)
        if menu_date<start or menu_date>end: continue
        try:
            soup1,soup2,meals=parse_day(parsed[day])
        except ValueError as e:
            print(f"WARN: preskakujem {DAY_LABELS[idx]} {menu_date}: {e}")
            continue
        payload.append({"menu_date":menu_date.isoformat(),"weekday":DAY_LABELS[idx],"soup_1":soup1,"soup_2":soup2,"meals":meals,"price":PRICE})
    if not payload: raise ValueError("Nenašli sa žiadne použiteľné dni menu")
    r=requests.post(f"{SUPABASE_URL}/rest/v1/rpc/import_weekly_menu",headers={"apikey":SERVICE_ROLE_KEY,"Authorization":f"Bearer {SERVICE_ROLE_KEY}","Content-Type":"application/json"},json={"p_week_start":monday.isoformat(),"p_week_end":friday.isoformat(),"p_source_url":source_url,"p_rows":payload},timeout=30)
    if not r.ok: raise RuntimeError(f"Supabase import zlyhal: {r.status_code} {r.text}")
    print(json.dumps(r.json(),ensure_ascii=False,indent=2))
def main():
    url,pdf=fetch_pdf_url();text=pdf_text(pdf);sections=split_sections(text);print("Zdroj:",url);print("Nájdené dni:",sorted(sections));
    if not sections: raise ValueError("V PDF sa nenašli žiadne dni menu")
    start,end=parse_week_range(text);validate_expected_week(start,end);print("Týždeň:",start,"-",end);upsert_week(start,end,sections,url);print("ObedGo: týždenné menu bolo úspešne aktualizované.")
if __name__=="__main__":
    try: main()
    except Exception as e: print("ERROR:",e,file=sys.stderr);sys.exit(1)
