#!/usr/bin/env python3
"""
NASA Flight Analog Subject Recruitment (analogstudies.jsc.nasa.gov)
Harvester, Static HTML Pre-Renderer & Sitemap Generator
===================================================================
Dynamically harvests:
  1. The live Angular 17 SPA bundle (main-*.js) from https://analogstudies.jsc.nasa.gov
     to extract all routes, component templates, text strings, news items (bp, kp),
     figure assets, and screening questionnaire definitions.
  2. The live ASP.NET SignalR LongPolling endpoint (https://analogstudies.jsc.nasa.gov/CommonHub)
     to retrieve real-time recruitment status (`AcceptingApplications`, `Note`) for
     the 20-Foot Chamber and Moon & Mars Exploration Analog (MMEA), Responsible NASA
     Official metadata, and active application module data.
  3. Linked NASA reference pages and NTRS technical reports (20-Foot Chamber HESTIA,
     NASA Analog Missions, MMEA Volunteer Announcement, NASA 10HERD SORN).

Generates:
  - Pre-rendered, self-contained semantic HTML pages at both `<route>/index.html` and
    `<route>.html` for 1-to-1 URL rewriting (`https://oht8woowi8yait9n.github.io/analogstudies/...`
    -> `https://analogstudies.jsc.nasa.gov/...`).
  - `analogstudies_sitemap.xml` & `sitemap.xml` (GitHub Pages mirror URLs + linked NASA reference URLs)
  - `analogstudies_direct_sitemap.xml` (100% direct live NASA URLs)
  - `analogstudies_urls.txt` & `analogstudies_direct_urls.txt`
  - `analogstudies_catalog.json` (structured metadata & content hashes for zero-churn CI/CD)
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_ORIGIN = "https://analogstudies.jsc.nasa.gov"
GH_PAGES_BASE = os.environ.get(
    "GH_PAGES_BASE", "https://oht8woowi8yait9n.github.io/analogstudies"
).rstrip("/")

CATALOG_FILE = os.path.join(BASE_DIR, "analogstudies_catalog.json")
SITEMAP_FILE = os.path.join(BASE_DIR, "analogstudies_sitemap.xml")
SITEMAP_ROOT_FILE = os.path.join(BASE_DIR, "sitemap.xml")
DIRECT_SITEMAP_FILE = os.path.join(BASE_DIR, "analogstudies_direct_sitemap.xml")
URLS_FILE = os.path.join(BASE_DIR, "analogstudies_urls.txt")
DIRECT_URLS_FILE = os.path.join(BASE_DIR, "analogstudies_direct_urls.txt")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 (NASA-Onyx-RAG-Indexer/1.0)"
)


def write_if_changed(path: str, content: str) -> bool:
    """Write file only if content actually changed to prevent unnecessary Git churn."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == content:
                    return False
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def load_existing_catalog() -> dict:
    """Load previous catalog to preserve <lastmod> dates and fallback metadata."""
    if os.path.exists(CATALOG_FILE):
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def load_existing_sitemap_dates(path: str) -> dict:
    """Parse existing XML sitemap to map <loc> -> <lastmod>."""
    dates = {}
    if not os.path.exists(path):
        return dates
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        for elem in root.iter():
            if elem.tag.endswith("url"):
                loc = None
                lastmod = None
                for child in elem:
                    if child.tag.endswith("loc") and child.text:
                        loc = child.text.strip()
                    elif child.tag.endswith("lastmod") and child.text:
                        lastmod = child.text.strip()
                if loc and lastmod:
                    dates[loc] = lastmod
    except Exception:
        pass
    return dates


def fetch_signalr_live_data(session: requests.Session, fallback: dict) -> dict:
    """
    Query https://analogstudies.jsc.nasa.gov/CommonHub via ASP.NET Core SignalR
    LongPolling protocol to retrieve live recruitment status and official metadata.
    """
    result = {
        "nasaOfficial": fallback.get(
            "nasaOfficial",
            {
                "Name": "Brandon Vessey",
                "Email": "william.b.vessey@nasa.gov",
                "Role": "nasaOfficial",
            },
        ),
        "chamberStatus": fallback.get(
            "chamberStatus",
            {
                "ID": 19,
                "Name": "chamber",
                "AcceptingApplications": True,
                "Note": None,
            },
        ),
        "mmeaStatus": fallback.get(
            "mmeaStatus",
            {
                "ID": 20,
                "Name": "mmea",
                "AcceptingApplications": True,
                "Note": None,
            },
        ),
        "platforms": fallback.get(
            "platforms",
            [
                {
                    "ID": 19,
                    "Name": "Chamber",
                    "AcceptingApplications": True,
                    "Note": None,
                },
                {
                    "ID": 20,
                    "Name": "MMEA",
                    "AcceptingApplications": True,
                    "Note": None,
                },
            ],
        ),
        "countriesCount": fallback.get("countriesCount", 252),
        "usStatesCount": fallback.get("usStatesCount", 51),
    }

    try:
        neg_url = f"{SITE_ORIGIN}/CommonHub/negotiate?negotiateVersion=1"
        r_neg = session.post(neg_url, timeout=15)
        r_neg.raise_for_status()
        token = r_neg.json().get("connectionToken")
        if not token:
            return result

        hub_url = f"{SITE_ORIGIN}/CommonHub?id={urllib.parse.quote(token)}"
        # 1. Initial LongPolling request
        session.get(hub_url, timeout=15)
        # 2. JSON protocol v1 handshake
        session.post(
            hub_url,
            data='{"protocol":"json","version":1}\x1e',
            headers={"Content-Type": "text/plain;charset=UTF-8"},
            timeout=15,
        )
        session.get(hub_url, timeout=15)

        # 3. Invoke all 4 methods
        calls = [
            {
                "type": 1,
                "invocationId": "0",
                "target": "handleMethodCall",
                "arguments": [["getNasaOfficial"]],
            },
            {
                "type": 1,
                "invocationId": "1",
                "target": "handleMethodCall",
                "arguments": [["getApplicationAcceptanceStatus", "chamber"]],
            },
            {
                "type": 1,
                "invocationId": "2",
                "target": "handleMethodCall",
                "arguments": [["getApplicationAcceptanceStatus", "mmea"]],
            },
            {
                "type": 1,
                "invocationId": "3",
                "target": "handleMethodCall",
                "arguments": [["application_getInitialModuleData"]],
            },
        ]
        payload = "".join(json.dumps(c) + "\x1e" for c in calls)
        session.post(
            hub_url,
            data=payload,
            headers={"Content-Type": "text/plain;charset=UTF-8"},
            timeout=15,
        )

        # 4. Poll for callbacks (up to 3 polls to ensure all 4 callbacks arrive)
        seen_callbacks = 0
        for _ in range(3):
            r_poll = session.get(hub_url, timeout=15)
            if r_poll.status_code != 200 or not r_poll.text:
                break
            frames = [f for f in r_poll.text.split("\x1e") if f.strip()]
            for frame in frames:
                try:
                    msg = json.loads(frame)
                except Exception:
                    continue
                target = msg.get("target")
                args = msg.get("arguments") or []
                if target == "footer_getNasaOfficialCallback" and args:
                    result["nasaOfficial"] = args[0]
                    seen_callbacks += 1
                elif target == "getApplicationAcceptanceStatusCallback" and args:
                    st = args[0]
                    name = (st.get("Name") or "").lower()
                    if name == "chamber":
                        result["chamberStatus"] = st
                        seen_callbacks += 1
                    elif name == "mmea":
                        result["mmeaStatus"] = st
                        seen_callbacks += 1
                elif target == "application_getInitialModuleDataCallback" and args:
                    raw_mod = args[0]
                    if isinstance(raw_mod, str):
                        mod = json.loads(raw_mod)
                    else:
                        mod = raw_mod
                    if mod.get("Platforms"):
                        result["platforms"] = mod["Platforms"]
                    if mod.get("Countries"):
                        result["countriesCount"] = len(mod["Countries"])
                    if mod.get("USStates"):
                        result["usStatesCount"] = len(mod["USStates"])
                    seen_callbacks += 1
            if seen_callbacks >= 4:
                break
    except Exception as e:
        print(f"  [!] SignalR poll warning (using fallback): {e}")

    return result


def fetch_angular_bundle(session: requests.Session) -> tuple[str, str]:
    """Fetch index.html and main-*.js from https://analogstudies.jsc.nasa.gov."""
    r = session.get(f"{SITE_ORIGIN}/", timeout=20)
    r.raise_for_status()
    index_html = r.text
    m = re.search(r'src="(main-[A-Za-z0-9]+\.js)"', index_html)
    if not m:
        raise RuntimeError("Could not locate main-*.js in index.html")
    main_js_name = m.group(1)
    r_js = session.get(f"{SITE_ORIGIN}/{main_js_name}", timeout=30)
    r_js.raise_for_status()
    return main_js_name, r_js.text


MMEA_IMAGE_FILES = [
    "TransitVehicle-exteriorPhoto.jpg",
    "TransitVehicle-floorplan-level1.png",
    "TransitVehicle-floorplan-level2and3.png",
    "SurfaceHabitat-exteriorIllustration.png",
    "SurfaceHabitat-floorplan.png",
    "RoverVehicle-topAndSideViewIllustration.png",
    "RoverVehicle-interiorPhotos.png",
]


def mirror_mmea_images(session: requests.Session) -> None:
    """Download the 7 MMEA platform images into assets/images/mmea/ so GitHub Pages serves them directly."""
    img_dir = os.path.join(BASE_DIR, "assets", "images", "mmea")
    os.makedirs(img_dir, exist_ok=True)
    for fname in MMEA_IMAGE_FILES:
        dest = os.path.join(img_dir, fname)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            continue
        url = f"{SITE_ORIGIN}/assets/images/mmea/{fname}"
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200 and r.content:
                with open(dest, "wb") as f:
                    f.write(r.content)
        except Exception as e:
            print(f"  [!] Image mirror warning ({fname}): {e}")


