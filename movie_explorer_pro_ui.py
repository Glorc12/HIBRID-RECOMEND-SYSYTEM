from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
from typing import Any, Optional

import requests
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

APP_TITLE = "Movie Explorer Pro"
APP_VERSION = "1.0.0"
BASE_DIR = Path(__file__).resolve().parent
POSTER_CACHE = BASE_DIR / "poster_cache_ui"
POSTER_CACHE.mkdir(exist_ok=True)

def _env(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    return value.rstrip("/")

BACKEND_API_URL = _env("BACKEND_API_URL", "https://downloading-fleet-purchase-cleaning.trycloudflare.com/")
BACKEND_TIMEOUT = float(os.getenv("BACKEND_TIMEOUT", "45"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))

app = FastAPI(title=APP_TITLE, version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class RecommendationRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    topn: int = Field(default=12, ge=1, le=50)
    mode: str = Field(default="hybrid")


class ExplanationRequest(BaseModel):
    query_title: str = Field(..., min_length=1, max_length=200)
    rec_title: str = Field(..., min_length=1, max_length=200)


def backend_url(path: str) -> str:
    return f"{BACKEND_API_URL}{path}"


def forward_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None
    url = backend_url(path)
    for _ in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                timeout=BACKEND_TIMEOUT,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "MovieExplorerPro/1.0.0",
                },
            )
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception as e:
                raise HTTPException(status_code=502, detail="Backend returned invalid JSON") from e
        except Exception as e:
            last_error = e
    raise HTTPException(status_code=502, detail=f"Backend unavailable: {type(last_error).__name__ if last_error else 'unknown'}")


def _safe_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return default
    return s


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for item in value:
            s = _safe_text(item)
            if s:
                out.append(s)
        return out
    s = _safe_text(value)
    if not s:
        return []
    for sep in ["|", ",", ";", "/"]:
        if sep in s:
            parts = [_safe_text(x) for x in s.split(sep)]
            parts = [x for x in parts if x]
            if len(parts) > 1:
                return parts
    return [s]


def _poster_value(item: dict[str, Any]) -> str:
    poster = _safe_text(item.get("poster_url")) or _safe_text(item.get("poster_path")) or _safe_text(item.get("poster"))
    if not poster:
        return ""
    if poster.startswith("http://") or poster.startswith("https://"):
        return poster
    if poster.startswith("/"):
        return "https://image.tmdb.org/t/p/w500" + poster
    return poster


