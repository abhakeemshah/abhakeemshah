#!/usr/bin/env python3
"""Render the profile README cards into cards/*.svg.

    pip install fonttools
    python scripts/render_readme_cards.py

Reads data/contributions.json (refreshed daily by the GitHub Action) and writes:

    hero.svg             name + one line about me
    tile-*.svg           three link tiles (README shows each at width 32.5%)
    activity.svg         contributions per month, streak, active days, busiest day
    repo-N.svg           one card per featured repo
    graph.svg            contribution graph in the brand colour

Every SVG is self-contained: an inline <style> with CSS keyframes, SMIL <animate>
elements, and WOFF fonts subset to the glyphs that card uses, as base64.
Output is deterministic, so the Action only commits when the data changes.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import math
import os
import re
import sys
import urllib.request
from collections import OrderedDict
from html import escape
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC", "").lower() in {"1", "true", "yes"}

# ───────────────────────────── edit these ─────────────────────────────
LOGIN = "abhakeemshah"
NAME = "Abdul Hakeem Shah"
TAGLINE = "I build software and write down what I learn along the way."
BRAND = "#2F80ED"          # your brand colour: bars, graph cells, accents
# ───────────────────────────────────────────────────────────────────────

INK, SURFACE, LINE = "#0B1F3A", "#123B6D", "#2A5D91"
TEXT, MUTED, CELL0, SHEEN = "#FFFFFF", "#D9EAFE", "#1B4F86", "#BFE0FF"

# Fonts are fetched once into fonts/ (OFL licensed) and subset per card.
IBM = "https://raw.githubusercontent.com/IBM/plex/master/packages/plex-sans/fonts/complete/ttf/"
GF = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
FONTS = {
    "d": dict(family="DM Serif Display", weight=400, file="DMSerifDisplay-Regular.ttf",
              url=GF + "dmserifdisplay/DMSerifDisplay-Regular.ttf"),
    "b": dict(family="IBM Plex Sans", weight=400, file="IBMPlexSans-Regular.ttf",
              url=IBM + "IBMPlexSans-Regular.ttf"),
    "s": dict(family="IBM Plex Sans", weight=600, file="IBMPlexSans-SemiBold.ttf",
              url=IBM + "IBMPlexSans-SemiBold.ttf"),
}

MAX_REPOS = 6
REPO_START, REPO_END = "<!-- repo-cards:start -->", "<!-- repo-cards:end -->"

BASE_CSS = """
text{font-family:'IBM Plex Sans',-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-weight:400}
text.d{font-family:'DM Serif Display',Georgia,'Times New Roman',serif}
text.s{font-weight:600}
.l0{fill:%CELL0%}
.l1{fill:%BRAND%;fill-opacity:.3}
.l2{fill:%BRAND%;fill-opacity:.55}
.l3{fill:%BRAND%;fill-opacity:.8}
.l4{fill:%BRAND%}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
@keyframes fade{from{opacity:0}}
@keyframes grow{from{transform:scaleY(0)}}
@keyframes drift{to{transform:translate(-40px,26px)}}
@keyframes particleFloat{0%,100%{transform:translate3d(0,0,0) scale(1);opacity:.25}50%{transform:translate3d(18px,-24px,0) scale(1.35);opacity:.9}}
@keyframes codeOrbit{0%,100%{transform:translate3d(0,0,0) rotate(-4deg);opacity:.2}50%{transform:translate3d(-18px,16px,0) rotate(4deg);opacity:.75}}
@keyframes shimmer3d{0%,100%{filter:drop-shadow(0 0 2px %BRAND%);opacity:.35}50%{filter:drop-shadow(0 0 9px %BRAND%);opacity:1}}
.rise{animation:rise .7s cubic-bezier(.2,.7,.2,1) backwards}
.fade{animation:fade .6s ease-out backwards}
.grow{transform-box:fill-box;transform-origin:50% 100%;animation:grow .85s cubic-bezier(.2,.7,.2,1) backwards}
.drift{animation:drift 18s ease-in-out infinite alternate}
.particle{animation:particleFloat 5s ease-in-out infinite}
.code-orbit{animation:codeOrbit 8s ease-in-out infinite}
.particle-glow{animation:shimmer3d 3.5s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
""".replace("%CELL0%", CELL0).replace("%BRAND%", BRAND)

STATIC_CSS = """
.rise,.fade{opacity:1!important;transform:none!important;animation:none!important}
.grow,.drift,.particle,.code-orbit,.particle-glow{transform:none!important;animation:none!important;filter:none!important}
""".strip()


# ─────────────────────────────── helpers ───────────────────────────────
def n(v: float) -> str:
    """Compact number for SVG attributes."""
    s = f"{v:.1f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def esc(s: str) -> str:
    return escape(s, quote=True)


def short(v: int) -> str:
    return f"{v / 1000:.1f}".rstrip("0").rstrip(".") + "k" if v >= 10_000 else f"{v:,}"


def fmt_day(d: dt.date) -> str:
    return f"{d:%b} {d.day}"


class Face:
    """A full font file: measures text and produces per-card WOFF subsets."""

    def __init__(self, key: str, fonts_dir: Path):
        self.key, self.spec = key, FONTS[key]
        self.path = fonts_dir / self.spec["file"]
        tt = TTFont(self.path)
        self.cmap, self.hmtx, self.upem = tt.getBestCmap(), tt["hmtx"], tt["head"].unitsPerEm
        self._cache: dict[str, str] = {}

    def clean(self, s: str) -> str:
        """Collapse whitespace and drop glyphs this font lacks (emoji etc.)."""
        return " ".join("".join(c for c in s if ord(c) in self.cmap or c.isspace()).split())

    def width(self, s: str, size: float) -> float:
        return sum(self.hmtx[self.cmap[ord(c)]][0] for c in s) * size / self.upem

    def woff_b64(self, chars: set[str]) -> str:
        text = "".join(sorted(chars))
        if text not in self._cache:
            opts = subset.Options()
            opts.flavor, opts.hinting = "woff", False
            font = subset.load_font(str(self.path), opts)
            sub = subset.Subsetter(opts)
            sub.populate(text=text)
            sub.subset(font)
            buf = io.BytesIO()
            subset.save_font(font, buf, opts)
            self._cache[text] = base64.b64encode(buf.getvalue()).decode()
        return self._cache[text]


def ensure_fonts(fonts_dir: Path) -> None:
    fonts_dir.mkdir(parents=True, exist_ok=True)
    for spec in FONTS.values():
        dest = fonts_dir / spec["file"]
        if not dest.exists():
            print(f"downloading {spec['file']}")
            req = urllib.request.Request(spec["url"], headers={"User-Agent": "profile-cards"})
            with urllib.request.urlopen(req, timeout=60) as r:
                dest.write_bytes(r.read())


GRAPHQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    login
    name
    publicRepos: repositories(first: 1, ownerAffiliations: OWNER, privacy: PUBLIC) {
      totalCount
    }
    followers { totalCount }
    contributionsCollection {
      contributionCalendar {
        weeks {
          contributionDays { date contributionCount }
        }
      }
    }
    featuredRepos: repositories(
      first: 6
      ownerAffiliations: OWNER
      privacy: PUBLIC
      isFork: false
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      nodes {
        name
        url
        description
        stargazerCount
        forkCount
        primaryLanguage { name color }
      }
    }
  }
}
"""


def fetch_data(login: str, token: str) -> dict:
    payload = json.dumps({
        "query": GRAPHQL_QUERY,
        "variables": {"login": login},
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-cards",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errors") or not result.get("data", {}).get("user"):
        raise RuntimeError(f"GitHub GraphQL request failed: {result.get('errors', result)}")

    user = result["data"]["user"]
    repos = []
    for repo in user["featuredRepos"]["nodes"]:
        language = repo.get("primaryLanguage")
        repos.append({
            "name": repo["name"],
            "url": repo["url"],
            "description": repo.get("description"),
            "stars": repo["stargazerCount"],
            "forks": repo["forkCount"],
            "language": language["name"] if language else None,
            "language_color": language["color"] if language else None,
        })
    days = [
        {"date": day["date"], "count": day["contributionCount"]}
        for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]
        for day in week["contributionDays"]
    ]
    return {
        "login": user["login"],
        "name": user.get("name") or user["login"],
        "public_repos": user["publicRepos"]["totalCount"],
        "followers": user["followers"]["totalCount"],
        "stars": sum(repo["stars"] for repo in repos),
        "days": days,
        "featured": repos,
    }


def load_data(path: Path, login: str, token: str | None) -> dict:
    if token:
        data = fetch_data(login, token)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return data
    if not path.exists():
        raise RuntimeError(f"{path} not found and GH_TOKEN is unavailable")
    return json.loads(path.read_text(encoding="utf-8"))


FACES: dict[str, Face] = {}


class Card:
    def __init__(self, name: str, w: int, h: int, title: str, desc: str = ""):
        self.name, self.w, self.h, self.title, self.desc = name, w, h, title, desc
        self.defs: list[str] = []
        self.css: list[str] = []
        self.body: list[str] = []
        self.used: dict[str, set[str]] = {}
        self.defs.append(
            f'<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{SURFACE}"/><stop offset="1" stop-color="{INK}"/></linearGradient>'
            f'<clipPath id="card"><rect width="{w}" height="{h}" rx="18"/></clipPath>'
        )
        self.body.append(
            f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="18" fill="url(#bg)" stroke="{LINE}"/>'
        )

    def add(self, markup: str) -> None:
        self.body.append(markup)

    def text(self, x, y, s, font="b", size=16, fill=TEXT, anchor="start",
             anim: str | None = None, delay: int = 0, extra: str = "") -> str:
        s = FACES[font].clean(s)
        self.used.setdefault(font, set()).update(s)
        cls = font + (f" {anim}" if anim else "")
        style = f' style="animation-delay:{delay}ms"' if anim else ""
        anchor_attr = f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.body.append(
            f'<text class="{cls}" x="{n(x)}" y="{n(y)}" font-size="{n(size)}" fill="{fill}"'
            f'{anchor_attr}{style}{extra}>{esc(s)}</text>'
        )
        return s

    def svg(self) -> str:
        faces = "".join(
            "@font-face{font-family:'%s';font-style:normal;font-weight:%d;"
            "src:url(data:font/woff;base64,%s) format('woff')}"
            % (FONTS[k]["family"], FONTS[k]["weight"], FACES[k].woff_b64(chars))
            for k, chars in sorted(self.used.items())
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="t d">\n'
            f'<title id="t">{esc(self.title)}</title>\n<desc id="d">{esc(self.desc or self.title)}</desc>\n'
            f"<style>{faces}{BASE_CSS}{STATIC_CSS if STATIC else ''}{''.join(self.css)}</style>\n"
            f"<defs>{''.join(self.defs)}</defs>\n" + "\n".join(self.body) + "\n</svg>\n"
        )


def reveal(attr: str, start: float, end: float, delay: float, dur: float) -> str:
    """SMIL: hold at `start` for `delay` seconds, then ease to `end` and freeze."""
    if STATIC:
        return f'<set attributeName="{attr}" to="{n(end)}" fill="freeze"/>'
    total = delay + dur
    return (
        f'<animate attributeName="{attr}" values="{n(start)};{n(start)};{n(end)}" '
        f'keyTimes="0;{delay / total:.3f};1" dur="{total:.2f}s" fill="freeze" '
        f'calcMode="spline" keySplines="0 0 1 1;.2 .7 .2 1"/>'
    )


def fit_line(text: str, font: str, size: float, maxw: float, min_size: float):
    """Single line that shrinks, then truncates, to fit maxw."""
    face = FACES[font]
    text = face.clean(text)
    while face.width(text, size) > maxw and size > min_size:
        size -= 0.5
    while face.width(text, size) > maxw and len(text) > 1:
        text = text[:-2].rstrip() + "…"
    return text, size


def wrap(text: str, font: str, size: float, maxw: float, maxlines: int) -> list[str]:
    face = FACES[font]
    lines, cur = [], ""
    for word in face.clean(text).split(" "):
        trial = f"{cur} {word}".strip()
        if face.width(trial, size) <= maxw:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = word
        while face.width(cur, size) > maxw and len(cur) > 1:
            cur = cur[:-1]
    if cur:
        lines.append(cur)
    if len(lines) > maxlines:
        lines = lines[:maxlines]
        last = lines[-1]
        while last and face.width(last + "…", size) > maxw:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


# ───────────────────────────── data model ─────────────────────────────
def load_days(data: dict) -> list[dict]:
    days = sorted(data["days"], key=lambda d: d["date"])
    peak = max((d["count"] for d in days), default=0)
    for d in days:
        d["date"] = dt.date.fromisoformat(d["date"]) if isinstance(d["date"], str) else d["date"]
        if "level" not in d:
            d["level"] = 0 if d["count"] == 0 or peak == 0 else min(4, math.ceil(d["count"] / peak * 4))
    return days


def build_weeks(days: list[dict]) -> list[list[dict | None]]:
    """Sunday-first columns of 7 cells, like GitHub's calendar."""
    if not days:
        return []
    cells: list[dict | None] = [None] * ((days[0]["date"].weekday() + 1) % 7) + days
    cells += [None] * (-len(cells) % 7)
    return [cells[i:i + 7] for i in range(0, len(cells), 7)]


def month_totals(days: list[dict], months: int = 12) -> list[tuple[dt.date, int]]:
    totals: "OrderedDict[tuple[int, int], int]" = OrderedDict()
    for d in days:
        k = (d["date"].year, d["date"].month)
        totals[k] = totals.get(k, 0) + d["count"]
    return [(dt.date(y, m, 1), c) for (y, m), c in list(totals.items())[-months:]]


def current_streak(days: list[dict]) -> int:
    i = len(days) - 1
    if i >= 0 and days[i]["count"] == 0:
        i -= 1                      # today may simply not have a contribution yet
    streak = 0
    while i >= 0 and days[i]["count"] > 0:
        streak, i = streak + 1, i - 1
    return streak


# ──────────────────────────────── icons ────────────────────────────────
def arrow_icon(x, y, color=BRAND) -> str:
    return (f'<path transform="translate({n(x)} {n(y)})" d="M3 13 13 3M5.5 3H13v7.5" fill="none" '
            f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>')


def star_icon(x, y, r=7.5, color=MUTED) -> str:
    pts = []
    for i in range(10):
        rad = r if i % 2 == 0 else r * 0.42
        a = -math.pi / 2 + i * math.pi / 5
        pts.append(f"{n(x + r + rad * math.cos(a))},{n(y + r + rad * math.sin(a))}")
    return f'<polygon points="{" ".join(pts)}" fill="{color}"/>'


def fork_icon(x, y, color=MUTED) -> str:
    return (f'<g transform="translate({n(x)} {n(y)})" fill="none" stroke="{color}" stroke-width="1.6" '
            f'stroke-linecap="round"><circle cx="3.5" cy="3" r="1.9"/><circle cx="12.5" cy="3" r="1.9"/>'
            f'<circle cx="8" cy="13" r="1.9"/><path d="M3.5 4.9V6.5q0 1.8 2 1.8h5q2 0 2-1.8V4.9M8 8.3v2.8"/></g>')


# ──────────────────────────────── cards ────────────────────────────────
def hero_card(data: dict, weeks) -> Card:
    W, H = 900, 260
    c = Card("hero", W, H, NAME, TAGLINE)
    sheen_motion = "" if STATIC else (
        '<animateTransform attributeName="gradientTransform" type="translate" '
        'from="0 0" to="1 0" dur="7s" repeatCount="indefinite"/>'
    )
    c.defs.append(
        f'<radialGradient id="glow"><stop offset="0" stop-color="{BRAND}" stop-opacity=".4"/>'
        f'<stop offset="1" stop-color="{BRAND}" stop-opacity="0"/></radialGradient>'
        f'<linearGradient id="sheen" x1="0" y1="0" x2="1" y2="0" spreadMethod="repeat">'
        f'<stop offset="0" stop-color="#fff"/><stop offset=".5" stop-color="{SHEEN}"/>'
        f'<stop offset="1" stop-color="#fff"/>'
        f'{sheen_motion}</linearGradient>'
        f'<radialGradient id="particle"><stop offset="0" stop-color="#fff" stop-opacity=".95"/>'
        f'<stop offset=".25" stop-color="{BRAND}" stop-opacity=".9"/>'
        f'<stop offset="1" stop-color="{BRAND}" stop-opacity="0"/></radialGradient>'
        f'<linearGradient id="hoodie" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="#9B8CFF"/><stop offset="1" stop-color="#40358F"/></linearGradient>'
        f'<linearGradient id="skin" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="#FFD8BC"/><stop offset=".7" stop-color="#D98D70"/>'
        f'<stop offset="1" stop-color="#9D554D"/></linearGradient>'
        f'<linearGradient id="hair" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="#30264B"/><stop offset="1" stop-color="#120E24"/></linearGradient>'
        f'<linearGradient id="shirt" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="#24295C"/><stop offset="1" stop-color="#11142F"/></linearGradient>'
        f'<filter id="softShadow"><feGaussianBlur stdDeviation="7"/></filter>'
    )
    c.add('<g clip-path="url(#card)"><circle class="drift" cx="790" cy="60" r="250" fill="url(#glow)"/></g>')

    # A small depth field: glowing particles and code fragments drift at different speeds.
    particles = [
        (590, 42, 3, 0), (635, 224, 2, 700), (715, 32, 4, 1100),
        (775, 218, 2, 1500), (846, 78, 3, 1900), (865, 190, 5, 2300),
        (552, 205, 2, 2700), (610, 78, 1.5, 3100), (740, 238, 2.5, 3500),
        (830, 35, 1.5, 3900),
    ]
    for x, y, radius, delay in particles:
        motion = "" if STATIC else (
            f'<animateTransform attributeName="transform" type="translate" '
            f'values="0 0;{(delay % 13) - 6} -12;0 0" dur="{4.5 + (delay % 5) / 2:.1f}s" '
            f'begin="{delay / 1000:.2f}s" repeatCount="indefinite"/>'
        )
        c.add(f'<circle class="particle particle-glow" cx="{x}" cy="{y}" r="{radius}" '
               f'fill="url(#particle)" style="animation-delay:{delay}ms">{motion}</circle>')

    code_fragments = [
        (575, 62, "</>", 0, 13), (790, 52, "git push", 900, 12),
        (548, 178, "python", 1700, 11), (820, 150, "{ }", 2500, 16),
        (705, 236, "const", 3300, 11), (855, 228, "SVG", 4200, 13),
    ]
    for x, y, word, delay, size in code_fragments:
        clean = FACES["b"].clean(word)
        used = set(clean)
        c.used.setdefault("b", set()).update(used)
        motion = "" if STATIC else (
            f'<animateTransform attributeName="transform" type="translate" '
            f'values="0 0;{-8 if delay % 2 else 8} 10;0 0" dur="7s" '
            f'begin="{delay / 1000:.2f}s" repeatCount="indefinite"/>'
        )
        markup = (f'<text class="b code-orbit" x="{x}" y="{y}" font-size="{size}" fill="{BRAND}" '
                  f'opacity=".55">{esc(clean)}{motion}</text>')
        c.add(markup)

    # The last 12 weeks of real activity, drawn as the same cells the graph uses.
    pitch, size = 18, 14
    recent = weeks[-12:]
    x0, y0 = W - 48 - len(recent) * pitch + (pitch - size), (H - (7 * pitch - 4)) / 2
    last = max((d["date"] for col in recent for d in col if d), default=None)
    for i, col in enumerate(recent):
        cells = []
        for r, d in enumerate(col):
            if not d:
                continue
            pulse = ('<animate attributeName="opacity" values="1;.25;1" dur="2.4s" repeatCount="indefinite"/>'
                     if d["date"] == last and not STATIC else "")
            cells.append(f'<rect class="l{d["level"]}" x="{n(x0 + i * pitch)}" y="{n(y0 + r * pitch)}" '
                         f'width="{size}" height="{size}" rx="3.5">{pulse}</rect>')
        c.add(f'<g class="fade" style="animation-delay:{500 + i * 70}ms">{"".join(cells)}</g>')

    # Name: shimmering gradient fill, sized to stay clear of the cells.
    name, nsize = FACES["d"].clean(NAME), 68.0
    nsize = min(nsize, 560 / FACES["d"].width(name, 1))
    c.text(48, 130, name, "d", nsize, "url(#sheen)", anim="rise", delay=80)

    # One line about me, typed in with a clip-path wipe and a blinking caret.
    line, lsize = fit_line(TAGLINE, "b", 20, 580, 15)
    lw = FACES["b"].width(line, lsize)
    c.defs.append(
        f'<clipPath id="type"><rect x="48" y="150" width="{n(lw + 4)}" height="44">'
        f'{reveal("width", 0, lw + 4, 0.6, 1.8)}</rect></clipPath>'
    )
    c.add('<g clip-path="url(#type)">')
    c.text(48, 182, line, "b", lsize, MUTED)
    c.add("</g>")
    caret_motion = "" if STATIC else (
        '<animate attributeName="opacity" values="1;0;1" dur="1s" repeatCount="indefinite"/>'
    )
    c.add(f'<rect x="{n(48 + lw + 3)}" y="{n(182 - lsize * .85)}" width="2" height="{n(lsize * 1.1)}" fill="{BRAND}">'
          f'{reveal("x", 48, 48 + lw + 3, 0.6, 1.8)}{caret_motion}</rect>')
    return c


def tile_card(idx: int, slug: str, value: int, label: str) -> Card:
    W, H = 300, 140
    c = Card(f"tile-{slug}", W, H, f"{label}: {value:,}")
    c.defs.append(
        f'<radialGradient id="tg" cx="1" cy="0" r="1"><stop offset="0" stop-color="{BRAND}" stop-opacity=".35"/>'
        f'<stop offset="1" stop-color="{BRAND}" stop-opacity="0"/></radialGradient>'
    )
    c.add(f'<g clip-path="url(#card)"><rect width="{W}" height="{H}" fill="url(#tg)"/></g>')
    c.text(26, 84, short(value), "d", 54, TEXT, anim="rise", delay=idx * 110)
    shown = c.text(26, 114, label, "b", 16, MUTED, anim="fade", delay=300 + idx * 110)
    lw = FACES["b"].width(shown, 16)
    # Underline = this tile is a link.
    c.add(f'<rect x="26" y="121" width="{n(lw)}" height="2" rx="1" fill="{BRAND}">'
          f'{reveal("width", 0, lw, 0.5 + idx * 0.12, 0.7)}</rect>')
    c.add(arrow_icon(W - 40, 22))
    return c


def activity_card(data: dict, days: list[dict]) -> Card:
    W, H = 900, 440
    months = month_totals(days)
    total = sum(d["count"] for d in days)
    c = Card("activity", W, H, "Contributions per month",
             f"{total:,} contributions over the last year, grouped by month.")
    c.text(48, 66, "Contributions per month", "d", 30, TEXT, anim="rise")
    c.text(W - 48, 64, f"{total:,} in the last year", "b", 15, MUTED, "end", anim="fade", delay=150)

    active = sum(1 for d in days if d["count"] > 0)
    busiest = max(days, key=lambda d: d["count"]) if days else {"count": 0, "date": dt.date.today()}
    stats = [
        (str(current_streak(days)), "days", "Current streak"),
        (str(active), f"of {len(days)} days", "Active days"),
        (str(busiest["count"]), f"on {fmt_day(busiest['date'])}", "Busiest day"),
    ]
    for i, (big, unit, label) in enumerate(stats):
        x, delay = 48 + i * 268, 200 + i * 100
        big = c.text(x, 148, big, "d", 46, TEXT, anim="rise", delay=delay)
        c.text(x + FACES["d"].width(big, 46) + 8, 148, unit, "b", 16, MUTED, anim="rise", delay=delay)
        c.text(x, 174, label, "b", 14, MUTED, anim="rise", delay=delay)

    base, top, slot, bw = 392, 226, (W - 96) / 12, 38
    peak = max((v for _, v in months), default=0) or 1
    peak_i = max(range(len(months)), key=lambda i: months[i][1]) if months else -1
    for i, (month, v) in enumerate(months):
        h = max(3, (base - top) * v / peak)
        x, y, r = 48 + i * slot + (slot - bw) / 2, base - h, min(7, h)
        hot = i == peak_i
        c.add(f'<path class="grow" style="animation-delay:{450 + i * 55}ms" fill="{BRAND}" '
              f'fill-opacity="{1 if hot else .5}" d="M{n(x)} {n(base)}V{n(y + r)}Q{n(x)} {n(y)} {n(x + r)} {n(y)}'
              f'H{n(x + bw - r)}Q{n(x + bw)} {n(y)} {n(x + bw)} {n(y + r)}V{n(base)}Z"/>')
        c.text(x + bw / 2, y - 9, f"{v:,}", "s" if hot else "b", 13, TEXT if hot else MUTED, "middle",
               anim="fade", delay=900 + i * 55)
        c.text(x + bw / 2, base + 26, f"{month:%b}", "b", 14, MUTED, "middle", anim="fade", delay=600 + i * 55)
    c.add(f'<line x1="48" y1="{base}" x2="48" y2="{base}" stroke="{LINE}" stroke-width="1.5" stroke-linecap="round">'
          f'{reveal("x2", 48, W - 48, 0.3, 1.2)}</line>')
    return c


def graph_card(data: dict, days: list[dict], weeks) -> Card:
    W, H = 900, 240
    total = sum(d["count"] for d in days)
    c = Card("graph", W, H, "Contribution graph", f"{total:,} contributions in the last year, one cell per day.")
    c.text(48, 60, "Contribution graph", "d", 28, TEXT, anim="rise")
    c.text(W - 48, 58, f"{total:,} contributions in the last year", "b", 15, MUTED, "end", anim="fade", delay=150)

    cell, gap, x0, y0 = 12, 3, 72, 94
    pitch = cell + gap
    gw, gh = len(weeks) * pitch - gap, 7 * pitch - gap

    last_label_col, last_month = -9, None
    for i, col in enumerate(weeks):
        first = next((d for d in col if d), None)
        if first and first["date"].month != last_month:
            last_month = first["date"].month
            if i - last_label_col >= 3:
                c.text(x0 + i * pitch, y0 - 12, f"{first['date']:%b}", "b", 12, MUTED, anim="fade", delay=300)
                last_label_col = i
    for r, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.text(x0 - 8, y0 + r * pitch + cell - 2, label, "b", 11, MUTED, "end", anim="fade", delay=300)

    for i, col in enumerate(weeks):
        rects = "".join(
            f'<rect class="l{d["level"]}" x="{x0 + i * pitch}" y="{y0 + r * pitch}" width="{cell}" height="{cell}" rx="3"/>'
            for r, d in enumerate(col) if d
        )
        c.add(f'<g class="fade" style="animation-delay:{200 + i * 16}ms">{rects}</g>')

    # A soft highlight sweeps across the grid now and then (SMIL, clipped to the cells).
    c.defs.append(
        f'<clipPath id="grid"><rect x="{x0}" y="{y0}" width="{gw}" height="{gh}"/></clipPath>'
        f'<linearGradient id="sweep"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
        f'<stop offset=".5" stop-color="#fff" stop-opacity=".28"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
    )
    if not STATIC:
        c.add(f'<rect x="-90" y="{y0}" width="90" height="{gh}" fill="url(#sweep)" clip-path="url(#grid)">'
              f'<animate attributeName="x" values="{x0 - 90};{x0 + gw};{x0 + gw}" keyTimes="0;.4;1" '
              f'dur="14s" begin="2.5s" repeatCount="indefinite"/></rect>')

    # Legend: Less ▢▢▢▢▢ More
    ly = y0 + gh + 30
    c.text(W - 48, ly + 10, "More", "b", 12, MUTED, "end", anim="fade", delay=400)
    lx = W - 48 - FACES["b"].width("More", 12) - 8 - 5 * pitch + gap
    c.add("".join(f'<rect class="l{k}" x="{n(lx + k * pitch)}" y="{ly}" width="{cell}" height="{cell}" rx="3"/>'
                  for k in range(5)))
    c.text(lx - 8, ly + 10, "Less", "b", 12, MUTED, "end", anim="fade", delay=400)
    return c


def repo_card(idx: int, repo: dict) -> Card:
    W, H = 900, 150
    c = Card(f"repo-{idx}", W, H, repo["name"], repo.get("description") or repo["name"])
    name, nsize = fit_line(repo["name"], "d", 26, 720, 16)
    c.text(48, 52, name, "d", nsize, TEXT, anim="rise", delay=idx * 60)
    nw = FACES["d"].width(name, nsize)
    c.add(f'<rect x="48" y="60" width="{n(nw)}" height="2" rx="1" fill="{BRAND}">'
          f'{reveal("width", 0, nw, 0.4 + idx * 0.1, 0.7)}</rect>')
    c.add(arrow_icon(W - 48 - 16, 28))

    desc = repo.get("description") or "No description yet."
    for i, line in enumerate(wrap(desc, "b", 15, 760, 2)):
        c.text(48, 88 + i * 21, line, "b", 15, MUTED, anim="fade", delay=250 + idx * 60)

    x, my = 48, 130
    lang = repo.get("language")
    if lang:
        color = repo.get("language_color") or MUTED
        color = color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else MUTED
        c.add(f'<circle cx="{x + 5}" cy="{my - 5}" r="5" fill="{color}"/>')
        shown = c.text(x + 18, my, lang, "b", 14, TEXT, anim="fade", delay=350)
        x += 18 + FACES["b"].width(shown, 14) + 28
    for icon, value in ((star_icon, repo.get("stars", 0)), (fork_icon, repo.get("forks", 0))):
        c.add(icon(x, my - 13))
        shown = c.text(x + 22, my, short(value), "b", 14, TEXT, anim="fade", delay=350)
        x += 22 + FACES["b"].width(shown, 14) + 28
    return c


# ───────────────────────────── README + main ─────────────────────────────
def update_readme(path: Path, repos: list[dict]) -> bool:
    if not path.exists():
        return False
    block = "\n".join(
        f'<p align="center"><a href="{esc(r["url"])}"><img src="cards/repo-{i}.svg" width="100%" '
        f'alt="{esc(r["name"])}"></a></p>'
        for i, r in enumerate(repos, 1)
    )
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(REPO_START) + r".*?" + re.escape(REPO_END), re.S)
    if not pattern.search(text):
        print(f"note: {path.name} has no {REPO_START} … {REPO_END} markers; repo cards not linked", file=sys.stderr)
        return False
    new = pattern.sub(lambda _: f"{REPO_START}\n{block}\n{REPO_END}", text)
    if new != text:
        path.write_text(new, encoding="utf-8")
    return True


def main() -> None:
    global STATIC
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "contributions.json")
    ap.add_argument("--out", type=Path, default=ROOT / "cards")
    ap.add_argument("--readme", type=Path, default=ROOT / "README.md")
    ap.add_argument("--fonts", type=Path, default=ROOT / "fonts")
    ap.add_argument("--static", action="store_true", help="render the final frame without motion")
    args = ap.parse_args()
    STATIC = STATIC or args.static

    login = os.environ.get("GITHUB_REPOSITORY_OWNER", LOGIN)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    try:
        data = load_data(args.data, login, token)
    except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        sys.exit(str(exc))
    days = load_days(data)
    weeks = build_weeks(days)

    ensure_fonts(args.fonts)
    FACES.update({k: Face(k, args.fonts) for k in FONTS})

    cards = [hero_card(data, weeks)]
    cards += [
        tile_card(0, "repositories", data.get("public_repos", 0), "Public repositories"),
        tile_card(1, "stars", data.get("stars", 0), "Stars earned"),
        tile_card(2, "followers", data.get("followers", 0), "Followers"),
        activity_card(data, days),
    ]
    repos = data.get("featured", [])[:MAX_REPOS]
    cards += [repo_card(i, r) for i, r in enumerate(repos, 1)]
    cards.append(graph_card(data, days, weeks))

    args.out.mkdir(parents=True, exist_ok=True)
    keep = {f"{c.name}.svg" for c in cards}
    for stale in args.out.glob("repo-*.svg"):
        if stale.name not in keep:
            stale.unlink()
    for c in cards:
        out = args.out / f"{c.name}.svg"
        svg = c.svg()
        if not out.exists() or out.read_text(encoding="utf-8") != svg:
            out.write_text(svg, encoding="utf-8")
        print(f"{out.relative_to(args.out.parent) if out.is_relative_to(args.out.parent) else out}  {len(svg) / 1024:.1f} KB")

    aliases = {
        "build": "tile-repositories",
        "brand": "tile-stars",
        "learn": "tile-followers",
        "monthly_activity": "activity",
        "contribution_graph": "graph",
    }
    for alias, source in aliases.items():
        source_path = args.out / f"{source}.svg"
        alias_path = args.out / f"{alias}.svg"
        svg = source_path.read_text(encoding="utf-8")
        if not alias_path.exists() or alias_path.read_text(encoding="utf-8") != svg:
            alias_path.write_text(svg, encoding="utf-8")

    update_readme(args.readme, repos)


if __name__ == "__main__":
    main()