def extract_js_strings_for_selector(js_text: str, selector: str) -> list[str]:
    """
    Extract all literal text nodes `c(idx, "...")` from the Angular Ivy component
    matching `selectors:[["<selector>"]]` in `main-*.js`.
    """
    pat = re.compile(
        r'selectors:\[\["' + re.escape(selector) + r'"\]\].*?template:function\([^)]*\)\{(.*?)\}(?:,dependencies:|,styles:|\}\)\}\})',
        re.DOTALL,
    )
    m = pat.search(js_text)
    if not m:
        return []
    tmpl = m.group(1)
    texts = []
    for cm in re.finditer(r'\bc\(\d+,"((?:[^"\\]|\\.)*)"\)', tmpl):
        raw = cm.group(1)
        try:
            decoded = json.loads(f'"{raw}"')
        except Exception:
            decoded = raw
        cleaned = " ".join(decoded.split())
        if cleaned:
            texts.append(cleaned)
    return texts


def extract_news_arrays(js_text: str) -> dict:
    """
    Dynamically parse news arrays (such as `bp` for Chamber News and `kp` for MMEA News)
    from `main-*.js`.
    """
    news = {
        "chamber": [
            {
                "source": "HESTIA Tour",
                "text": "Tour HESTIA 20-foot Chamber at NASA's Johnson Space Center",
                "url": "https://www.youtube.com/watch?v=vDdtMnNOYBk",
            }
        ],
        "mmea": [],
    }
    # Match objects with {source:"...",text:"...",url:"..."}
    item_pat = re.compile(
        r'\{source:"((?:[^"\\]|\\.)*)",text:"((?:[^"\\]|\\.)*)",url:"((?:[^"\\]|\\.)*)"\}'
    )
    # Find Chamber news component Cp and trace its data array variable
    m_cp = re.search(
        r'this\.data=([A-Za-z0-9_$]+)\}static\{this\.\\u0275fac.*?FlightAnalogRecruiting-chamber-news',
        js_text,
    )
    if m_cp:
        var_name = m_cp.group(1)
        m_arr = re.search(
            r"\b" + re.escape(var_name) + r"=\[(.*?)\];", js_text, re.DOTALL
        )
        if m_arr:
            items = []
            for im in item_pat.finditer(m_arr.group(1)):
                items.append(
                    {
                        "source": im.group(1),
                        "text": im.group(2),
                        "url": im.group(3),
                    }
                )
            if items:
                news["chamber"] = items

    m_ip = re.search(
        r'this\.data=([A-Za-z0-9_$]+)\}static\{this\.\\u0275fac.*?FlightAnalogRecruiting-mmea-news',
        js_text,
    )
    if m_ip:
        var_name = m_ip.group(1)
        m_arr = re.search(
            r"\b" + re.escape(var_name) + r"=\[(.*?)\];", js_text, re.DOTALL
        )
        if m_arr:
            items = []
            for im in item_pat.finditer(m_arr.group(1)):
                items.append(
                    {
                        "source": im.group(1),
                        "text": im.group(2),
                        "url": im.group(3),
                    }
                )
            news["mmea"] = items

    return news


def extract_contact_email(js_text: str) -> str:
    m = re.search(r'analogContactEmail:"([^"]+)"', js_text)
    return m.group(1) if m else "jsc-analog-application@mail.nasa.gov"


def render_page_shell(
    title: str,
    route_path: str,
    section_label: str,
    body_html: str,
    live_data: dict,
    contact_email: str,
) -> str:
    """Wrap page content in a clean, semantic, accessible HTML5 document."""
    live_url = f"{SITE_ORIGIN}{route_path}" if route_path != "/" else f"{SITE_ORIGIN}/"
    official = live_data.get("nasaOfficial") or {}
    official_name = official.get("Name", "Brandon Vessey")
    official_email = official.get("Email", "william.b.vessey@nasa.gov")

    chamber_open = (live_data.get("chamberStatus") or {}).get(
        "AcceptingApplications", True
    )
    mmea_open = (live_data.get("mmeaStatus") or {}).get("AcceptingApplications", True)
    chamber_badge = (
        "Open — Accepting Applications"
        if chamber_open
        else "Closed — Not Currently Accepting Applications"
    )
    mmea_badge = (
        "Open — Accepting Applications"
        if mmea_open
        else "Closed — Not Currently Accepting Applications"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)} | NASA Flight Analog Subject Recruitment (JSC)</title>
  <link rel="canonical" href="{html.escape(live_url)}" />
  <meta name="description" content="{html.escape(title)} — NASA Human Research Program (HRP) Flight Analog Subject Recruitment at Johnson Space Center (JSC)." />
  <style>
    :root {{
      --nasa-blue: #0b3d91;
      --nasa-red: #fc3d21;
      --bg-page: #f8fafc;
      --bg-card: #ffffff;
      --text-main: #0f172a;
      --text-muted: #475569;
      --border-color: #cbd5e1;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      line-height: 1.65;
      color: var(--text-main);
      background: var(--bg-page);
      margin: 0;
      padding: 0;
    }}
    header.site-header {{
      background: var(--nasa-blue);
      color: #ffffff;
      padding: 1.25rem 1.5rem;
    }}
    header.site-header .container {{
      max-width: 1080px;
      margin: 0 auto;
    }}
    header.site-header h1 {{
      margin: 0 0 0.35rem 0;
      font-size: 1.55rem;
    }}
    header.site-header p.subtitle {{
      margin: 0;
      opacity: 0.9;
      font-size: 0.95rem;
    }}
    nav.site-nav {{
      background: #1e293b;
      color: #ffffff;
      padding: 0.6rem 1.5rem;
      font-size: 0.9rem;
    }}
    nav.site-nav .container {{
      max-width: 1080px;
      margin: 0 auto;
      display: flex;
      flex-wrap: wrap;
      gap: 0.85rem;
    }}
    nav.site-nav a {{
      color: #e2e8f0;
      text-decoration: none;
      font-weight: 500;
    }}
    nav.site-nav a:hover {{
      color: #ffffff;
      text-decoration: underline;
    }}
    main.container {{
      max-width: 1080px;
      margin: 1.5rem auto;
      padding: 0 1.5rem;
    }}
    .meta-banner {{
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      border-radius: 8px;
      padding: 0.85rem 1.15rem;
      margin-bottom: 1.5rem;
      font-size: 0.92rem;
    }}
    .meta-banner strong {{
      color: var(--nasa-blue);
    }}
    .card {{
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 1.5rem;
      margin-bottom: 1.5rem;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
    }}
    h2, h3, h4 {{
      color: var(--nasa-blue);
      margin-top: 1.25rem;
      margin-bottom: 0.65rem;
    }}
    .card > h2:first-child, .card > h3:first-child {{
      margin-top: 0;
    }}
    .status-badge {{
      display: inline-block;
      padding: 0.25rem 0.65rem;
      border-radius: 999px;
      font-size: 0.85rem;
      font-weight: 600;
      background: #dcfce7;
      color: #166534;
    }}
    .figure-box {{
      margin: 1.25rem 0;
      padding: 1rem;
      background: #f8fafc;
      border: 1px solid var(--border-color);
      border-radius: 6px;
      text-align: center;
    }}
    .figure-box img {{
      max-width: 100%;
      height: auto;
      border-radius: 4px;
      margin-bottom: 0.5rem;
    }}
    .figure-caption {{
      font-size: 0.92rem;
      color: var(--text-muted);
    }}
    .notice-box {{
      background: #fffbeb;
      border: 1px solid #fde68a;
      border-radius: 6px;
      padding: 1rem 1.25rem;
      margin-bottom: 1.25rem;
    }}
    table.data-table {{
      width: 100%;
      border-collapse: collapse;
      margin: 1rem 0;
      font-size: 0.94rem;
    }}
    table.data-table th, table.data-table td {{
      border: 1px solid var(--border-color);
      padding: 0.6rem 0.75rem;
      text-align: left;
      vertical-align: top;
    }}
    table.data-table th {{
      background: #f1f5f9;
      font-weight: 600;
    }}
    footer.site-footer {{
      background: #0f172a;
      color: #cbd5e1;
      padding: 1.5rem;
      margin-top: 2.5rem;
      font-size: 0.88rem;
    }}
    footer.site-footer .container {{
      max-width: 1080px;
      margin: 0 auto;
    }}
    footer.site-footer a {{
      color: #93c5fd;
    }}
    footer.site-footer ul.footer-links {{
      list-style: none;
      padding: 0;
      margin: 0.75rem 0;
      display: flex;
      flex-wrap: wrap;
      gap: 1rem;
    }}
  </style>