def _placeholder_svg(title: str = "No poster", year: str = "", genres: str = "") -> bytes:
    title = html.escape(title)[:80]
    year = html.escape(year)[:20]
    genres = html.escape(genres)[:48]
    lines = []
    if year:
        lines.append(year)
    if genres:
        lines.append(genres)
    meta = " • ".join(lines)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="500" height="750" viewBox="0 0 500 750">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#0f172a"/>
          <stop offset="100%" stop-color="#1d4ed8"/>
        </linearGradient>
        <radialGradient id="r" cx="50%" cy="35%" r="75%">
          <stop offset="0%" stop-color="#60a5fa" stop-opacity="0.36"/>
          <stop offset="100%" stop-color="#0f172a" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <rect width="500" height="750" fill="url(#g)"/>
      <rect width="500" height="750" fill="url(#r)"/>
      <rect x="28" y="28" width="444" height="694" rx="28" fill="rgba(255,255,255,0.04)" stroke="rgba(255,255,255,0.12)"/>
      <text x="250" y="220" fill="#dbeafe" font-family="Inter,Arial,sans-serif" font-size="30" font-weight="700" text-anchor="middle">Poster unavailable</text>
      <text x="250" y="272" fill="#ffffff" font-family="Inter,Arial,sans-serif" font-size="24" font-weight="700" text-anchor="middle">{title}</text>
      <text x="250" y="330" fill="#bfdbfe" font-family="Inter,Arial,sans-serif" font-size="18" text-anchor="middle">{meta}</text>
      <text x="250" y="610" fill="#dbeafe" font-family="Inter,Arial,sans-serif" font-size="18" text-anchor="middle">Movie Explorer Pro</text>
    </svg>"""
    return svg.encode("utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    ok = False
    try:
        r = requests.get(backend_url("/"), timeout=10)
        ok = r.status_code == 200
    except Exception:
        ok = False
    return {
        "status": "ok",
        "app": APP_TITLE,
        "version": APP_VERSION,
        "backend_url": BACKEND_API_URL,
        "backend_reachable": ok,
    }


@app.post("/api/recommendations")
def api_recommendations(request: RecommendationRequest) -> dict[str, Any]:
    payload = forward_json("/recommend/", request.model_dump())
    try:
        print("\n=== API /recommend/ response ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("=== /recommend/ response end ===\n")
    except Exception:
        pass
    recommendations = payload.get("recommendations", [])
    if isinstance(recommendations, list):
        cleaned: list[dict[str, Any]] = []
        for item in recommendations:
            if not isinstance(item, dict):
                continue
            rec = dict(item)
            rec["poster_url"] = _poster_value(rec)
            cleaned.append(rec)
        payload["recommendations"] = cleaned
    return payload


@app.post("/api/explain")
def api_explain(request: ExplanationRequest) -> dict[str, Any]:
    return forward_json("/explain/", request.model_dump())


@app.get("/api/poster-placeholder")
def poster_placeholder(
    title: str = Query(default="No poster"),
    year: str = Query(default=""),
    genres: str = Query(default=""),
) -> Response:
    return Response(
        content=_placeholder_svg(title=title or "No poster", year=year, genres=genres),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )



@app.get("/api/poster-proxy")
async def poster_proxy(url: str = Query(...)) -> Response:
    cache_key = hashlib.md5(url.encode()).hexdigest()
    cache_file = POSTER_CACHE / cache_key

    if cache_file.exists() and cache_file.stat().st_size > 1000:
        return Response(
            content=cache_file.read_bytes(),
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.themoviedb.org/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            })
            resp.raise_for_status()
            content = resp.content
            cache_file.write_bytes(content)
            content_type = resp.headers.get("content-type", "image/jpeg")
            return Response(
                content=content,
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                },
            )
    except Exception:
        return Response(
            content=_placeholder_svg(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(APP_TITLE)}</title>
  <style>
    :root {{
      --bg: #07111f;
      --bg2: #0b1628;
      --card: rgba(13, 23, 40, 0.82);
      --card-strong: rgba(11, 17, 30, 0.96);
      --line: rgba(148, 163, 184, 0.16);
      --line-strong: rgba(148, 163, 184, 0.22);
      --text: #e5eefb;
      --muted: #9bb0ca;
      --accent: #60a5fa;
      --accent2: #8b5cf6;
      --good: #22c55e;
      --warn: #f59e0b;
      --shadow: 0 22px 80px rgba(0,0,0,0.42);
      --radius: 24px;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      min-height: 100%;
      background:
        radial-gradient(circle at top left, rgba(96,165,250,0.18), transparent 28%),
        radial-gradient(circle at top right, rgba(139,92,246,0.16), transparent 24%),
        linear-gradient(180deg, var(--bg), var(--bg2));
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: .45;
      background-image:
        linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px);
      background-size: 64px 64px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,.55), transparent 88%);
    }}
    .shell {{
      position: relative;
      max-width: 1500px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }}
    .hero {{
      position: relative;
      overflow: hidden;
      padding: 28px;
      border-radius: 30px;
      border: 1px solid var(--line);
      background: linear-gradient(135deg, rgba(96,165,250,.16), rgba(139,92,246,.12));
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
      letter-spacing: .14em;
      text-transform: uppercase;
      margin-bottom: 12px;
    }}
    .eyebrow span {{
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      box-shadow: 0 0 0 8px rgba(96,165,250,.10);
    }}
    h1 {{
      margin: 0;
      font-size: clamp(32px, 4vw, 58px);
      line-height: 1.02;
      letter-spacing: -0.05em;
    }}
    .subhead {{
      margin: 14px 0 0;
      max-width: 980px;
      color: var(--muted);
      line-height: 1.7;
      font-size: 16px;
    }}
    .toolbar {{
      display: grid;
      grid-template-columns: 1.6fr .7fr .5fr auto;
      gap: 14px;
      align-items: end;
      margin-top: 24px;
    }}
    .field label {{
      display: block;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .field input, .field select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 15px 16px;
      background: rgba(5, 12, 24, 0.60);
      color: var(--text);
      outline: none;
      font-size: 15px;
      backdrop-filter: blur(12px);
      transition: border-color .18s ease, box-shadow .18s ease, transform .18s ease;
    }}
    .field input:focus, .field select:focus {{
      border-color: rgba(96,165,250,.55);
      box-shadow: 0 0 0 4px rgba(96,165,250,.12);
    }}
    .button {{
      border: none;
      border-radius: 16px;
      padding: 15px 18px;
      min-width: 170px;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      color: white;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 14px 32px rgba(96,165,250,.25);
      transition: transform .18s ease, filter .18s ease;
    }}
    .button:hover {{ transform: translateY(-1px); filter: brightness(1.05); }}
    .button:disabled {{ opacity: .65; cursor: not-allowed; transform: none; }}
    .statusbar {{
      min-height: 24px;
      margin-top: 18px;
      display: flex;
      gap: 12px;
      align-items: center;
      color: var(--muted);
      font-size: 14px;
    }}
    .spinner {{
      width: 18px;
      height: 18px;
      border-radius: 999px;
      border: 2px solid rgba(255,255,255,0.18);
      border-top-color: var(--accent);
      animation: spin .85s linear infinite;
      display: none;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .content {{
      margin-top: 22px;
      display: grid;
      grid-template-columns: 1.9fr 1.05fr;
      gap: 18px;
      align-items: start;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--card);
      backdrop-filter: blur(16px);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .panel-head {{
      padding: 18px 18px 0;
    }}
    .panel-title {{
      margin: 0;
      font-size: 18px;
      letter-spacing: -.02em;
    }}
    .panel-subtitle {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      padding: 18px;
    }}
    .card {{
      position: relative;
      overflow: hidden;
      border: 1px solid rgba(148,163,184,.14);
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(15,23,42,.88), rgba(8,17,31,.98));
      cursor: pointer;
      transition: transform .2s ease, border-color .2s ease, box-shadow .2s ease;
      min-height: 430px;
    }}
    .card:hover {{
      transform: translateY(-4px);
      border-color: rgba(96,165,250,.42);
      box-shadow: 0 18px 36px rgba(0,0,0,.28);
    }}
    .poster-wrap {{
      position: relative;
      width: 100%;
      aspect-ratio: 2 / 3;
      overflow: hidden;
      background: linear-gradient(135deg, rgba(96,165,250,.18), rgba(139,92,246,.18));
    }}
    .poster {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      background: rgba(255,255,255,.03);
    }}
    .poster-badge {{
      position: absolute;
      left: 12px;
      top: 12px;
      z-index: 2;
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(34,197,94,.15);
      border: 1px solid rgba(34,197,94,.28);
      color: #b7f7d0;
      font-size: 12px;
      font-weight: 700;
      backdrop-filter: blur(8px);
    }}
    .poster-overlay {{
      position: absolute;
      inset: auto 0 0 0;
      padding: 24px 14px 14px;
      background: linear-gradient(180deg, transparent, rgba(0,0,0,.86));
    }}
    .poster-title {{
      margin: 0;
      font-size: 18px;
      line-height: 1.2;
      letter-spacing: -.02em;
    }}
    .poster-year {{
      margin-top: 6px;
      color: rgba(255,255,255,.78);
      font-size: 13px;
    }}
    .card-body {{
      padding: 15px;
    }}
    .title {{
      margin: 0;
      font-size: 17px;
      line-height: 1.25;
      letter-spacing: -.02em;
    }}
    .original {{
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
    }}
    .meta {{
      margin-top: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid rgba(148,163,184,.16);
      background: rgba(255,255,255,.03);
      border-radius: 999px;
      padding: 6px 10px;
      line-height: 1;
    }}
    .summary {{
      margin-top: 12px;
      color: var(--muted);
      line-height: 1.62;
      font-size: 13px;
      min-height: 78px;
    }}
    .detail {{
      padding: 18px;
    }}
    .detail-box {{
      padding: 16px;
      border-radius: 20px;
      background: var(--card-strong);
      border: 1px solid rgba(148,163,184,.14);
      margin-bottom: 14px;
    }}
    .detail-box h3 {{
      margin: 0 0 10px;
      font-size: 16px;
    }}
    .detail-box p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.7;
      font-size: 14px;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .kv {{
      padding: 12px 14px;
      border-radius: 16px;
      border: 1px solid rgba(148,163,184,.12);
      background: rgba(255,255,255,.03);
    }}
    .kv .k {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }}
    .kv .v {{
      display: block;
      font-size: 14px;
      line-height: 1.5;
    }}
    .tags {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 10px;
    }}
    .tag {{
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(96,165,250,.10);
      border: 1px solid rgba(96,165,250,.18);
      color: #d8e8ff;
      font-size: 13px;
    }}
    .empty {{
      grid-column: 1 / -1;
      padding: 36px 18px 42px;
      color: var(--muted);
      text-align: center;
    }}
    .skeleton {{
      position: relative;
      overflow: hidden;
      background: rgba(255,255,255,.035);
    }}
    .skeleton::after {{
      content: "";
      position: absolute;
      inset: 0;
      transform: translateX(-100%);
      background: linear-gradient(90deg, transparent, rgba(255,255,255,.10), transparent);
      animation: shimmer 1.15s infinite;
    }}
    @keyframes shimmer {{ to {{ transform: translateX(100%); }} }}
    .modal {{
      position: fixed;
      inset: 0;
      background: rgba(1,6,16,.66);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
      z-index: 40;
    }}
    .modal.open {{ display: flex; }}
    .modal-card {{
      width: min(1120px, 100%);
      max-height: 90vh;
      overflow: auto;
      border-radius: 26px;
      border: 1px solid rgba(148,163,184,.18);
      background: #07111f;
      box-shadow: 0 24px 80px rgba(0,0,0,.48);
    }}
    .modal-head {{
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 16px 18px;
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: center;
      border-bottom: 1px solid rgba(148,163,184,.12);
      background: rgba(7,17,31,.95);
      backdrop-filter: blur(10px);
    }}
    .close {{
      width: 40px;
      height: 40px;
      border-radius: 12px;
      border: 1px solid rgba(148,163,184,.16);
      background: rgba(255,255,255,.04);
      color: var(--text);
      font-size: 18px;
      cursor: pointer;
    }}
    .modal-body {{
      display: grid;
      grid-template-columns: 320px 1fr;
    }}
    .modal-poster {{
      width: 100%;
      min-height: 480px;
      object-fit: cover;
      display: block;
      background: rgba(255,255,255,.03);
    }}
    .modal-info {{
      padding: 18px;
    }}
    .modal-info h2 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.1;
      letter-spacing: -.03em;
    }}
    .modal-info .meta {{
      margin-top: 12px;
    }}
    .section {{
      margin-top: 18px;
    }}
    .section h3 {{
      margin: 0 0 10px;
      font-size: 16px;
    }}
    .section p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.75;
      font-size: 14px;
    }}
    .section-row {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 18px;
    }}
    .mini {{
      padding: 14px 16px;
      border-radius: 18px;
      border: 1px solid rgba(148,163,184,.12);
      background: rgba(255,255,255,.03);
    }}
    .mini .label {{
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }}
    .mini .value {{
      font-size: 14px;
      line-height: 1.5;
    }}
    .footer-note {{
      margin-top: 12px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 1120px) {{
      .toolbar {{ grid-template-columns: 1fr 1fr; }}
      .content {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr 1fr; }}
      .modal-body {{ grid-template-columns: 1fr; }}
      .modal-poster {{ min-height: 360px; }}
    }}
    @media (max-width: 720px) {{
      .shell {{ padding: 16px 12px 28px; }}
      .hero {{ padding: 20px; border-radius: 24px; }}
      .toolbar {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      .detail-grid {{ grid-template-columns: 1fr; }}
      .section-row {{ grid-template-columns: 1fr; }}
      .card {{ min-height: 0; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow"><span></span>Movie recommendation interface</div>
      <h1>Production-ready movie search with clean cards and poster fallbacks</h1>
      <p class="subhead">The app talks only to your API, shows title, original title, year, rating, genres, explanation and tags, and falls back to a local SVG poster when an image cannot load.</p>

      <div class="toolbar">
        <div class="field">
          <label for="title">Movie title or query</label>
          <input id="title" type="text" placeholder="Матрица, Интерстеллар, Операция Ы..." />
        </div>
        <div class="field">
          <label for="mode">Mode</label>
          <select id="mode">
            <option value="hybrid" selected>Hybrid</option>
            <option value="semantic">Semantic</option>
            <option value="context">Context</option>
          </select>
        </div>
        <div class="field">
          <label for="topn">Results</label>
          <select id="topn">
            <option value="6">6</option>
            <option value="12" selected>12</option>
            <option value="18">18</option>
            <option value="24">24</option>
            <option value="30">30</option>
          </select>
        </div>
        <button id="searchBtn" class="button">Search</button>
      </div>

      <div class="statusbar">
        <div id="spinner" class="spinner"></div>
        <div id="statusText">Ready.</div>
      </div>
    </section>

    <div class="content">
      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">Recommendations</h2>
          <p class="panel-subtitle">Click any card to open the movie details panel.</p>
        </div>
        <div id="results" class="grid">
          <div class="empty">Enter a title and press Search.</div>
        </div>
      </section>

      <aside class="panel">
        <div class="panel-head">
          <h2 class="panel-title">Details</h2>
          <p class="panel-subtitle">Selected movie metadata and explanation.</p>
        </div>
        <div id="detail" class="detail">
          <div class="detail-box">
            <h3>No movie selected</h3>
            <p>Click a recommendation card to inspect genres, release date, language, runtime, rating and the explanation returned by your API.</p>
          </div>
          <div class="detail-grid">
            <div class="kv"><span class="k">Genres</span><span class="v">—</span></div>
            <div class="kv"><span class="k">Language</span><span class="v">—</span></div>
            <div class="kv"><span class="k">Release year</span><span class="v">—</span></div>
            <div class="kv"><span class="k">Rating</span><span class="v">—</span></div>
          </div>
        </div>
      </aside>
    </div>
  </div>

  <div id="modal" class="modal" aria-hidden="true">
    <div class="modal-card" role="dialog" aria-modal="true">
      <div class="modal-head">
        <div>
          <div id="modalTitle" style="font-size:14px;color:var(--muted);">Selected movie</div>
          <div id="modalSubtitle" style="font-size:18px;font-weight:700;margin-top:4px;">—</div>
        </div>
        <button id="closeModal" class="close">×</button>
      </div>
      <div class="modal-body">
        <img id="modalPoster" class="modal-poster" alt="Poster">
        <div class="modal-info">
          <h2 id="modalName">—</h2>
          <div id="modalMeta" class="meta"></div>

          <div class="section">
            <h3>Overview</h3>
            <p id="modalOverview">—</p>
          </div>

          <div class="section">
            <h3>Reason</h3>
            <p id="modalReason">—</p>
          </div>

          <div class="section">
            <h3>Tags</h3>
            <div id="modalTags" class="tags"></div>
          </div>

          <div class="section-row">
            <div class="mini">
              <div class="label">Director</div>
              <div class="value" id="modalDirector">—</div>
            </div>
            <div class="mini">
              <div class="label">Cast</div>
              <div class="value" id="modalCast">—</div>
            </div>
            <div class="mini">
              <div class="label">Production</div>
              <div class="value" id="modalCompanies">—</div>
            </div>
            <div class="mini">
              <div class="label">Countries</div>
              <div class="value" id="modalCountries">—</div>
            </div>
          </div>

          <div class="footer-note">Poster fallback is local and does not depend on external proxy services.</div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const resultsEl = document.getElementById("results");
    const detailEl = document.getElementById("detail");
    const spinner = document.getElementById("spinner");
    const statusText = document.getElementById("statusText");
    const searchBtn = document.getElementById("searchBtn");
    const titleEl = document.getElementById("title");
    const modeEl = document.getElementById("mode");
    const topnEl = document.getElementById("topn");
    const modal = document.getElementById("modal");
    const closeModal = document.getElementById("closeModal");
    const modalName = document.getElementById("modalName");
    const modalSubtitle = document.getElementById("modalSubtitle");
    const modalPoster = document.getElementById("modalPoster");
    const modalMeta = document.getElementById("modalMeta");
    const modalOverview = document.getElementById("modalOverview");
    const modalReason = document.getElementById("modalReason");
    const modalTags = document.getElementById("modalTags");
    const modalDirector = document.getElementById("modalDirector");
    const modalCast = document.getElementById("modalCast");
    const modalCompanies = document.getElementById("modalCompanies");
    const modalCountries = document.getElementById("modalCountries");

    const escapeHtml = (text) => String(text ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");

    function setLoading(state) {{
      spinner.style.display = state ? "inline-block" : "none";
      searchBtn.disabled = state;
      titleEl.disabled = state;
      modeEl.disabled = state;
      topnEl.disabled = state;
      statusText.textContent = state ? "Loading recommendations..." : "Ready.";
    }}

    function skeletonCards() {{
      return Array.from({{ length: 6 }}, () => `
        <div class="card skeleton" style="cursor:default">
          <div class="poster-wrap"></div>
          <div class="card-body">
            <div style="height:18px;width:80%;border-radius:8px;background:rgba(255,255,255,0.05);"></div>
            <div style="height:12px;width:56%;margin-top:10px;border-radius:8px;background:rgba(255,255,255,0.05);"></div>
            <div style="height:70px;width:100%;margin-top:12px;border-radius:14px;background:rgba(255,255,255,0.04);"></div>
          </div>
        </div>`).join("");
    }}

    function normalizeList(value) {{
      if (!value) return [];
      if (Array.isArray(value)) return value.filter(Boolean).map(String);
      const s = String(value).trim();
      if (!s) return [];
      for (const sep of ["|", ",", ";", "/"]) {{
        if (s.includes(sep)) {{
          const parts = s.split(sep).map(x => x.trim()).filter(Boolean);
          if (parts.length > 1) return parts;
        }}
      }}
      return [s];
    }}

    function normalizeGenres(genres) {{
      return normalizeList(genres);
    }}

    function chooseScoreValue(item, index, total, minScore, maxScore) {{
      const raw = Number(item.similarity ?? item.score ?? 0);
      if (Number.isFinite(raw) && maxScore > minScore) {{
        return Math.max(0, Math.min(100, ((raw + 1) / 2) * 100));
      }}
      if (total <= 1) return 100;
      return Math.max(55, 100 - (index * 8));
    }}

    function placeholderUrl(item) {{
      const title = encodeURIComponent(item.title ?? item.original_title ?? "No poster");
      const year = encodeURIComponent(item.release_year ?? (String(item.release_date ?? "").slice(0, 4)) ?? "");
      const genres = encodeURIComponent((normalizeGenres(item.genres).slice(0, 3).join(" • ")) || "");
      return `/api/poster-placeholder?title=${{title}}&year=${{year}}&genres=${{genres}}`;
    }}

    function resolvePosterUrl(item) {{
      const poster = item.poster_url ?? item.poster_path ?? item.poster ?? "";
      if (!poster) return placeholderUrl(item);
      if (poster.startsWith("data:image/")) return poster;
      if (poster.startsWith("/api/")) return poster;
      if (poster.startsWith("http://") || poster.startsWith("https://")) return poster;
      if (poster.startsWith("/")) return `https://image.tmdb.org/t/p/w500${{poster}}`;
      return placeholderUrl(item);
    }}

    function getDirectPosterUrl(item) {{
      const poster = item.poster_url ?? item.poster_path ?? item.poster ?? "";
      if (!poster) return null;
      if (poster.startsWith("data:image/")) return poster;
      if (poster.startsWith("http://") || poster.startsWith("https://")) return poster;
      if (poster.startsWith("/")) return `https://image.tmdb.org/t/p/w500${{poster}}`;
      return null;
    }}

    function renderEmpty(message) {{
      resultsEl.innerHTML = `<div class="empty">${{escapeHtml(message)}}</div>`;
    }}

    function renderResults(data) {{
      const items = Array.isArray(data.recommendations) ? data.recommendations : [];
      if (!items.length) {{
        renderEmpty("No recommendations returned by the API.");
        return;
      }}

      const scores = items.map(item => Number(item.similarity ?? item.score)).filter(Number.isFinite);
      const minScore = scores.length ? Math.min(...scores) : 0;
      const maxScore = scores.length ? Math.max(...scores) : 0;

      resultsEl.innerHTML = items.map((item, index) => {{
        const title = item.title ?? item.name ?? "Untitled";
        const original = item.original_title ?? "";
        const poster = resolvePosterUrl(item);
        const directPoster = getDirectPosterUrl(item) || placeholderUrl(item);
        const overview = item.overview ?? item.reason ?? item.tagline ?? "";
        const genres = normalizeGenres(item.genres).slice(0, 3);
        const year = item.release_year ?? (String(item.release_date ?? "").slice(0, 4) || "—");
        const language = item.original_language ?? "—";
        const score = chooseScoreValue(item, index, items.length, minScore, maxScore);
        const reason = item.reason ?? item.explanation ?? "";
        const voteAvg = item.vote_average ?? item.imdb_rating ?? "—";
        const voteCount = item.vote_count ?? item.imdb_votes ?? "—";

        const posterFallback = placeholderUrl(item);

        return `
          <article class="card" data-index="${{index}}">
            <div class="poster-wrap">
              <div class="poster-badge">${{score.toFixed(0)}}%</div>
              <img class="poster" src="${{escapeHtml(poster)}}" alt="${{escapeHtml(title)}}" loading="eager" decoding="async"
                onerror="this.onerror=null;this.src='${{escapeHtml(posterFallback)}}';">
              <div class="poster-overlay">
                <h3 class="poster-title">${{escapeHtml(title)}}</h3>
                <div class="poster-year">${{escapeHtml(year)}}${{original ? ' • ' + escapeHtml(original) : ''}}</div>
              </div>
            </div>
            <div class="card-body">
              <h3 class="title">${{escapeHtml(title)}}</h3>
              ${{original ? `<div class="original">${{escapeHtml(original)}}</div>` : ""}}
              <div class="meta">
                <span class="chip">${{escapeHtml(year)}}</span>
                <span class="chip">Rating ${{escapeHtml(voteAvg)}}</span>
                <span class="chip">Votes ${{escapeHtml(voteCount)}}</span>
                <span class="chip">${{escapeHtml(language)}}</span>
                ${{genres.map(g => `<span class="chip">${{escapeHtml(g)}}</span>`).join("")}}
              </div>
              <div class="summary">${{escapeHtml(overview || reason || "Click to inspect full details.")}}</div>
            </div>
          </article>`;
      }}).join("");

      [...resultsEl.querySelectorAll(".card")].forEach((card, i) => {{
        card.addEventListener("click", () => openDetail(items[i]));
      }});
    }}

    function openDetail(item) {{
      const title = item.title ?? item.name ?? "Untitled";
      const poster = resolvePosterUrl(item);
      const overview = item.overview ?? item.tagline ?? "No overview available.";
      const reason = item.reason ?? item.explanation ?? "No explanation available.";
      const genres = normalizeGenres(item.genres);
      const year = item.release_year ?? (String(item.release_date ?? "").slice(0, 4) || "—");
      const language = item.original_language ?? "—";
      const rating = item.vote_average ?? item.imdb_rating ?? "—";
      const runtime = item.runtime ?? "—";
      const voteCount = item.vote_count ?? item.imdb_votes ?? "—";
      const releaseDate = item.release_date ?? "—";
      const director = item.director ?? "—";
      const cast = normalizeList(item.cast).slice(0, 5);
      const companies = normalizeList(item.production_companies).slice(0, 4);
      const countries = normalizeList(item.production_countries).slice(0, 4);
      const tagline = item.tagline ?? "—";

      modalName.textContent = title;
      modalSubtitle.textContent = item.original_title ? item.original_title : title;
      modalPoster.src = poster;
      modalPoster.alt = title;
      modalPoster.onerror = function() {{
        if (this.dataset.tried) {{
          this.src = placeholderUrl(item);
          this.onerror = null;
        }} else {{
          this.dataset.tried = "1";
          this.src = getDirectPosterUrl(item) || placeholderUrl(item);
        }}
      }};

      modalMeta.innerHTML = [
        year !== "—" ? `<span class="chip">${{escapeHtml(year)}}</span>` : "",
        language !== "—" ? `<span class="chip">${{escapeHtml(language)}}</span>` : "",
        rating !== "—" ? `<span class="chip">Rating ${{escapeHtml(rating)}}</span>` : "",
        runtime !== "—" ? `<span class="chip">${{escapeHtml(runtime)}} min</span>` : "",
        voteCount !== "—" ? `<span class="chip">${{escapeHtml(voteCount)}} votes</span>` : "",
        releaseDate !== "—" ? `<span class="chip">${{escapeHtml(releaseDate)}}</span>` : ""
      ].join("");

      modalOverview.textContent = overview;
      modalReason.textContent = reason;

      const tags = [
        ...genres.map(g => `<span class="tag">${{escapeHtml(g)}}</span>`),
        tagline !== "—" ? `<span class="tag">${{escapeHtml(tagline)}}</span>` : "",
        director !== "—" ? `<span class="tag">Director: ${{escapeHtml(director)}}</span>` : "",
        ...cast.map(x => `<span class="tag">${{escapeHtml(x)}}</span>`),
        ...companies.map(x => `<span class="tag">${{escapeHtml(x)}}</span>`),
        ...countries.map(x => `<span class="tag">${{escapeHtml(x)}}</span>`),
      ].filter(Boolean);

      modalTags.innerHTML = tags.length ? tags.join("") : `<span class="tag">No tags provided</span>`;

      modalDirector.textContent = director;
      modalCast.textContent = cast.length ? cast.join(", ") : "—";
      modalCompanies.textContent = companies.length ? companies.join(", ") : "—";
      modalCountries.textContent = countries.length ? countries.join(", ") : "—";

      detailEl.innerHTML = `
        <div class="detail-box">
          <h3>${{escapeHtml(title)}}</h3>
          <p>${{escapeHtml(reason || overview || "No details available.")}}</p>
        </div>
        <div class="detail-grid">
          <div class="kv"><span class="k">Genres</span><span class="v">${{genres.length ? genres.map(escapeHtml).join(", ") : "—"}}</span></div>
          <div class="kv"><span class="k">Language</span><span class="v">${{escapeHtml(language)}}</span></div>
          <div class="kv"><span class="k">Release year</span><span class="v">${{escapeHtml(year)}}</span></div>
          <div class="kv"><span class="k">Rating</span><span class="v">${{escapeHtml(rating)}}</span></div>
        </div>
        <div class="detail-box" style="margin-top:14px;">
          <h3>Overview</h3>
          <p>${{escapeHtml(overview)}}</p>
        </div>
        <div class="detail-box" style="margin-top:14px;">
          <h3>Poster status</h3>
          <p>The app uses the raw poster URL first and falls back to a local SVG poster card if the browser cannot load the image.</p>
        </div>`;

      modal.classList.add("open");
      modal.setAttribute("aria-hidden", "false");
    }}

    function closeDetail() {{
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
    }}

    closeModal.addEventListener("click", closeDetail);
    modal.addEventListener("click", (e) => {{ if (e.target === modal) closeDetail(); }});
    document.addEventListener("keydown", (e) => {{ if (e.key === "Escape") closeDetail(); }});

    async function searchMovies() {{
      const title = titleEl.value.trim();
      const mode = modeEl.value;
      const topn = Number(topnEl.value);

      if (!title) {{
        statusText.textContent = "Enter a title first.";
        return;
      }}

      setLoading(true);
      resultsEl.innerHTML = skeletonCards();

      try {{
        const response = await fetch("/api/recommendations", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ title, mode, topn }})
        }});
        const data = await response.json();
        console.log("API response:", data);
        console.table(data.recommendations || []);
        if (!response.ok) throw new Error(data?.detail || "Request failed");
        statusText.textContent = `Loaded ${{data.recommendations?.length ?? 0}} results for "${{title}}".`;
        renderResults(data);
      }} catch (error) {{
        console.error(error);
        statusText.textContent = `Error: ${{error.message}}`;
        renderEmpty(`Request failed: ${{error.message}}`);
      }} finally {{
        setLoading(false);
      }}
    }}

    searchBtn.addEventListener("click", searchMovies);
    titleEl.addEventListener("keydown", (e) => {{ if (e.key === "Enter") searchMovies(); }});
    renderEmpty("Enter a title and press Search.");
  </script>
</body>
</html>"""


if __name__ == "__main__":
    print(f"Starting {APP_TITLE} v{APP_VERSION}")
    print(f"Backend API: {BACKEND_API_URL}")
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "7861")), log_level="info")