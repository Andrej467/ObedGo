import os, re, json, sys, html as html_lib
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF

MENU_URL = "https://superobed.sk/podnik/4m-restaurant/denne-menu"
SUPABASE_URL = "https://qbwrfortjvzqtdiupgva.supabase.co"
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
PRICE = 6.20
DAYS = ["PONDELOK