</head>
<body>
  <header class="site-header">
    <div class="container">
      <h1>NASA Human Research Program — Flight Analog Subject Recruitment</h1>
      <p class="subtitle">NASA Lyndon B. Johnson Space Center (JSC) | {html.escape(section_label)}</p>
    </div>
  </header>
  <nav class="site-nav" aria-label="Primary Navigation">
    <div class="container">
      <a href="{SITE_ORIGIN}/">Home</a>
      <a href="{SITE_ORIGIN}/chamber">20-Foot Chamber</a>
      <a href="{SITE_ORIGIN}/chamber/about/requirements">Chamber Requirements</a>
      <a href="{SITE_ORIGIN}/chamber/news">Chamber News</a>
      <a href="{SITE_ORIGIN}/mmea">MMEA (Moon &amp; Mars Analog)</a>
      <a href="{SITE_ORIGIN}/mmea/about/requirements">MMEA Requirements</a>
      <a href="{SITE_ORIGIN}/mmea/about/platforms">MMEA Platforms</a>
      <a href="{SITE_ORIGIN}/mmea/about/missions">MMEA Missions</a>
      <a href="{SITE_ORIGIN}/mmea/news">MMEA News</a>
      <a href="{SITE_ORIGIN}/apply">Apply Now</a>
      <a href="https://www.nasa.gov/setmo/facilities/20-foot-chamber/">NASA 20-Foot Chamber Facility</a>
      <a href="https://www.nasa.gov/analog-missions/">NASA Analog Missions</a>
    </div>
  </nav>
  <main class="container">
    <div class="meta-banner">
      <strong>Source URL:</strong> <a href="{html.escape(live_url)}">{html.escape(live_url)}</a><br />
      <strong>Program:</strong> NASA Human Research Program (HRP) — Flight Analog Project, NASA Johnson Space Center (Houston, TX)<br />
      <strong>Recruitment Contact:</strong> <a href="mailto:{html.escape(contact_email)}">{html.escape(contact_email)}</a> |
      <strong>Responsible NASA Official:</strong> <a href="mailto:{html.escape(official_email)}">{html.escape(official_name)} ({html.escape(official_email)})</a><br />
      <strong>Live Recruitment Status:</strong>
      20-Foot Chamber: <span class="status-badge">{html.escape(chamber_badge)}</span> |
      MMEA: <span class="status-badge">{html.escape(mmea_badge)}</span>
    </div>
    {body_html}
  </main>
  <footer class="site-footer">
    <div class="container">
      <div><strong>National Aeronautics and Space Administration (NASA)</strong> — Lyndon B. Johnson Space Center</div>
      <div>Responsible NASA Official: <a href="mailto:{html.escape(official_email)}">{html.escape(official_name)}</a> | Curator: <a href="mailto:{html.escape(contact_email)}">HH&amp;P App Dev Team</a></div>
      <ul class="footer-links">
        <li><a href="https://www.nasa.gov/privacy/">Privacy Policy</a></li>
        <li><a href="https://www.nasa.gov/foia/">FOIA</a></li>
        <li><a href="https://www.nasa.gov/organizations/ochmo/no-fear-act/">No FEAR Act</a></li>
        <li><a href="https://oig.nasa.gov/">Office of the IG</a></li>
        <li><a href="https://www.nasa.gov/organizations/budget-annual-reports/agency-financial-reports/">Agency Financial Reports</a></li>
        <li><a href="https://www.nasa.gov/contact-nasa/">Contact NASA</a></li>
        <li><a href="https://www.nasa.gov/accessibility/">Accessibility</a></li>
      </ul>
      <div>Be advised, this is a website for recruiting test subjects for NASA ground-based spaceflight analog studies.</div>
    </div>
  </footer>
