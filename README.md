# NASA Flight Analog Subject Recruitment (`analogstudies.jsc.nasa.gov`) — Pre-Rendered Mirror & Sitemap Generator

Automated harvester, static HTML pre-renderer, and XML sitemap generator for **NASA's Human Research Program (HRP) Flight Analog Subject Recruitment** portal at Lyndon B. Johnson Space Center (`https://analogstudies.jsc.nasa.gov`).

## Why Pre-Rendering Is Needed for RAG Ingestion

`https://analogstudies.jsc.nasa.gov` is an Angular 17 Single-Page Application (`<flightanalogrecruiting-root></flightanalogrecruiting-root>`) backed by an ASP.NET Core SignalR hub (`/CommonHub`):

1. **Dynamic SignalR Status (`/CommonHub`)**: Live recruitment status (`AcceptingApplications`, `Note`) for the **20-Foot Chamber** (`ID: 19`) and **Moon & Mars Exploration Analog (MMEA)** (`ID: 20`), as well as Responsible NASA Official metadata and active platform definitions, are streamed asynchronously over SignalR after initial page load.
2. **Conditional Screening Questionnaire (`/apply`)**: The 43-question candidate screening form (`FlightAnalogRecruiting-application`) is gated behind `*ngIf="showAllQuestions()"`, which remains hidden in headless browser crawlers unless a user interactively selects US Citizenship / Green Card status, target analog(s), and extended confinement consent.
3. **1-to-1 URL Rewriting Compatibility**: By pre-rendering semantic static HTML for every Angular route at matching relative paths on GitHub Pages (`https://oht8woowi8yait9n.github.io/analogstudies/<route>`), Onyx can ingest 100% of the portal's content while rewriting source links directly back to `https://analogstudies.jsc.nasa.gov/<route>`.

---

## Published Sitemaps & URL Lists

| File | URL / Path | Description |
| :--- | :--- | :--- |
| **`analogstudies_sitemap.xml`** | `https://oht8woowi8yait9n.github.io/analogstudies/analogstudies_sitemap.xml` | Primary XML sitemap (12 pre-rendered GitHub Pages mirror routes + 8 linked NASA reference pages & NTRS PDF) |
| **`sitemap.xml`** | `https://oht8woowi8yait9n.github.io/analogstudies/sitemap.xml` | Root alias of `analogstudies_sitemap.xml` |
| **`analogstudies_direct_sitemap.xml`** | `https://oht8woowi8yait9n.github.io/analogstudies/analogstudies_direct_sitemap.xml` | Direct XML sitemap pointing 100% to `https://analogstudies.jsc.nasa.gov/...` and `https://www.nasa.gov/...` |
| **`analogstudies_urls.txt`** | `https://oht8woowi8yait9n.github.io/analogstudies/analogstudies_urls.txt` | Plain-text URL list (mirror + linked NASA references) |
| **`analogstudies_direct_urls.txt`** | `https://oht8woowi8yait9n.github.io/analogstudies/analogstudies_direct_urls.txt` | Plain-text URL list (direct NASA URLs) |
| **`analogstudies_catalog.json`** | `https://oht8woowi8yait9n.github.io/analogstudies/analogstudies_catalog.json` | Structured metadata, live SignalR status, and SHA-256 page hashes for zero-churn CI/CD |

---

## Pre-Rendered SPA Routes (12 Routes)

Each route is generated at both `<route>/index.html` and `<route>.html`:

- `/` — NASA HRP Flight Analog Subject Recruitment Home & Program Overview
- `/chamber` — 20-Foot Chamber Analog (Exploration Atmosphere) Overview & Requirements
- `/chamber/about` — About the 20-Foot Chamber Analog
- `/chamber/about/requirements` — 20-Foot Chamber Participant Requirements & Exclusion Criteria (29-day total commitment, 15-day chamber isolation, VO2 peak requirements)
- `/chamber/news` — 20-Foot Chamber in the News (HESTIA 20-Foot Chamber Tour & NTRS references)
- `/mmea` — Moon & Mars Exploration Analog (MMEA) Overview & Requirements
- `/mmea/about` — Complete MMEA Overview (Requirements, Platforms & Missions)
- `/mmea/about/requirements` — MMEA Participant Requirements & Astronaut-Like Selection Criteria (ages 30–55, height limit 6'2", STEM degree & experience substitutions)
- `/mmea/about/platforms` — MMEA Platforms in Building 220 at JSC (Transit Vehicle 682 sq. ft., Surface Habitat ~1,700 sq. ft. 3D-printed Martian habitat, Rover Vehicle, and floorplan figures)
- `/mmea/about/missions` — MMEA Mission Definition (~1-year mission starting NET August 2027), Reference Conditions, Mission Fidelity, Dietary Conditions, Crew Daily Schedule, and Research Demonstrations
- `/mmea/news` — MMEA in the News & NASA Analog Mission References
- `/apply` — Application for Analog Missions (Paperwork Reduction Act OMB Control No. 2700-0174, Privacy Act Statement NASA 10HERD, Confinement Disclosures, and all 43 screening questions)

---

## Onyx Web Connector & Proxy URL Rewrite Rule

To rewrite the GitHub Pages mirror URLs back to canonical `https://analogstudies.jsc.nasa.gov` URLs in Onyx, add this single prefix mapping in `url_rewrites.json`:

```json
"https://oht8woowi8yait9n.github.io/analogstudies": "https://analogstudies.jsc.nasa.gov"
```

---

## Local Execution

```bash
pip install -r requirements.txt
python3 generate_analogstudies_sitemap.py
```