</body>
</html>
"""


def build_pages(js_text: str, live_data: dict) -> list[dict]:
    """
    Build all pre-rendered HTML pages for `analogstudies.jsc.nasa.gov`.
    Combines structured semantic sections with dynamically extracted strings from `main-*.js`
    so any future bundle text updates are automatically reflected.
    """
    contact_email = extract_contact_email(js_text)
    news_data = extract_news_arrays(js_text)

    chamber_open = (live_data.get("chamberStatus") or {}).get(
        "AcceptingApplications", True
    )
    chamber_note = (live_data.get("chamberStatus") or {}).get("Note") or ""
    mmea_open = (live_data.get("mmeaStatus") or {}).get("AcceptingApplications", True)
    mmea_note = (live_data.get("mmeaStatus") or {}).get("Note") or ""

    # Extract raw component text strings from main-*.js to detect any future additions
    comp_texts = {
        "main": extract_js_strings_for_selector(js_text, "main"),
        "chamber_main": extract_js_strings_for_selector(
            js_text, "FlightAnalogRecruiting-chamber-main"
        ),
        "chamber_req": extract_js_strings_for_selector(
            js_text, "FlightAnalogRecruiting-chamber-requirements"
        ),
        "mmea_main": extract_js_strings_for_selector(
            js_text, "FlightAnalogRecruiting-mmea-main"
        ),
        "mmea_req": extract_js_strings_for_selector(
            js_text, "FlightAnalogRecruiting-mmea-requirements"
        ),
        "mmea_platforms": extract_js_strings_for_selector(
            js_text, "FlightAnalogRecruiting-mmea-platforms"
        ),
        "mmea_missions": extract_js_strings_for_selector(
            js_text, "FlightAnalogRecruiting-mmea-missions"
        ),
    }

    # Reusable HTML blocks for Chamber and MMEA sub-sections
    chamber_overview_html = f"""
    <section class="card">
      <h2>20-Foot Chamber Analog (Exploration Atmosphere)</h2>
      <p><strong>Do you have a passion for space? Are you interested in being a human research subject for NASA’s Exploration Atmosphere?</strong></p>
      <p>NASA’s Johnson Space Center (JSC) test subject screening is recruiting volunteers for an overnight study lasting up to 15 days where you will live and work with others in an altitude chamber to test procedures that should reduce the risk of decompression sickness in astronauts during spacewalks, also called extravehicular activity (EVA). In brief, selected participants will aid researchers in the validation of a denitrogenation protocol that reduces the risk of decompression sickness during a 6-hour exposure to low atmospheric pressure in an altitude chamber. Volunteers must pass a JSC Class III physical, psychological assessment, physiological training, VO2 Max test, lung capacity test/x-rays, and ultrasound of the heart to qualify and comply with requirements.</p>
      <p>
        <strong>Application Status:</strong> {"Currently Accepting Applications (<a href='" + SITE_ORIGIN + "/apply'>Apply Online</a>)" if chamber_open else html.escape(chamber_note or "Not currently accepting applications")}<br />
        <strong>Contact Email:</strong> <a href="mailto:{html.escape(contact_email)}">{html.escape(contact_email)}</a><br />
        <strong>Facility Reference:</strong> <a href="https://www.nasa.gov/setmo/facilities/20-foot-chamber/">NASA JSC 20-Foot Chamber (HESTIA Facility Overview)</a> |
        <a href="https://ntrs.nasa.gov/api/citations/20160003488/downloads/20160003488.pdf">HESTIA Technical Paper (NTRS 20160003488 PDF)</a>
      </p>
    </section>
    """

    chamber_requirements_html = """
    <section class="card">
      <h2>Human Research Participant Requirements for Exploration Atmosphere (20-Foot Chamber)</h2>
      <ul>
        <li>
          Must commit to an up to 15 day mission along with multiple days of pre and post mission activities living at a reduced atmospheric pressure in the 20-foot altitude chamber in Building 7 at NASA’s Johnson Space Center including:
          <ul>
            <li>2–3 days of pre-mission screening</li>
            <li>2–3 days of pre-mission data collection</li>
            <li>5 days pre-mission training, including 1 overnight stay</li>
            <li>Up to 15 days confined habitation, including 14 overnight stays and 1 pre-chamber training</li>
            <li>3 days post-mission testing</li>
          </ul>
          <em>(Total experiment commitment: up to 29 days, including 15 days of confined isolation.)</em>
        </li>
        <li>Must be healthy with physical and biological characteristics comparable to those of astronauts currently involved in NASA’s human space flight program.</li>
        <li>Must be between the ages of <strong>30–60 at mission start time</strong>.</li>
        <li>Must have a bachelor’s degree, and ability to read/speak/comprehend English.</li>
        <li>Must be physically active and able to achieve a VO2 peak (cycling) of <strong>35 ml/min/kg for Male</strong> and <strong>30 ml/min/kg for Female</strong>.</li>
        <li>Must be able to pass all of the screening tests and training activities.</li>
      </ul>

      <h3>Specific Exclusion Criteria (20-Foot Chamber)</h3>
      <ul>
        <li>Must be a healthy, non-smoking volunteer.</li>
        <li>Subject does not take over-the-counter or prescription medication to control: a chronic painful condition, anxiety or stress disorder, psychosis, a sleep disorder or a breathing disorder.</li>
        <li>Must have <strong>no</strong> dietary restrictions.</li>
        <li>No respiratory illness.</li>
        <li>No significant findings on Echocardiogram screening (Atrial Septal Defect, etc.).</li>
        <li>Must have <strong>no</strong> history of significant sleep apnea, sleepwalking or taking sleep-aids.</li>
        <li>Must have US citizenship or be a legal permanent resident (Green Card holder; no foreign nationals).</li>
        <li>Consent to research studies collecting biological samples and investigating the effects on personal behaviors, team cohesion, cognition, and communication.</li>
        <li>Pregnancy is an exclusion criterion.</li>
      </ul>
      <p><em>**Volunteers are compensated. Some restrictions apply to NASA civil servants and contractors. Contact your Human Resources department for your company's policy.</em></p>
    </section>
    """

    chamber_news_items_li = "\n".join(
        f'<li><a href="{html.escape(item["url"])}">{html.escape(item["text"])}</a> <strong>({html.escape(item["source"])})</strong> — <code>{html.escape(item["url"])}</code></li>'
        for item in news_data.get("chamber", [])
    )
    chamber_news_html = f"""
    <section class="card">
      <h2>20-Foot Chamber in the News</h2>
      <ul>
        {chamber_news_items_li or "<li>News coming soon!</li>"}
      </ul>
      <h3>Related NASA Resources</h3>
      <ul>
        <li><a href="https://www.nasa.gov/setmo/facilities/20-foot-chamber/">20-Foot Chamber at NASA Johnson Space Center (HESTIA / Crew &amp; Thermal Systems Division)</a></li>
        <li><a href="https://ntrs.nasa.gov/api/citations/20160003488/downloads/20160003488.pdf">Human Exploration System Test-Bed for Integration and Advancement (HESTIA) Support of Future NASA Deep-Space Missions (NTRS 20160003488)</a></li>
        <li><a href="https://www.nasa.gov/mission/altitude-chambers/">NASA Altitude Chambers Mission Overview</a></li>
      </ul>
    </section>
    """

    mmea_overview_html = f"""
    <section class="card">
      <h2>Moon &amp; Mars Exploration Analog (MMEA)</h2>
      <p><strong>Do you have a passion for space? Do you want to be a Crew Member on a Year-Long Simulated Space Mission?</strong></p>
      <p>NASA’s Johnson Space Center (JSC) test subject screening is recruiting participants for the first mission inside the MMEA analog. For one year, you would be living and working in isolation and confinement at JSC while simulating inter-planetary transit and planetary surface operations, including simulated planetary surface walks. Volunteers inside MMEA will help NASA assess and validate hardware and technologies, protocols, requirements, and other support systems designed to sustain crew health and performance on long-duration missions — all without leaving Earth.</p>
      <p>For more information continue reading the information on this website to learn more about these missions and if interested, submit the application for consideration as a potential MMEA mission crew member.</p>
      <p>
        <strong>Application Status:</strong> {"Currently Accepting Applications (<a href='" + SITE_ORIGIN + "/apply'>Apply Online</a>)" if mmea_open else html.escape(mmea_note or "Not currently accepting applications")}<br />
        <strong>Contact Email:</strong> <a href="mailto:{html.escape(contact_email)}">{html.escape(contact_email)}</a><br />
        <strong>NASA Analog Missions Portal:</strong> <a href="https://www.nasa.gov/analog-missions/">https://www.nasa.gov/analog-missions/</a> |
        <a href="https://www.nasa.gov/hrp/nasa-seeks-volunteers-for-yearlong-simulated-mission-to-moon-mars/">NASA Seeks Volunteers for Yearlong Simulated Mission to Moon, Mars</a>
      </p>
    </section>
    """

    mmea_requirements_html = """
    <section class="card">
      <h2>MMEA Participant Requirements</h2>
      <ul>
        <li>Four to six crew per mission</li>
        <li>Must be US citizens, or hold a US Permanent Resident Card (Green Card), unless international crew participate under an international partner agreement</li>
        <li>Must be proficient in English</li>
        <li>Goal of 50/50 male:female ratio for each mission, but the actual crew mix is contingent on participant pool availability</li>
        <li>The preferred age range of crew is from <strong>30 – 55</strong>; candidates outside the stated age range may be considered but will require additional approvals</li>
        <li>Height limited to <strong>6’2” maximum</strong> (74 inches)</li>
        <li>Pass a modified Class III flight physical</li>
        <li>Technical skills demonstrated through professional experience and education</li>
        <li>Must have demonstrated motivation and work ethic similar to the current astronaut population</li>
        <li>Psychological assessment by a clinical psychologist to qualify for participation</li>
        <li>Participation in a multi-day selection activity</li>
        <li>
          Astronaut-like characteristics that are considered during MMEA test participant selection include the following criteria used in astronaut selection:
          <ul>
            <li>The requirements for Astronaut Candidates are a bachelor’s degree from an accredited institution in engineering, biological science, physical science, or mathematics. Quality of academic preparation is important.</li>
            <li>An advanced degree (e.g. M.S.) in a STEM field is preferred and may be substituted for experience as follows: <strong>Master’s Degree = 1 year of experience</strong>, <strong>Doctoral Degree = 3 years of experience</strong>.</li>
            <li>Military experience may be considered equivalent years of experience.</li>
          </ul>
        </li>
      </ul>
    </section>
    """

    mmea_platforms_html = f"""
    <section class="card">
      <h2>MMEA Platforms (Building 220, NASA Johnson Space Center)</h2>
      <p>The MMEA platforms reside in <strong>Building 220 at NASA Johnson Space Center (JSC)</strong>, under the supervision of a centralized Mission Control Center (MCC).</p>

      <h3>1. Transit Vehicle (682 sq. ft.)</h3>
      <p>The Transit Vehicle is a two-story, four-port habitat. It is a cylindrical core with a vertical axis and connects to a Hydroponics Module/Airlock and Hygiene Module (Figure 1). The total space comprises <strong>682 sq. ft.</strong>, distributed as follows:</p>
      <ul>
        <li><strong>L1 Work Area core:</strong> 214 sq. ft.</li>
        <li><strong>L2 Living Area:</strong> 214 sq. ft.</li>
        <li><strong>L3 Crew Quarters:</strong> 131 sq. ft.</li>
        <li><strong>Hydroponics Module and Airlock:</strong> ~65 sq. ft.</li>
        <li><strong>Hygiene Module:</strong> 58 sq. ft.</li>
      </ul>
      <div class="figure-box">
        <img src="{GH_PAGES_BASE}/assets/images/mmea/TransitVehicle-exteriorPhoto.jpg" alt="MMEA Transit Vehicle - External View" />
        <img src="{GH_PAGES_BASE}/assets/images/mmea/TransitVehicle-floorplan-level1.png" alt="MMEA Transit Vehicle - Level 1 Floorplan" />
        <img src="{GH_PAGES_BASE}/assets/images/mmea/TransitVehicle-floorplan-level2and3.png" alt="MMEA Transit Vehicle - Levels 2 and 3 Floorplan" />
        <div class="figure-caption"><strong>Figure 1:</strong> Transit Vehicle. The core module has three levels and two adjoining modules (hydroponics and hygiene).</div>
      </div>

      <h3>2. Surface Habitat (~1,700 sq. ft.)</h3>
      <p>The Surface Habitat is a one-story 3D printed habitat built to simulate habitation on the Martian surface (Figure 2). It has multiple rooms and a covered outside area that can support both physical EVAs on a sand surface and clean spaces for VR EVAs.</p>
      <div class="figure-box">
        <img src="{GH_PAGES_BASE}/assets/images/mmea/SurfaceHabitat-exteriorIllustration.png" alt="MMEA Surface Habitat - External Illustration" />
        <img src="{GH_PAGES_BASE}/assets/images/mmea/SurfaceHabitat-floorplan.png" alt="MMEA Surface Habitat - Floorplan" />
        <div class="figure-caption"><strong>Figure 2:</strong> Surface Habitat. The total interior area is ~1700 ft<sup>2</sup>.</div>
      </div>

      <h3>3. Rover Vehicle</h3>
      <p>The Rover Vehicle is a simulated transport unit that contains two driver seats, two beds, a nonflushable toilet, and a small samples airlock. It can house two participants for multiple days depending on food/water supply and operational requirements. It is anticipated that it will serve as a closed, habitable volume constrained space for high task cadence during short term occupancy (3 days/2 nights, for example). It simulates a vehicle to transfer a crew of two to exploration sites far from the main habitat, physically separating the crew into two teams for specified periods and increasing communication complexity between the teams, and between the teams and MCC.</p>
      <p>The Rover has simulated active views of a planetary surface for driving via monitors located in front of the driver seats. Crew can also don VR and drive the Rover to locations far from the Surface Habitat to perform simulated Extra Vehicular Activities (EVAs). During the surface exploration phase, the Rover can be accessed from the Surface Habitat via an airlock.</p>
      <div class="figure-box">
        <img src="{GH_PAGES_BASE}/assets/images/mmea/RoverVehicle-topAndSideViewIllustration.png" alt="MMEA Rover Vehicle - Top and Side View Illustrations" />
        <img src="{GH_PAGES_BASE}/assets/images/mmea/RoverVehicle-interiorPhotos.png" alt="MMEA Rover Vehicle - Interior Photos" />
        <div class="figure-caption"><strong>Figure 3:</strong> Rover Vehicle. The vehicle has two hatches that can be attached to an airlock or allow direct ingress and egress.</div>
      </div>
    </section>
    """

    mmea_missions_html = """
    <section class="card">
      <h2>MMEA Mission Definition &amp; Operations</h2>

      <h3>Mission Definition</h3>
      <p>The recently established MMEA project plans for missions of approximately <strong>1-year duration starting no earlier than August 2027</strong>. A mission is defined as one integrated protocol which may include one or more transit phases and a surface exploration phase. Mission phases and durations are variable and determined by the science and operational requirements for each mission.</p>
      <p>Each phase is typically performed in a different analog platform: transit in the Transit Vehicle, and surface exploration in the Surface Habitat and Rover Vehicle, with transfer of crew and supplies via a tunnel linking the platforms.</p>

      <h3>Reference Mission Conditions</h3>
      <ul>
        <li>Simulated space mission using the MMEA to emulate a non-specific spacecraft and surface habitat</li>
        <li>4 to 6 crew members</li>
        <li><strong>Duration:</strong> Up to approximately 1-year confined habitation specific to science requirements; approximately 14 total study months inclusive of 6 weeks pre- and 2 weeks post-confinement training and baseline data collection</li>
        <li><strong>Light/Dark Cycle (typical):</strong> Lights on 0700, lights out 2300, 7 days per week</li>
        <li>Typical crew day schedule requires 16 hours awake; 8 hours of sleep scheduled</li>
        <li>Crew mission schedule modeled after ISS crew schedule, modified to reflect exploration mission activities and events</li>
        <li>Mission scenario constructed to simulate varying degrees of workload and includes specific stressful conditions commonly encountered in space flight</li>
        <li>Crew remains isolated for the duration of the mission</li>
        <li>No access to internet content, social media, television/radio, and telephone. However, the crew will receive regular uplinks of selected newspapers</li>
        <li>Crew members have regularly scheduled private family conference, private medical conference, and private psychological conference</li>
        <li>Crew members have access to a limited selection of audio and video media, books and training materials through a controlled account</li>
        <li>Continuous monitoring of crew members during isolation, excluding sleep quarters, hygiene areas, and during private family/medical/psychological conferences</li>
      </ul>

      <h3>Mission Fidelity</h3>
      <ul>
        <li>
          Mission Control Center (MCC) for mission appropriate interaction with MMEA crew members:
          <ul>
            <li>24/7 mission video surveillance with audio, recorded during mission with long term storage and transfer available on specific request consistent with privacy and confidentiality requirements</li>
            <li>Voice communication recordings and text communication between crew and MCC during the mission with long term storage and available to payload developers on specific request consistent with privacy and confidentiality requirements</li>
            <li>Logging of operationally relevant mission events, including off-nominal situations and anomalies</li>
          </ul>
        </li>
        <li>
          Exploration realistic comm delays from seconds to minutes each way for voice and/or text:
          <ul>
            <li>Simulation of Acquisition of Signal/Loss of Signal (AOS/LOS) of varying duration</li>
            <li>Simulation of solar conjunction (total comm blackout up to two weeks)</li>
          </ul>
        </li>
        <li>Altered MCC and crew schedules to simulate Mars Sol</li>
        <li>Communication bandwidth realism with priority queuing of data types and file transfers between ground and habitats</li>
        <li>Flight simulators to support exploration mission scenarios such as docking, rover activities or piloting activities</li>
        <li>Rover module capable of supporting two crew members for 2–3 days isolated from the Surface Habitat and other crew members</li>
        <li>
          The rover module does not physically move but allows for Virtual Reality excursions (driving and EVA):
          <ul>
            <li>Includes communication with MDA and MCC</li>
          </ul>
        </li>
        <li>Virtual reality system for simulated EVA tasks including VR EVAs on treadmills</li>
        <li>Visually isolated EVA sandbox with equipment for physical EVAs including EVA suits (low fidelity), airlock procedures, In Situ Resource Units, and tools</li>
        <li>Heart rate monitor to support exercise safety and data collection activities</li>
        <li>Support for actigraphy and other wearables, including implementation in multiple platforms</li>
        <li>Support for basic laboratory tools</li>
        <li>Full facility coverage of surveillance video and audio except for bedrooms, bathrooms and ability to turn off areas for private activities</li>
        <li>Blood/saliva/urine/fecal collection pre, during and post mission. Crewmembers may be trained for sample processing.</li>
        <li>
          Medical workstation:
          <ul>
            <li>Remote medical procedures and examinations</li>
          </ul>
        </li>
        <li>Adjustable LED lighting</li>
        <li>Stowage modules to house full mission supplies with pass through for hardware, biological samples, and trash</li>
        <li>Modifiable virtual window views</li>
        <li>Ability to control habitat temperature for crew comfort</li>
        <li>Limited exercise equipment (aerobic and resistive) to simulate daily operational activities</li>
        <li>Simulated habitat and vehicle systems (e.g. Environmental Control and Life Support System (ECLSS), power, and propulsion systems) with physical or electronic user interfaces to support complex operational activities</li>
        <li>3D printer to support vehicle maintenance and operational tasks</li>
        <li>Simulated alarm system to support emergency scenarios</li>
        <li>Flight-similar galley capabilities for preparing meals. Limited galley in the Transit Vehicle, no galley in Rover, and more equipped galley in the Surface Habitat</li>
        <li>Plumbed water for crew consumption and food rehydration</li>
        <li>Shower/sink with hot and cold running water for crew hygiene</li>
        <li>Ability to monitor and restrict resources such as water, power, consumables, etc.</li>
        <li>Hydroponics as supplemental food source</li>
        <li>System spares, consumables and toolkits to facilitate realistic maintenance and repair tasks</li>
      </ul>

      <h3>Dietary Conditions</h3>
      <p>Dietary conditions are subject to potential modifications based on user needs. As a proposed baseline participating crew members will be provided a minimum of 3 meals each day. Diet and core menu is based on NASA spaceflight nutritional requirements. Once a crew is selected, specific crew caloric requirements are determined via measurements of resting metabolic rate (RMR). A core menu of primarily off-the-shelf food items is provided with a flight like combination of thermo-stabilized, rehydratable, and natural food items. Due to the objective of stocking all foods required for a complete mission in each habitat, fresh foods are not available in the primary food supply. A hydroponics system will be available to supplement the food supply with crew grown crops.</p>

      <h3>Typical Crew Daily and Weekly Work Requirements</h3>
      <p>The operational plan uses the ISS Program nominal 24-hour workday structure as a guideline. Feasibility of studies and technology demonstrations with specific workday organization requirements will be assessed on a per study basis.</p>
      <p>Below is the proposed baseline MMEA mission plan:</p>
      <ul>
        <li>All time spent in the habitat will be working on tasks related to the mission and associated payloads (studies, tech demos) — individual’s professional/work activities outside of the mission scenario are not allowed</li>
        <li>Crew awake at 0700 and are off duty at 2300 with nominal one shift operation for all crew</li>
        <li>Sleep period (8.0 hours)</li>
        <li>Post-sleep period includes morning meal (1.5 hours)</li>
        <li>Daily planning conferences, medical conferences, work preparation, and plan familiarization (2.0 hours)</li>
        <li>Work consists of scheduled payload tasks and MMEA operations tasks, i.e. Transit Vehicle/Rover/Surface Habitat maintenance, flight simulator for a spacecraft and/or terrestrial rendezvous mission, public affairs activities, education outreach, etc. (6.5–8.0 hours)</li>
        <li>Midday meal (1 hour)</li>
        <li>Exercise period (1.25 – 2.5 hours, includes time for setup, cardiovascular/resistive exercise, stowage, hygiene [cool down and cleanup])</li>
        <li>Pre-sleep period includes evening meal (2.0 hours)</li>
        <li>A nominal 7-day work/rest cycle will consist of 5.5 days available for conducting planned mission tasks and payload activities and 1.5 consecutive off-duty days. Housekeeping and 1.0 hour of scheduled work on the weekends is included in the 5.5 working days</li>
      </ul>

      <h3>Research and Technology Demonstrations</h3>
      <p>Studies and technology demonstrations suitable for this analog may include, but are not limited to:</p>
      <ul>
        <li>Crew hierarchy, team cohesion, teamwork and stress</li>
        <li>Communication and autonomy with varying communication delay</li>
        <li>Simulation of autonomous operations</li>
        <li>Multi-Team systems (MCC and Crew)</li>
        <li>Testing of autonomous and AI assisted technologies</li>
        <li>
          Controlled environmental conditions:
          <ul>
            <li>Temperature</li>
            <li>Humidity</li>
            <li>Lighting</li>
          </ul>
        </li>
        <li>Simulated habitat, vehicle, and EVA systems</li>
        <li>Planetary surface exploration (VR and physical)</li>
        <li>High tempo operations</li>
        <li>Exploration medical capabilities, assessments and operations</li>
        <li>Behavioral health and performance assessments</li>
        <li>Human factors evaluations and HSIA</li>
        <li>Human health countermeasures testing and assessments</li>
        <li>In-mission training paradigms</li>
        <li>Exercise countermeasures</li>
        <li>Performance evaluations after short periods of sleep deprivation</li>
        <li>Effect of lack of privacy, cramped space, simulated emergency response, visual monotony, food monotony, etc.</li>
        <li>Food systems including crop growth</li>
      </ul>
    </section>
    """

    mmea_news_items_li = "\n".join(
        f'<li><a href="{html.escape(item["url"])}">{html.escape(item["text"])}</a> <strong>({html.escape(item["source"])})</strong> — <code>{html.escape(item["url"])}</code></li>'
        for item in news_data.get("mmea", [])
    )
    mmea_news_html = f"""
    <section class="card">
      <h2>MMEA in the News</h2>
      <ul>
        {mmea_news_items_li or "<li>News coming soon!</li>"}
      </ul>
      <h3>Related NASA MMEA &amp; Analog Mission Articles</h3>
      <ul>
        <li><a href="https://www.nasa.gov/hrp/nasa-seeks-volunteers-for-yearlong-simulated-mission-to-moon-mars/">NASA Seeks Volunteers for Yearlong Simulated Mission to Moon, Mars (MMEA Recruitment Announcement)</a></li>
        <li><a href="https://www.nasa.gov/analog-missions/">NASA Analog Missions Overview</a></li>
        <li><a href="https://www.nasa.gov/reference/analog-missions-why-do-we-use-analogs/">Analog Missions: Why Do We Use Analogs?</a></li>
        <li><a href="https://www.nasa.gov/humans-in-space/step-1-earth-analogs-help-advance-missions-to-moon-mars/">Step 1: Earth Analogs Help Advance Missions to Moon, Mars</a></li>
      </ul>
    </section>
    """

    # Full /apply page HTML (including Paperwork Reduction Act, Privacy Act Statement, Confinement Disclosures, and all 40 screening questions)
    apply_body_html = f"""
    <section class="card">
      <h2>Application for Analog Missions</h2>
      <p>Submit an application online at <a href="{SITE_ORIGIN}/apply">{SITE_ORIGIN}/apply</a> or contact the recruitment team at <a href="mailto:{html.escape(contact_email)}">{html.escape(contact_email)}</a>.</p>

      <div class="notice-box">
        <h3>Paperwork Reduction Act Statement (OMB Control No. 2700-0174)</h3>
        <p>The public burden to complete this information collection is estimated at 15 minutes per response, including time for reviewing instructions, searching data sources, gathering and maintaining the data needed, and completing and reviewing the collected information. An agency may not conduct or sponsor, and a person is not required to respond to, a collection of information unless it displays a currently valid OMB control number.</p>
        <p>Send comments regarding this burden estimate or any other aspect of this collection of information, including suggestions for reducing this burden, to <a href="mailto:{html.escape(contact_email)}">{html.escape(contact_email)}</a>. Current information regarding this collection of information — including all background materials — can be found at <a href="https://www.reginfo.gov/public/do/PRAMain">https://www.reginfo.gov/public/do/PRAMain</a> by using the search function to enter either the title of the collection or <strong>2700-0174</strong>.</p>
      </div>

      <div class="notice-box">
        <h3>Privacy Act Statement</h3>
        <p>Pursuant to the Privacy Act of 1974, 5 U.S.C. § 552a, the following statement is furnished to individuals applying to become ground study subjects for the Flight Analog Project.</p>
        <ul>
          <li><strong>Authority:</strong> This collection is authorized by 51 U.S.C. 20113(a) and 44 U.S.C. 3101.</li>
          <li><strong>Purpose:</strong> We will use the information to determine eligibility for the Flight Analog Project, including initial screening and evaluation to become a ground study subject. The information will also be used in conjunction with additional information collected via other downstream processes to assess compatibility with research protocol requirements for data collection and ground mission assignments if the applicant is selected to become a ground study subject.</li>
          <li><strong>Routine Uses:</strong> Any disclosures of information will be compatible with the purpose for which the Agency collected the information. Records and information in this system may be disclosed under the following Routine Uses: (1) To other individuals or organizations, including Federal, State, or local agencies, and nonprofit, educational, or private entities, who are participating in NASA programs or are otherwise furthering the understanding or application of biological, physiological, and behavioral phenomena as reflected in the data contained in this system of records; (2) to external biomedical professionals and independent entities to support internal and external reviews for purposes of research quality assurance; (3) to international partners for research activities pursuant to NASA Space Act agreements; (4) to external professionals conducting research, studies, or other activities through arrangements or agreements with NASA and for mutual benefit; and (5) in accordance with all NASA Standard Routine Uses, which can be found in the “Routine Uses” section of the System of Records Notice (SORN), <strong>Human Experimental and Research Data Record (NASA 10HERD)</strong> at <a href="https://www.federalregister.gov/documents/2015/11/05/2015-28254/privacy-act-of-1974-privacy-act-system-of-records">https://www.federalregister.gov/documents/2015/11/05/2015-28254/privacy-act-of-1974-privacy-act-system-of-records</a>.</li>
          <li><strong>Effect of Nondisclosure:</strong> Supplying the information is voluntary on your part. However, failing to provide all or part of the information may prevent an accurate and timely decision on your application.</li>
        </ul>
      </div>

      <h3>Application Overview &amp; Consent</h3>
      <p>The following questions determine the flight analog study or studies for which you are best suited. This site does not use cookies or tracking.</p>
      <p>Your responses will be reviewed. By submitting this form, you consent to adding your responses to the analog subject registry. After reviewing your responses, further information may be requested. You may withdraw your information from this registry at any time by sending an e-mail to the recruitment team at <a href="mailto:{html.escape(contact_email)}">{html.escape(contact_email)}</a>.</p>
      <p><strong>Citizenship Requirement:</strong> You must be a United States Citizen or Green Card holder to apply to participate as a ground test subject through this application. Submitted applications are processed equally regardless of the type of citizenship status selected.</p>

      <h3>Mission Confinement Duration Disclosures</h3>
      <ul>
        <li><strong>20-Foot Chamber Confinement Duration:</strong> The Chamber mission duration includes a confined isolation of <strong>15 days</strong>. This is in addition to 2–3 days of pre-mission screening, 2–3 days of pre-mission data collection, 5 days of pre-mission training, and 3 days of post-mission training, totaling up to <strong>29 days</strong> of experiment duration.</li>
        <li><strong>MMEA Confinement Duration:</strong> The MMEA mission duration includes a confined isolation of <strong>~366 days</strong>. This is in addition to ~2 weeks of pre-mission screening, 6 weeks of pre-mission data collection and training, and 2 weeks of post-mission data collection and debriefing, totaling up to <strong>14 – 15 months</strong> of experiment participation.</li>
      </ul>

      <h3>Complete Screening &amp; Eligibility Questionnaire</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Screening Question / Field</th>
            <th>Options / Validation Rules</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>1</td><td>Are you a United States citizen?</td><td>Yes / No (If No, asks Question 2)</td></tr>
          <tr><td>2</td><td>Are you a Green Card holder?</td><td>Yes / No (Disqualification if both US Citizen and Green Card holder are No: <em>"You must be a United States citizen or a Green Card holder to apply."</em>)</td></tr>
          <tr><td>3</td><td>For which analogs are you interested in applying?</td><td>Checkboxes: <strong>Chamber</strong> (ID 19), <strong>MMEA</strong> (ID 20)</td></tr>
          <tr><td>4</td><td>Are you willing to be confined and isolated for the required periods of time?</td><td>Yes / No (Disqualification if No: <em>"In order to be considered, you must be willing to be confined."</em>)</td></tr>
          <tr><td>5</td><td>First Name</td><td>Required text (max 49 characters)</td></tr>
          <tr><td>6</td><td>Middle Name</td><td>Optional text (max 49 characters)</td></tr>
          <tr><td>7</td><td>Last Name</td><td>Required text (max 49 characters)</td></tr>
          <tr><td>8</td><td>Phone Number</td><td>Required text (max 20 characters)</td></tr>
          <tr><td>9</td><td>Email Address &amp; Confirm Email Address</td><td>Required valid email address (must match confirmation)</td></tr>
          <tr><td>10</td><td>Are you currently between the ages of 25 and 60?</td><td>Yes / No (Required)</td></tr>
          <tr><td>11</td><td>Birthdate</td><td>Required date</td></tr>
          <tr><td>12</td><td>Sex</td><td>Male / Female (Required; if Female, asks Question 32 on pregnancy in the past 6 months)</td></tr>
          <tr><td>13</td><td>Height (in inches)</td><td>Required integer (12 to 100 inches; MMEA height limit is 6'2" / 74 inches)</td></tr>
          <tr><td>14</td><td>Weight (in pounds)</td><td>Required integer (1 to 350 pounds)</td></tr>
          <tr><td>15</td><td>Were you born outside of the United States?</td><td>Yes / No (If Yes, select birth country from 251 non-US countries)</td></tr>
          <tr><td>16</td><td>Do you hold citizenship in any country other than the United States?</td><td>Yes / No (If Yes, select one or more additional citizenship countries)</td></tr>
          <tr><td>17</td><td>Where are you currently living?</td><td>Country selector (252 countries; if US, select State/Territory from 51 options)</td></tr>
          <tr><td>18</td><td>Do you use any tobacco or nicotine products (check all that apply)?</td><td>None, Smoking, Vaping, Chewing, Skin Patches, Other</td></tr>
          <tr><td>19</td><td>Do you have any metal in your body other than dental work?</td><td>Yes / No (Required)</td></tr>
          <tr><td>20</td><td>Are you claustrophobic?</td><td>Yes / No (Required)</td></tr>
          <tr><td>21</td><td>Do you have any of the following?</td><td>Checkboxes: Allergies (Including Food), GI Disorders, Dietary Limitations</td></tr>
          <tr><td>22</td><td>Any cardiovascular or circulatory system issues?</td><td>Yes / No (Required)</td></tr>
          <tr><td>23</td><td>Any neurological problems?</td><td>Yes / No (Required)</td></tr>
          <tr><td>24</td><td>Any musculoskeletal problems?</td><td>Yes / No (Required)</td></tr>
          <tr><td>25</td><td>Are you taking any medications?</td><td>Yes / No (Required)</td></tr>
          <tr><td>26</td><td>Any pharmacological treatment for ADHD?</td><td>Yes / No (Required)</td></tr>
          <tr><td>27</td><td>Are you currently on Bis-Phosphonate (for example, Fosamax, Boniva, or Reclast)?</td><td>Yes / No (Required)</td></tr>
          <tr><td>28</td><td>Are you currently on any medication that might increase chances of seizures?</td><td>Yes / No (Required)</td></tr>
          <tr><td>29</td><td>Any sleep disorders such as sleep walking or use of sleep aids?</td><td>Yes / No (Required)</td></tr>
          <tr><td>30</td><td>Any psychiatric disorders or history of mental illness?</td><td>Yes / No (Required)</td></tr>
          <tr><td>31</td><td>Any motion sickness (not a disqualifier)?</td><td>Yes / No (Required)</td></tr>
          <tr><td>32</td><td>Any history of kidney stones?</td><td>Yes / No (Required)</td></tr>
          <tr><td>33</td><td>Any history of ulcers?</td><td>Yes / No (Required)</td></tr>
          <tr><td>34</td><td>Any history of epilepsy or other disorders of the brain?</td><td>Yes / No (Required)</td></tr>
          <tr><td>35</td><td>Any history of blood clots?</td><td>Yes / No (Required)</td></tr>
          <tr><td>36</td><td>Any history of close relatives with seizures?</td><td>Yes / No (Required)</td></tr>
          <tr><td>37</td><td>Have you been pregnant in the past 6 months?</td><td>Yes / No (Required if Sex is Female)</td></tr>
          <tr><td>38</td><td>Have you ever experienced virtual reality?</td><td>Yes / No (If Yes, asks: <em>"Any simulation/motion sickness following VR (not a disqualifier)?"</em> Yes / No)</td></tr>
          <tr><td>39</td><td>Do you have a military background?</td><td>Yes / No (If Yes, asks: <em>"Please provide a brief description of your military background"</em>, max 250 chars)</td></tr>
          <tr><td>40</td><td>What is your highest level of education in a STEM related field?</td><td>High School, Associates, Bachelor, Master of Science, PhD, Medical Doctor, Other (If High School, Associates, Bachelor, or Other, asks: <em>"Equivalent of 2 years full time grad work?"</em> and <em>"Minimum of 2 years professional experience?"</em>)</td></tr>
          <tr><td>41</td><td>Employment Status</td><td>Checkboxes: US Civil Servant, Government Contractor, Military, Other (explain, max 50 chars), Unemployed</td></tr>
          <tr><td>42</td><td>How did you hear about participation in NASA Analogs?</td><td>Professional Organization, Recruitment Announcement, Conference, Social Media, Referred by friend or colleague, Online Search, Other (explain, max 50 chars)</td></tr>
          <tr><td>43</td><td>Additional Information</td><td>Optional textarea: <em>"Please provide any additional information you wish us to consider in your application"</em> (max 500 characters)</td></tr>
        </tbody>
      </table>

      <h3>Post-Submission Process &amp; Certification</h3>
      <p>Submitting voluntary information constitutes your consent to the use of the information for the stated purpose. By clicking the 'Apply' or 'Submit' buttons on any of the site’s forms, you are providing voluntary consent to use the information submitted for the purpose stated.</p>
      <p>The final determination for clearance is the responsibility of NASA. You may be provided the opportunity to personally explain, refute, or clarify any information before a final decision is made.</p>
      <p><strong>Follow-Up Timeline:</strong> Typically, all applicants will be contacted within <strong>5 to 7 business days</strong> following the submission of this application regarding its status. Eligible applicants may then be asked to provide additional screening information such as a professional resume or CV and must pass a federal background check.</p>
      <p>Contact us at <a href="mailto:{html.escape(contact_email)}">{html.escape(contact_email)}</a> if you have any additional questions.</p>
      <p><em>Certification Declaration: "I declare that I have examined all the information I provided on this form and certify it is true, complete and correct to the best of my knowledge."</em></p>
    </section>
    """

    # Landing page (`/`) combines the landing overview + executive summary of both analogs + links to all subpages
    home_body_html = f"""
    <section class="card">
      <h2>NASA Human Research Program — Flight Analog Subject Recruitment</h2>
      <p>NASA's Human Research Program (HRP) employs human analog missions in various environments to simulate aspects of human space exploration missions including to the Moon and Mars. An analog mission is an activity conducted on Earth with characteristics that resemble the challenges of a space mission such as isolation and confinement. The analog missions are used to conduct research informing the future direction of human exploration of the solar system. Analog mission participants support NASA’s objectives by participating in relevant and valuable scientific research.</p>
      <p>All highly motivated individuals with an interest in contributing to space exploration are invited to review the information and if interested to submit the application for consideration as a NASA analog crew member.</p>

      <h3>Active JSC Flight Analog Recruitment Programs</h3>
      <ul>
        <li>
          <strong><a href="{SITE_ORIGIN}/chamber">20-Foot Chamber Analog (Exploration Atmosphere)</a></strong> —
          Up to 15-day overnight study in Building 7 at NASA Johnson Space Center validating denitrogenation protocols to reduce the risk of decompression sickness during extravehicular activity (EVA) spacewalks (up to 29 days total including pre/post-mission screening, training, and testing).
          <ul>
            <li><a href="{SITE_ORIGIN}/chamber/about/requirements">20-Foot Chamber Participant Requirements &amp; Exclusion Criteria</a></li>
            <li><a href="{SITE_ORIGIN}/chamber/news">20-Foot Chamber in the News (HESTIA Tour)</a></li>
            <li><a href="https://www.nasa.gov/setmo/facilities/20-foot-chamber/">NASA 20-Foot Chamber Facility Page</a></li>
          </ul>
        </li>
        <li>
          <strong><a href="{SITE_ORIGIN}/mmea">Moon &amp; Mars Exploration Analog (MMEA)</a></strong> —
          Approximately 1-year confined mission in Building 220 at NASA Johnson Space Center (starting no earlier than August 2027; 14–15 months total participation) simulating interplanetary transit (Transit Vehicle, 682 sq. ft.) and planetary surface operations (Surface Habitat, ~1,700 sq. ft. 3D-printed Martian habitat, and Rover Vehicle).
          <ul>
            <li><a href="{SITE_ORIGIN}/mmea/about/requirements">MMEA Participant Requirements &amp; Astronaut-like Selection Criteria</a></li>
            <li><a href="{SITE_ORIGIN}/mmea/about/platforms">MMEA Platforms &amp; Floorplans (Transit Vehicle, Surface Habitat, Rover Vehicle)</a></li>
            <li><a href="{SITE_ORIGIN}/mmea/about/missions">MMEA Mission Definition, Fidelity, Dietary Conditions &amp; Crew Schedule</a></li>
            <li><a href="{SITE_ORIGIN}/mmea/news">MMEA in the News</a></li>
            <li><a href="https://www.nasa.gov/analog-missions/">NASA Analog Missions Portal</a></li>
          </ul>
        </li>
        <li>
          <strong><a href="{SITE_ORIGIN}/apply">Application for Analog Missions (/apply)</a></strong> —
          Online screening questionnaire, Paperwork Reduction Act Statement (OMB Control No. 2700-0174), and Privacy Act Statement (NASA 10HERD).
        </li>
      </ul>
    </section>
    {chamber_overview_html}
    {mmea_overview_html}
    """

    pages_spec = [
        {
            "route": "/",
            "rel_files": ["index.html"],
            "title": "NASA Human Research Program — Flight Analog Subject Recruitment",
            "section": "Home / Overview",
            "body": home_body_html,
            "priority": "1.0",
            "raw_Check": comp_texts["main"],
        },
        {
            "route": "/chamber",
            "rel_files": ["chamber/index.html", "chamber.html"],
            "title": "20-Foot Chamber Analog — Exploration Atmosphere Study",
            "section": "20-Foot Chamber Analog",
            "body": chamber_overview_html + chamber_requirements_html,
            "priority": "0.9",
            "raw_Check": comp_texts["chamber_main"],
        },
        {
            "route": "/chamber/about",
            "rel_files": ["chamber/about/index.html", "chamber/about.html"],
            "title": "About the 20-Foot Chamber Analog & Participant Requirements",
            "section": "20-Foot Chamber — About",
            "body": chamber_overview_html + chamber_requirements_html,
            "priority": "0.8",
            "raw_Check": comp_texts["chamber_req"],
        },
        {
            "route": "/chamber/about/requirements",
            "rel_files": [
                "chamber/about/requirements/index.html",
                "chamber/about/requirements.html",
            ],
            "title": "20-Foot Chamber — Human Research Participant Requirements & Exclusion Criteria",
            "section": "20-Foot Chamber — Participant Requirements",
            "body": chamber_requirements_html,
            "priority": "0.9",
            "raw_Check": comp_texts["chamber_req"],
        },
        {
            "route": "/chamber/news",
            "rel_files": ["chamber/news/index.html", "chamber/news.html"],
            "title": "20-Foot Chamber in the News",
            "section": "20-Foot Chamber — News",
            "body": chamber_news_html,
            "priority": "0.8",
            "raw_Check": [ json.dumps(news_data.get("chamber", []), sort_keys=True) ],
        },
        {
            "route": "/mmea",
            "rel_files": ["mmea/index.html", "mmea.html"],
            "title": "Moon & Mars Exploration Analog (MMEA)",
            "section": "Moon & Mars Exploration Analog (MMEA)",
            "body": mmea_overview_html + mmea_requirements_html,
            "priority": "0.9",
            "raw_Check": comp_texts["mmea_main"],
        },
        {
            "route": "/mmea/about",
            "rel_files": ["mmea/about/index.html", "mmea/about.html"],
            "title": "About MMEA — Requirements, Platforms & Missions Overview",
            "section": "MMEA — Complete Overview",
            "body": mmea_overview_html
            + mmea_requirements_html
            + mmea_platforms_html
            + mmea_missions_html,
            "priority": "0.85",
            "raw_Check": comp_texts["mmea_req"]
            + comp_texts["mmea_platforms"]
            + comp_texts["mmea_missions"],
        },
        {
            "route": "/mmea/about/requirements",
            "rel_files": [
                "mmea/about/requirements/index.html",
                "mmea/about/requirements.html",
            ],
            "title": "MMEA — Participant Requirements & Selection Criteria",
            "section": "MMEA — Participant Requirements",
            "body": mmea_requirements_html,
            "priority": "0.9",
            "raw_Check": comp_texts["mmea_req"],
        },
        {
            "route": "/mmea/about/platforms",
            "rel_files": [
                "mmea/about/platforms/index.html",
                "mmea/about/platforms.html",
            ],
            "title": "MMEA Platforms — Transit Vehicle, Surface Habitat & Rover Vehicle",
            "section": "MMEA — Habitat & Vehicle Platforms (Building 220)",
            "body": mmea_platforms_html,
            "priority": "0.9",
            "raw_Check": comp_texts["mmea_platforms"],
        },
        {
            "route": "/mmea/about/missions",
            "rel_files": [
                "mmea/about/missions/index.html",
                "mmea/about/missions.html",
            ],
            "title": "MMEA Missions — Mission Definition, Fidelity, Diet & Crew Schedule",
            "section": "MMEA — Mission Definition & Fidelity",
            "body": mmea_missions_html,
            "priority": "0.9",
            "raw_Check": comp_texts["mmea_missions"],
        },
        {
            "route": "/mmea/news",
            "rel_files": ["mmea/news/index.html", "mmea/news.html"],
            "title": "MMEA in the News",
            "section": "MMEA — News",
            "body": mmea_news_html,
            "priority": "0.8",
            "raw_Check": [ json.dumps(news_data.get("mmea", []), sort_keys=True) ],
        },
        {
            "route": "/apply",
            "rel_files": ["apply/index.html", "apply.html"],
            "title": "Application for Analog Missions — Screening Questionnaire & Privacy Act Statement",
            "section": "Application for Analog Missions",
            "body": apply_body_html,
            "priority": "0.95",
            "raw_Check": extract_js_strings_for_selector(
                js_text, "FlightAnalogRecruiting-application"
            ),
        },
    ]

    def append_dynamic_js_diff(body_html: str, raw_strings: list[str]) -> str:
        """
        If future deployments of `main-*.js` add new text strings or modify announcements
        not already present in `body_html`, automatically append them to the page so
        both the static HTML mirror and content hash reflect the update.
        """
        if not raw_strings:
            return body_html
        plain_body = re.sub(r"<[^>]+>", " ", html.unescape(body_html)).lower()
        body_toks = re.findall(r"[a-z0-9]+", plain_body)
        body_bigrams = set(zip(body_toks, body_toks[1:]))
        novel_items = []
        for st in raw_strings:
            toks = re.findall(r"[a-z0-9]+", html.unescape(st).lower())
            if len(toks) < 5:
                continue
            st_bigrams = set(zip(toks, toks[1:]))
            if not st_bigrams:
                continue
            overlap = len(st_bigrams & body_bigrams) / len(st_bigrams)
            if overlap < 0.78 and st not in novel_items:
                novel_items.append(st)
        if not novel_items:
            return body_html
        items_li = "\n".join(f"        <li>{html.escape(item)}</li>" for item in novel_items)
        return (
            body_html
            + f"""
    <section class="card">
      <h3>Additional Live Portal Updates</h3>
      <ul>
{items_li}
      </ul>
    </section>
    """
        )

    rendered_pages = []
    for spec in pages_spec:
        body_with_diff = append_dynamic_js_diff(spec["body"], spec.get("raw_Check", []))
        html_doc = render_page_shell(
            title=spec["title"],
            route_path=spec["route"],
            section_label=spec["section"],
            body_html=body_with_diff,
            live_data=live_data,
            contact_email=contact_email,
        )
        content_hash = hashlib.sha256(html_doc.encode("utf-8")).hexdigest()[:16]
        for rel_path in spec["rel_files"]:
            full_path = os.path.join(BASE_DIR, rel_path)
            write_if_changed(full_path, html_doc)

        mirror_url = (
            f"{GH_PAGES_BASE}/"
            if spec["route"] == "/"
            else f"{GH_PAGES_BASE}{spec['route']}"
        )
        direct_url = (
            f"{SITE_ORIGIN}/"
            if spec["route"] == "/"
            else f"{SITE_ORIGIN}{spec['route']}"
        )
        rendered_pages.append(
            {
                "route": spec["route"],
                "title": spec["title"],
                "mirror_url": mirror_url,
                "direct_url": direct_url,
                "priority": spec["priority"],
                "content_hash": content_hash,
            }
        )

    return rendered_pages


EXTERNAL_REFERENCE_URLS = [
    {
        "url": "https://www.nasa.gov/setmo/facilities/20-foot-chamber/",
        "title": "20-Foot Chamber at NASA Johnson Space Center (HESTIA Facility)",
        "priority": "0.85",
    },
    {
        "url": "https://ntrs.nasa.gov/api/citations/20160003488/downloads/20160003488.pdf",
        "title": "Human Exploration System Test-Bed for Integration and Advancement (HESTIA) Support of Future NASA Deep-Space Missions (NTRS 20160003488 PDF)",
        "priority": "0.80",
    },
    {
        "url": "https://www.nasa.gov/analog-missions/",
        "title": "NASA Analog Missions Overview",
        "priority": "0.85",
    },
    {
        "url": "https://www.nasa.gov/hrp/nasa-seeks-volunteers-for-yearlong-simulated-mission-to-moon-mars/",
        "title": "NASA Seeks Volunteers for Yearlong Simulated Mission to Moon, Mars (MMEA)",
        "priority": "0.85",
    },
    {
        "url": "https://www.nasa.gov/mission/altitude-chambers/",
        "title": "NASA Altitude Chambers Mission Overview",
        "priority": "0.80",
    },
    {
        "url": "https://www.nasa.gov/reference/analog-missions-why-do-we-use-analogs/",
        "title": "Analog Missions: Why Do We Use Analogs?",
        "priority": "0.80",
    },
    {
        "url": "https://www.nasa.gov/humans-in-space/step-1-earth-analogs-help-advance-missions-to-moon-mars/",
        "title": "Step 1: Earth Analogs Help Advance Missions to Moon, Mars",
        "priority": "0.80",
    },
    {
        "url": "https://www.federalregister.gov/documents/2015/11/05/2015-28254/privacy-act-of-1974-privacy-act-system-of-records",
        "title": "Privacy Act of 1974; System of Records Notice — Human Experimental and Research Data Record (NASA 10HERD)",
        "priority": "0.75",
    },
]


def build_sitemap_xml(entries: list[dict]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for e in entries:
        lines.append("  <url>")
        lines.append(f"    <loc>{html.escape(e['loc'])}</loc>")
        lines.append(f"    <lastmod>{html.escape(e['lastmod'])}</lastmod>")
        lines.append("    <changefreq>weekly</changefreq>")
        lines.append(f"    <priority>{html.escape(e['priority'])}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def main():
    print("=" * 72)
    print("NASA Flight Analog Subject Recruitment (analogstudies.jsc.nasa.gov)")
    print("Scraper, Pre-Renderer & Sitemap Generator")
    print("=" * 72)

    existing_catalog = load_existing_catalog()
    existing_mirror_dates = load_existing_sitemap_dates(SITEMAP_FILE)
    existing_direct_dates = load_existing_sitemap_dates(DIRECT_SITEMAP_FILE)
    prev_hashes = existing_catalog.get("pageHashes", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.verify = False

    print("[1/4] Querying SignalR CommonHub for live recruitment status...")
    live_data = fetch_signalr_live_data(
        session, existing_catalog.get("liveData", {})
    )
    print(
        f"      Chamber AcceptingApplications={live_data['chamberStatus'].get('AcceptingApplications')} | "
        f"MMEA AcceptingApplications={live_data['mmeaStatus'].get('AcceptingApplications')} | "
        f"Official={live_data['nasaOfficial'].get('Name')}"
    )

    print("[2/4] Fetching Angular SPA bundle from analogstudies.jsc.nasa.gov...")
    main_js_name, js_text = fetch_angular_bundle(session)
    print(f"      Downloaded {main_js_name} ({len(js_text):,} bytes)")
    mirror_mmea_images(session)

    print("[3/4] Pre-rendering static HTML mirror pages...")
    rendered_pages = build_pages(js_text, live_data)
    print(f"      Pre-rendered {len(rendered_pages)} routes")

    print("[4/4] Generating XML sitemaps and URL lists (preserving unchanged <lastmod>)...")
    mirror_entries = []
    direct_entries = []
    new_hashes = {}

    for p in rendered_pages:
        route = p["route"]
        chash = p["content_hash"]
        new_hashes[route] = chash

        if prev_hashes.get(route) == chash and p["mirror_url"] in existing_mirror_dates:
            m_lastmod = existing_mirror_dates[p["mirror_url"]]
        else:
            m_lastmod = today

        if prev_hashes.get(route) == chash and p["direct_url"] in existing_direct_dates:
            d_lastmod = existing_direct_dates[p["direct_url"]]
        else:
            d_lastmod = today

        mirror_entries.append(
            {
                "loc": p["mirror_url"],
                "lastmod": m_lastmod,
                "priority": p["priority"],
            }
        )
        direct_entries.append(
            {
                "loc": p["direct_url"],
                "lastmod": d_lastmod,
                "priority": p["priority"],
            }
        )

    for ext in EXTERNAL_REFERENCE_URLS:
        u = ext["url"]
        m_lastmod = existing_mirror_dates.get(u, today)
        d_lastmod = existing_direct_dates.get(u, today)
        mirror_entries.append(
            {"loc": u, "lastmod": m_lastmod, "priority": ext["priority"]}
        )
        direct_entries.append(
            {"loc": u, "lastmod": d_lastmod, "priority": ext["priority"]}
        )

    mirror_xml = build_sitemap_xml(mirror_entries)
    direct_xml = build_sitemap_xml(direct_entries)

    write_if_changed(SITEMAP_FILE, mirror_xml)
    write_if_changed(SITEMAP_ROOT_FILE, mirror_xml)
    write_if_changed(DIRECT_SITEMAP_FILE, direct_xml)

    mirror_urls_txt = "\n".join(e["loc"] for e in mirror_entries) + "\n"
    direct_urls_txt = "\n".join(e["loc"] for e in direct_entries) + "\n"
    write_if_changed(URLS_FILE, mirror_urls_txt)
    write_if_changed(DIRECT_URLS_FILE, direct_urls_txt)

    catalog_payload = {
        "siteOrigin": SITE_ORIGIN,
        "ghPagesBase": GH_PAGES_BASE,
        "bundleName": main_js_name,
        "liveData": live_data,
        "pageHashes": new_hashes,
        "pages": rendered_pages,
        "externalReferences": EXTERNAL_REFERENCE_URLS,
    }
    write_if_changed(
        CATALOG_FILE, json.dumps(catalog_payload, indent=2, sort_keys=True) + "\n"
    )

    print(
        f"Done! Total URLs in sitemap: {len(mirror_entries)} "
        f"({len(rendered_pages)} pre-rendered SPA routes + {len(EXTERNAL_REFERENCE_URLS)} linked NASA references)."
    )


if __name__ == "__main__":
    main()
