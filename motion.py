"""
Animated infographic scenes for news Shorts.

Each scene is a small HTML page (big headline, one simple visual, captions, brand bar) with
CSS animations. Headless Chromium (Playwright) renders it frame by frame at exact times, and
the frames are piped straight into ffmpeg. Scene types:

  hook      big headline + icon + optional rubber stamp (CANCELLED, BANNED ...)
  headline  kicker + big headline over a blurred photo
  stat      big number (counts up) + optional donut chart
  map       world / regional map with highlighted countries or states and pulsing pins
  quote     big quote card
  icon      title + icon + optional verdict word (e.g. PROBABLY NOT)
  versus    two numbers side by side (before / after)
  outro     channel logo + sign-off
"""
import base64
import html as H
import json
import math
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
W, HGT = 1080, 1920

THEME = {
    "bg1": "#16223a", "bg2": "#0a111d", "ink": "#f4f6fa", "muted": "#a3afc2", "shape": "#71819c",
    "shape2": "#3b4b68", "land": "#2f3f5c", "edge": "#0c1422", "yellow": "#ffc629", "red": "#ea3a30",
    "green": "#33c17a", "paper": "#e9ecf1",
}

ICONS = ["people", "document", "list", "money", "house", "fuel", "chart_up", "chart_down", "calendar",
         "gavel", "ballot", "hospital", "school", "plane", "alert", "fire", "storm", "building", "police", "train"]


# ----------------------------------------------------------------------------- helpers

def esc(s):
    return H.escape(str(s or ""), quote=True)


def b64(path, mime):
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()


def hl(text, highlight, cls="y"):
    """Upper-case text with the highlight word/phrase wrapped in a coloured span."""
    text = str(text or "").upper()
    h = str(highlight or "").upper().strip()
    out = esc(text)
    if h and h in text:
        i = text.index(h)
        out = esc(text[:i]) + f'<span class="{cls}">' + esc(h) + "</span>" + esc(text[i + len(h):])
    return out


_FONT_CSS = None


def font_css():
    global _FONT_CSS
    if _FONT_CSS is None:
        parts = []
        for weight, name in ((500, "Medium"), (600, "SemiBold"), (700, "Bold"), (800, "ExtraBold")):
            p = ASSETS / "fonts" / f"BarlowCondensed-{name}.ttf"
            if p.exists():
                parts.append("@font-face{font-family:'Barlow Condensed';font-weight:%d;src:url(%s) format('truetype')}"
                             % (weight, b64(p, "font/ttf")))
            m = ASSETS / "fonts" / f"Mukta-{name}.ttf"  # Hindi (Devanagari) letters
            if m.exists():
                parts.append("@font-face{font-family:'Mukta';font-weight:%d;src:url(%s) format('truetype')}"
                             % (weight, b64(m, "font/ttf")))
        _FONT_CSS = "\n".join(parts)
    return _FONT_CSS


# ----------------------------------------------------------------------------- map

_GEO = {}


def _geo(name):
    if name not in _GEO:
        p = ASSETS / "maps" / name
        _GEO[name] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"features": []}
    return _GEO[name]


def _merc(lon, lat):
    lat = max(-80.0, min(84.0, lat))
    return lon, math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def _rings(geom):
    if not geom:
        return []
    if geom["type"] == "Polygon":
        return geom["coordinates"]
    if geom["type"] == "MultiPolygon":
        return [r for poly in geom["coordinates"] for r in poly]
    return []


def _bbox(rings):
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def map_svg(sc, box_w=1000, box_h=760):
    countries = [c for c in (sc.get("countries") or []) if isinstance(c, dict) and c.get("iso3")]
    states = [s for s in (sc.get("states") or []) if isinstance(s, dict) and s.get("name")]
    pins = []
    for p in sc.get("pins") or []:
        try:
            lat, lon = float(p["lat"]), float(p["lon"])
            if -85 <= lat <= 85 and -180 <= lon <= 180:
                pins.append({"label": str(p.get("label", "")), "lat": lat, "lon": lon})
        except Exception:
            pass
    cf = _geo("countries.geojson")["features"]
    sf = _geo("states.geojson")["features"]
    iso = {c["iso3"].upper(): c for c in countries}
    sname = {s["name"].lower(): s for s in states}

    # decide the view (lon/lat box)
    focus = []
    for f in cf:
        if f["properties"].get("ADM0_A3") in iso:
            r = _rings(f["geometry"])
            # ignore far-off islands for the bbox: use the biggest polygon
            if f["geometry"]["type"] == "MultiPolygon":
                polys = sorted(f["geometry"]["coordinates"], key=lambda p: -len(p[0]))
                r = polys[0]
            focus.append(_bbox(r))
    for f in sf:
        if f["properties"].get("name", "").lower() in sname:
            focus.append(_bbox(_rings(f["geometry"])))
    for p in pins:
        focus.append((p["lon"], p["lat"], p["lon"], p["lat"]))
    focus = [b for b in focus if b]
    if sc.get("view") == "world" or not focus:
        lon0, lat0, lon1, lat1 = -170, -56, 190, 78
    else:
        lon0 = min(b[0] for b in focus); lat0 = min(b[1] for b in focus)
        lon1 = max(b[2] for b in focus); lat1 = max(b[3] for b in focus)
        span = max(lon1 - lon0, (lat1 - lat0) * 1.2, 14)
        cx, cy = (lon0 + lon1) / 2, (lat0 + lat1) / 2
        span *= 1.6
        lon0, lon1 = cx - span / 2, cx + span / 2
        lat0, lat1 = cy - span * 0.42, cy + span * 0.42
    (x0, y0), (x1, y1) = _merc(lon0, lat0), _merc(lon1, lat1)
    s = min(box_w / (x1 - x0), box_h / (y1 - y0))
    ox = (box_w - (x1 - x0) * s) / 2
    oy = (box_h - (y1 - y0) * s) / 2

    def P(lon, lat):
        mx, my = _merc(lon, lat)
        return ox + (mx - x0) * s, oy + (y1 - my) * s

    def path(rings):
        d = []
        for r in rings:
            pts = [P(*pt[:2]) for pt in r]
            if all(px < -200 or px > box_w + 200 for px, _ in pts) or all(py < -200 or py > box_h + 200 for _, py in pts):
                continue
            d.append("M" + "L".join(f"{px:.1f} {py:.1f}" for px, py in pts) + "Z")
        return "".join(d)

    base, hi, labels = [], [], []
    for f in cf:
        a3 = f["properties"].get("ADM0_A3")
        d = path(_rings(f["geometry"]))
        if not d:
            continue
        if a3 in iso:
            hi.append(f'<path class="hi" d="{d}"/>')
            lab = iso[a3].get("label")
            if lab:
                g = f["geometry"]
                r = sorted(g["coordinates"], key=lambda p: -len(p[0]))[0] if g["type"] == "MultiPolygon" else g["coordinates"]
                bx = _bbox(r)
                lx, ly = P((bx[0] + bx[2]) / 2, bx[1])
                labels.append((lx, min(box_h - 20, ly + 50), lab))
        else:
            base.append(f'<path d="{d}"/>')
    for f in sf:
        n = f["properties"].get("name", "").lower()
        if n in sname:
            d = path(_rings(f["geometry"]))
            if d:
                hi.append(f'<path class="hi" d="{d}"/>')
                lab = sname[n].get("label")
                if lab:
                    bx = _bbox(_rings(f["geometry"]))
                    lx, ly = P((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2)
                    labels.append((lx, ly + 10, lab))
    pin_svg = []
    for i, p in enumerate(pins[:3]):
        px, py = P(p["lon"], p["lat"])
        dl = 0.7 + i * 0.35
        pin_svg.append(
            f'<g transform="translate({px:.1f},{py:.1f})">'
            f'<circle class="ring" r="34" style="animation-delay:{dl:.2f}s"/>'
            f'<circle class="dot pop" r="13" style="animation-delay:{dl:.2f}s"/>'
            f'<circle class="dotin pop" r="6" style="animation-delay:{dl:.2f}s"/></g>')
        if p["label"]:
            labels.append((px, py - 52, p["label"]))
    lab_svg = "".join(
        f'<text class="mlab pop" x="{x:.1f}" y="{y:.1f}" style="animation-delay:{0.9 + i * 0.3:.2f}s">{esc(t.upper())}</text>'
        for i, (x, y, t) in enumerate(labels[:4]))
    return (f'<svg class="map" viewBox="0 0 {box_w} {box_h}" width="{box_w}" height="{box_h}">'
            f'<g class="land">{"".join(base)}</g><g class="his">{"".join(hi)}</g>{"".join(pin_svg)}{lab_svg}</svg>')


# ----------------------------------------------------------------------------- icons (simple flat SVG)

def icon_svg(name, sc):
    T = THEME
    sh, sh2, pa, y, r = T["shape"], T["shape2"], T["paper"], T["yellow"], T["red"]
    v = 'viewBox="0 0 800 560" width="800" height="560"'
    if name == "people":
        g = []
        for row in range(3):
            yy = 120 + row * 150
            g.append(f'<rect x="40" y="{yy + 98}" width="720" height="12" rx="6" fill="{sh2}"/>')
            for col in range(7):
                xx = 85 + col * 105 + (26 if row == 1 else 0)
                if xx > 760:
                    continue
                d = 0.15 + col * 0.06 + row * 0.12
                g.append(f'<g class="pop" style="animation-delay:{d:.2f}s;transform-origin:{xx}px {yy + 60}px">'
                         f'<circle cx="{xx}" cy="{yy}" r="24" fill="{sh}"/>'
                         f'<rect x="{xx - 34}" y="{yy + 30}" width="68" height="66" rx="26" fill="{sh}"/></g>')
        return f'<svg {v}>{"".join(g)}</svg>'
    if name == "document":
        title = esc((sc.get("doc_title") or "STATEMENT").upper())
        lines = "".join(f'<rect x="230" y="{200 + i * 46}" width="{340 - (i % 3) * 50}" height="14" rx="7" fill="#b9c1ce"/>' for i in range(6))
        return (f'<svg {v}><g class="rise"><rect x="180" y="60" width="440" height="460" rx="18" fill="{pa}"/>'
                f'<text x="400" y="140" text-anchor="middle" class="svgt" fill="#2a3446" font-size="44">{title}</text>'
                f'{lines}</g></svg>')
    if name == "list":
        strike = esc((sc.get("strike") or "").title())
        g = []
        for i in range(4):
            yy = 60 + i * 120
            d = 0.1 + i * 0.12
            g.append(f'<g class="rise" style="animation-delay:{d:.2f}s"><rect x="110" y="{yy}" width="580" height="90" rx="14" fill="{sh2}" stroke="#55678a" stroke-width="3"/>')
            if i == 1 and strike:
                g.append(f'<text x="150" y="{yy + 60}" class="svgt" fill="{pa}" font-size="46">{strike}</text>'
                         f'<rect class="strike" x="135" y="{yy + 40}" width="520" height="9" rx="4" fill="{r}"/></g>')
            else:
                g.append(f'<rect x="150" y="{yy + 38}" width="{360 - i * 40}" height="14" rx="7" fill="#7d8eac"/></g>')
        return f'<svg {v}>{"".join(g)}</svg>'
    if name == "money":
        return (f'<svg {v}><g class="pop">'
                f'<rect x="170" y="190" width="460" height="230" rx="22" fill="#2f7d58"/>'
                f'<rect x="150" y="160" width="460" height="230" rx="22" fill="#3c9c6d"/>'
                f'<circle cx="380" cy="275" r="78" fill="#2f7d58"/>'
                f'<text x="380" y="308" text-anchor="middle" class="svgt" fill="{pa}" font-size="110">$</text></g></svg>')
    if name == "house":
        return (f'<svg {v}><g class="pop"><polygon points="400,70 640,270 160,270" fill="{y}"/>'
                f'<rect x="210" y="260" width="380" height="250" fill="{sh}"/>'
                f'<rect x="355" y="370" width="90" height="140" fill="{sh2}"/>'
                f'<rect x="250" y="310" width="70" height="60" fill="{pa}"/><rect x="480" y="310" width="70" height="60" fill="{pa}"/></g></svg>')
    if name == "fuel":
        return (f'<svg {v}><g class="pop"><rect x="270" y="90" width="230" height="420" rx="24" fill="{sh}"/>'
                f'<rect x="305" y="130" width="160" height="110" rx="12" fill="{pa}"/>'
                f'<path d="M500 200 h60 a30 30 0 0 1 30 30 v170 a25 25 0 0 0 50 0 v-200 l-50 -60" fill="none" stroke="{sh}" stroke-width="26" stroke-linecap="round"/>'
                f'<rect x="240" y="480" width="290" height="40" rx="10" fill="{sh2}"/></g></svg>')
    if name in ("chart_up", "chart_down"):
        up = name == "chart_up"
        hs = [120, 170, 230, 300, 380] if up else [380, 300, 230, 170, 120]
        col = y if up else r
        bars = "".join(f'<rect class="grow" x="{150 + i * 110}" y="{500 - h}" width="80" height="{h}" rx="8" fill="{sh}" style="animation-delay:{0.1 + i * 0.1:.2f}s;transform-origin:0 500px"/>' for i, h in enumerate(hs))
        pts = " ".join(f"{190 + i * 110},{470 - h}" for i, h in enumerate(hs))
        return (f'<svg {v}>{bars}<polyline class="draw" points="{pts}" fill="none" stroke="{col}" stroke-width="16" stroke-linecap="round" stroke-linejoin="round"/>'
                f'<rect x="120" y="500" width="580" height="8" fill="{sh2}"/></svg>')
    if name == "calendar":
        dt = esc((sc.get("date_text") or "").upper())
        return (f'<svg {v}><g class="pop"><rect x="230" y="90" width="340" height="400" rx="26" fill="{pa}"/>'
                f'<rect x="230" y="90" width="340" height="110" rx="26" fill="{r}"/><rect x="230" y="160" width="340" height="40" fill="{r}"/>'
                f'<rect x="300" y="60" width="24" height="70" rx="12" fill="{sh2}"/><rect x="476" y="60" width="24" height="70" rx="12" fill="{sh2}"/>'
                f'<text x="400" y="370" text-anchor="middle" class="svgt" fill="#1d2738" font-size="{110 if len(dt) <= 6 else 70}">{dt}</text></g></svg>')
    if name == "gavel":
        return (f'<svg {v}><g class="pop" style="transform-origin:400px 300px">'
                f'<g transform="rotate(-35 400 260)"><rect x="300" y="150" width="200" height="90" rx="18" fill="#a4683a"/>'
                f'<rect x="280" y="140" width="40" height="110" rx="10" fill="#8a5530"/><rect x="480" y="140" width="40" height="110" rx="10" fill="#8a5530"/>'
                f'<rect x="385" y="235" width="30" height="230" rx="12" fill="#c08452"/></g>'
                f'<rect x="200" y="470" width="400" height="44" rx="12" fill="{sh}"/></g></svg>')
    if name == "ballot":
        return (f'<svg {v}><rect class="drop" x="330" y="40" width="140" height="190" rx="10" fill="{pa}"/>'
                f'<g class="pop"><rect x="200" y="230" width="400" height="280" rx="22" fill="{sh}"/>'
                f'<rect x="300" y="220" width="200" height="22" rx="10" fill="{sh2}"/>'
                f'<polyline points="345,370 385,410 460,320" fill="none" stroke="{y}" stroke-width="22" stroke-linecap="round" stroke-linejoin="round"/></g></svg>')
    if name == "hospital":
        return (f'<svg {v}><g class="pop"><rect x="200" y="140" width="400" height="370" rx="14" fill="{sh}"/>'
                f'<rect x="355" y="190" width="90" height="230" rx="10" fill="{r}"/><rect x="285" y="260" width="230" height="90" rx="10" fill="{r}"/>'
                f'<rect x="360" y="440" width="80" height="70" fill="{sh2}"/></g></svg>')
    if name == "school":
        return (f'<svg {v}><g class="pop"><polygon points="400,110 700,240 400,370 100,240" fill="{sh}"/>'
                f'<path d="M230 300 v110 q170 90 340 0 v-110 l-170 75z" fill="{sh2}"/>'
                f'<rect x="640" y="240" width="14" height="170" fill="{y}"/><circle cx="647" cy="420" r="20" fill="{y}"/></g></svg>')
    if name == "plane":
        return (f'<svg {v}><g class="fly"><path d="M120 330 L680 250 Q730 245 720 280 Q710 300 660 305 L120 360 Z" fill="{pa}"/>'
                f'<path d="M400 285 L300 120 L360 115 L500 275 Z" fill="{sh}"/><path d="M400 330 L320 470 L380 475 L500 320 Z" fill="{sh}"/>'
                f'<path d="M150 335 L100 240 L140 238 L210 330Z" fill="{sh}"/></g></svg>')
    if name == "alert":
        return (f'<svg {v}><g class="pop"><polygon points="400,70 690,500 110,500" fill="{y}" stroke="{y}" stroke-width="30" stroke-linejoin="round"/>'
                f'<rect x="375" y="200" width="50" height="180" rx="22" fill="#1b2232"/><circle cx="400" cy="435" r="30" fill="#1b2232"/></g></svg>')
    if name == "fire":
        return (f'<svg {v}><g class="flick"><path d="M400 60 C470 170 600 230 600 380 C600 470 510 530 400 530 C290 530 200 470 200 380 C200 280 280 240 300 150 C340 220 360 240 380 250 C390 190 380 130 400 60Z" fill="{r}"/>'
                f'<path d="M400 260 C440 320 500 350 500 420 C500 480 455 515 400 515 C345 515 300 480 300 420 C300 370 340 350 360 300 C375 330 390 335 400 260Z" fill="{y}"/></g></svg>')
    if name == "storm":
        drops = "".join(f'<rect class="rain" x="{250 + i * 75}" y="330" width="14" height="70" rx="7" fill="#7fb2ff" style="animation-delay:{i * 0.13:.2f}s"/>' for i, _ in enumerate(range(5)))
        return (f'<svg {v}><g class="pop"><circle cx="300" cy="230" r="100" fill="{sh}"/><circle cx="440" cy="190" r="130" fill="{sh}"/>'
                f'<circle cx="560" cy="250" r="90" fill="{sh}"/><rect x="220" y="230" width="420" height="110" rx="55" fill="{sh}"/></g>'
                f'{drops}<polygon class="pop" style="animation-delay:.5s" points="430,330 370,450 420,450 390,540 480,410 425,410 460,330" fill="{y}"/></svg>')
    if name == "police":
        return (f'<svg {v}><g class="pop"><path d="M400 60 L620 140 Q620 380 400 510 Q180 380 180 140 Z" fill="#2c5aa0"/>'
                f'<path d="M400 110 L570 172 Q568 360 400 455 Q232 360 230 172 Z" fill="{sh2}"/>'
                f'<polygon points="400,180 428,250 503,252 444,298 465,370 400,328 335,370 356,298 297,252 372,250" fill="{y}"/></g></svg>')
    if name == "train":
        return (f'<svg {v}><g class="fly"><rect x="230" y="90" width="340" height="360" rx="60" fill="{sh}"/>'
                f'<rect x="270" y="140" width="260" height="130" rx="18" fill="{pa}"/>'
                f'<circle cx="310" cy="360" r="30" fill="{y}"/><circle cx="490" cy="360" r="30" fill="{y}"/>'
                f'<rect x="280" y="450" width="40" height="70" fill="{sh2}"/><rect x="480" y="450" width="40" height="70" fill="{sh2}"/>'
                f'<rect x="120" y="510" width="560" height="14" rx="7" fill="{sh2}"/></g></svg>')
    # building (parliament)
    cols = "".join(f'<rect x="{215 + i * 80}" y="230" width="40" height="230" fill="{sh}"/>' for i in range(5))
    return (f'<svg {v}><g class="pop"><polygon points="400,70 680,210 120,210" fill="{sh}"/>'
            f'<rect x="150" y="205" width="500" height="28" fill="{sh2}"/>{cols}'
            f'<rect x="130" y="460" width="540" height="50" fill="{sh2}"/></g></svg>')


# ----------------------------------------------------------------------------- scene HTML

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1920px;overflow:hidden;background:%(bg2)s}
body{font-family:'Barlow Condensed','Mukta','DejaVu Sans Condensed',sans-serif;color:%(ink)s}
.frame{position:absolute;inset:0;overflow:hidden;background:radial-gradient(120%% 80%% at 50%% 30%%,%(bg1)s,%(bg2)s);animation:fadein .25s both}
.photo{position:absolute;inset:-60px;width:1200px;height:2040px;object-fit:cover;filter:blur(7px) brightness(.38) saturate(.85);animation:bgzoom 14s linear both}
.shade{position:absolute;inset:0;background:linear-gradient(180deg,rgba(8,13,22,.55) 0%%,rgba(8,13,22,.15) 40%%,rgba(8,13,22,.75) 100%%)}
.brand{position:absolute;left:46px;top:58px;font-weight:700;font-size:30px;letter-spacing:1.5px;color:#e8edf4;opacity:.92}
.scene{position:absolute;left:0;right:0;top:0;bottom:0}
.zone-top{position:absolute;left:70px;right:70px;top:150px;height:400px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
.zone-mid{position:absolute;left:0;right:0;top:560px;height:760px;display:flex;align-items:center;justify-content:center}
.zone-center{position:absolute;left:70px;right:70px;top:380px;height:880px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
.kicker{font-weight:700;font-size:50px;letter-spacing:2px;color:%(muted)s;text-transform:uppercase;line-height:1.05}
.h1{font-weight:800;font-size:124px;line-height:.98;text-transform:uppercase;width:100%%;max-height:390px;overflow:hidden}
.h1.big{font-size:150px;max-height:620px}
.h1.yellow{color:%(yellow)s}
.sub{font-weight:600;font-size:42px;color:%(ink)s;margin-top:18px;line-height:1.15}
.y{color:%(yellow)s}.r{color:%(red)s}.g{color:%(green)s}
.cap{position:absolute;left:90px;right:90px;top:1360px;height:170px;display:flex;align-items:center;justify-content:center;text-align:center;font-weight:700;font-size:58px;line-height:1.12;color:#fff;text-shadow:0 4px 18px rgba(0,0,0,.85)}
.stamp{position:absolute;left:50%%;top:50%%;transform:translate(-50%%,-50%%) rotate(-9deg);font-weight:800;font-size:118px;letter-spacing:3px;color:%(red)s;border:12px solid %(red)s;border-radius:18px;padding:4px 34px;background:rgba(10,17,29,.35);animation:stamp .42s cubic-bezier(.3,1.6,.5,1) both;animation-delay:.9s;white-space:nowrap}
.viewfinder{position:relative;width:860px;height:600px;display:flex;align-items:center;justify-content:center}
.viewfinder i{position:absolute;width:70px;height:70px;border-color:#e6ebf2;border-style:solid}
.viewfinder i:nth-child(1){left:0;top:0;border-width:7px 0 0 7px}.viewfinder i:nth-child(2){right:0;top:0;border-width:7px 7px 0 0}
.viewfinder i:nth-child(3){left:0;bottom:0;border-width:0 0 7px 7px}.viewfinder i:nth-child(4){right:0;bottom:0;border-width:0 7px 7px 0}
.rec{position:absolute;right:34px;top:30px;width:26px;height:26px;border-radius:50%%;background:%(red)s;animation:blink 1s steps(2) infinite}
.big-num{font-weight:800;font-size:200px;line-height:1;color:%(yellow)s}
.donut{position:relative;width:440px;height:440px;margin-top:30px}
.donut svg{position:absolute;inset:0;transform:rotate(-90deg)}
.donut .arc{animation:donut 1.1s cubic-bezier(.3,.8,.3,1) both;animation-delay:.45s}
.donut .mid{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:92px}
.label{font-weight:600;font-size:44px;line-height:1.15;margin-top:34px;color:%(ink)s}
.qmark{font-weight:800;font-size:200px;line-height:.6;color:%(yellow)s;height:110px}
.quote{font-weight:800;font-size:96px;line-height:1.02;text-transform:uppercase;max-height:760px;overflow:hidden}
.by{font-weight:600;font-size:36px;color:%(muted)s;margin-top:34px}
.verdict{font-weight:800;font-size:132px;line-height:1;margin-top:10px;text-transform:uppercase}
.vs{display:flex;gap:40px;align-items:center;justify-content:center;width:100%%}
.card{flex:1;background:rgba(255,255,255,.06);border:3px solid rgba(255,255,255,.12);border-radius:28px;padding:40px 20px;text-align:center}
.card .lab{font-weight:700;font-size:42px;color:%(muted)s;text-transform:uppercase}
.card .val{font-weight:800;font-size:120px;line-height:1.05;margin-top:10px}
.arrow{font-weight:800;font-size:110px;color:%(yellow)s}
.map{display:block}
.map .land path{fill:%(land)s;stroke:%(edge)s;stroke-width:1.2}
.map .his path{fill:%(yellow)s;stroke:%(edge)s;stroke-width:1.2;animation:fillin .6s ease-out both;animation-delay:.35s}
.map .ring{fill:none;stroke:%(yellow)s;stroke-width:5;animation:ring 1.4s ease-out infinite;transform-box:fill-box;transform-origin:center}
.map .dot{fill:%(yellow)s}.map .dotin{fill:%(bg2)s}
.mlab{font-family:'Barlow Condensed','Mukta',sans-serif;font-weight:800;font-size:46px;fill:%(yellow)s;text-anchor:middle;paint-order:stroke;stroke:%(bg2)s;stroke-width:8px}
.mapwrap{animation:mapzoom 9s ease-out both}
.logo{width:340px;height:340px;border-radius:50%%;object-fit:cover;box-shadow:0 0 60px rgba(255,198,41,.18)}
.fit{flex-shrink:0}
.svgt{font-family:'Barlow Condensed','Mukta',sans-serif;font-weight:800}
.pop{animation:pop .5s cubic-bezier(.2,1.5,.4,1) both;transform-box:fill-box;transform-origin:center}
.rise{animation:rise .55s cubic-bezier(.2,.9,.3,1) both}
.fade{animation:fade .5s ease-out both}
.grow{animation:grow .55s cubic-bezier(.2,.9,.3,1) both;transform-box:view-box}
.draw{stroke-dasharray:1400;animation:draw 1.1s ease-out both;animation-delay:.5s}
.strike{animation:strike .45s ease-out both;animation-delay:.8s;transform-box:fill-box;transform-origin:left center}
.drop{animation:drop .7s cubic-bezier(.5,0,.7,1) both;animation-delay:.3s}
.fly{animation:fly 6s ease-out both}
.flick{animation:flick .6s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:bottom center}
.rain{animation:rain .8s linear infinite}
@keyframes fadein{from{opacity:0}to{opacity:1}}
@keyframes bgzoom{from{transform:scale(1.02)}to{transform:scale(1.14)}}
@keyframes pop{0%%{transform:scale(.3);opacity:0}100%%{transform:scale(1);opacity:1}}
@keyframes rise{from{transform:translateY(60px);opacity:0}to{transform:none;opacity:1}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes grow{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes draw{from{stroke-dashoffset:1400}to{stroke-dashoffset:0}}
@keyframes strike{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes stamp{0%%{transform:translate(-50%%,-50%%) rotate(-16deg) scale(2.6);opacity:0}100%%{transform:translate(-50%%,-50%%) rotate(-9deg) scale(1);opacity:1}}
@keyframes donut{from{stroke-dashoffset:var(--full)}}
@keyframes fillin{from{fill:%(land)s}to{fill:%(yellow)s}}
@keyframes ring{0%%{transform:scale(.4);opacity:1}100%%{transform:scale(1.9);opacity:0}}
@keyframes mapzoom{from{transform:scale(1.12)}to{transform:scale(1)}}
@keyframes blink{0%%{opacity:1}100%%{opacity:.15}}
@keyframes drop{0%%{transform:translateY(-120px);opacity:0}70%%{opacity:1}100%%{transform:translateY(120px);opacity:0}}
@keyframes fly{from{transform:translate(-160px,60px)}to{transform:translate(60px,-30px)}}
@keyframes flick{from{transform:scale(1,.96)}to{transform:scale(1,1.04)}}
@keyframes rain{from{transform:translateY(-20px);opacity:0}30%%{opacity:1}to{transform:translateY(130px);opacity:0}}
""" % THEME

JS = r"""
window.__seek = function(t){
  document.getAnimations().forEach(function(a){ a.pause(); a.currentTime = t*1000; });
  document.querySelectorAll('[data-count]').forEach(function(el){
    var d = parseFloat(el.dataset.delay||'0.3'), dur = 1.0, k = Math.max(0, Math.min(1,(t-d)/dur));
    k = 1 - Math.pow(1-k, 3);
    var v = parseFloat(el.dataset.count)*k, dec = parseInt(el.dataset.dec||'0');
    var s = v.toFixed(dec); if (el.dataset.comma==='1') s = Number(s).toLocaleString('en-AU',{minimumFractionDigits:dec,maximumFractionDigits:dec});
    el.textContent = (el.dataset.pre||'') + s + (el.dataset.suf||'');
  });
  var caps = window.__caps || [], cur = '';
  for (var i=0;i<caps.length;i++){ if (t >= caps[i][0]) cur = caps[i][1]; }
  var c = document.getElementById('cap'); if (c && c.textContent !== cur) c.textContent = cur;
};
window.__fit = function(){
  document.querySelectorAll('.fit').forEach(function(el){
    var inner = document.createElement('div'); inner.innerHTML = el.innerHTML; el.innerHTML = ''; el.appendChild(inner);
    var cs = getComputedStyle(el), maxH = parseFloat(el.dataset.maxh || cs.maxHeight) || 9999;
    var fs = parseFloat(cs.fontSize), lim = parseFloat(el.dataset.min || '40');
    while ((inner.offsetHeight > maxH || inner.scrollWidth > el.clientWidth + 2) && fs > lim){ fs -= 4; el.style.fontSize = fs + 'px'; }
  });
};
"""


def count_attrs(value, delay=0.35):
    """'$550' -> data-count attributes for a count-up; None if not a plain number."""
    m = re.fullmatch(r"\s*([^\d\-]*?)(\d[\d,]*(?:\.\d+)?)(.*)", str(value or ""))
    if not m:
        return None
    pre, num, suf = m.groups()
    if re.search(r"\d", suf) or len(suf) > 10:
        return None
    dec = len(num.split(".")[1]) if "." in num else 0
    return (f'data-count="{num.replace(",", "")}" data-dec="{dec}" data-comma="{1 if "," in num else 0}" '
            f'data-pre="{esc(pre)}" data-suf="{esc(suf)}" data-delay="{delay}"')


def donut_pct(value, given=None):
    v = str(value or "").upper()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|PER ?CENT)", v)
    if m:
        return max(0.0, min(100.0, float(m.group(1))))
    m = re.search(r"(\d+)\s+(?:IN|OUT OF)\s+(\d+)", v)
    if m and float(m.group(2)) > 0:
        return max(0.0, min(100.0, 100 * float(m.group(1)) / float(m.group(2))))
    return None


def scene_body(sc, assets):
    t = sc.get("type")
    bg = ""
    if sc.get("_photo"):
        bg = f'<img class="photo" src="{b64(sc["_photo"], "image/jpeg")}"><div class="shade"></div>'
    if t == "hook":
        icon = icon_svg(sc.get("icon") if sc.get("icon") in ICONS else "people", sc)
        stamp = f'<div class="stamp">{esc(str(sc.get("stamp")).upper())}</div>' if sc.get("stamp") else ""
        return bg + (
            f'<div class="zone-top"><div class="h1 yellow fit pop" data-min="70">{hl(sc.get("headline"), sc.get("highlight"), "w")}</div>'
            f'<div class="sub rise" style="animation-delay:.25s">{esc(sc.get("sub"))}</div></div>'
            f'<div class="zone-mid"><div class="viewfinder fade"><i></i><i></i><i></i><i></i><div class="rec"></div>{icon}{stamp}</div></div>')
    if t == "headline":
        k = f'<div class="kicker rise">{esc(sc.get("kicker"))}</div>' if sc.get("kicker") else ""
        return bg + (f'<div class="zone-center">{k}<div class="h1 big fit pop" style="animation-delay:.12s;margin-top:20px" data-min="70">'
                     f'{hl(sc.get("headline"), sc.get("highlight"))}</div></div>')
    if t == "stat":
        val = str(sc.get("value") or "").upper()
        ca = count_attrs(val)
        pct = donut_pct(val)
        k = f'<div class="kicker rise">{esc(sc.get("kicker"))}</div>' if sc.get("kicker") else ""
        num = f'<div class="big-num fit pop" style="width:100%;max-height:230px" {ca or ""}>{esc(val)}</div>'
        don = ""
        if pct is not None:
            R = 180
            full = 2 * math.pi * R
            off = full * (1 - pct / 100)
            don = (f'<div class="donut fade" style="animation-delay:.3s"><svg viewBox="0 0 440 440"><circle cx="220" cy="220" r="{R}" fill="none" stroke="{THEME["shape2"]}" stroke-width="70"/>'
                   f'<circle class="arc" cx="220" cy="220" r="{R}" fill="none" stroke="{THEME["yellow"]}" stroke-width="70" '
                   f'style="--full:{full:.1f};stroke-dasharray:{full:.1f};stroke-dashoffset:{off:.1f}"/></svg>'
                   f'<div class="mid">{esc(str(sc.get("donut_label") or "").upper())}</div></div>')
        return bg + (f'<div class="zone-center" style="top:200px;height:1120px">{k}{num}{don}'
                     f'<div class="label rise" style="animation-delay:.6s">{esc(sc.get("label"))}</div></div>')
    if t == "map":
        k = f'<div class="kicker rise">{esc(sc.get("kicker"))}</div>' if sc.get("kicker") else ""
        return bg + (f'<div class="zone-top" style="height:330px">{k}<div class="h1 fit pop" style="font-size:104px;max-height:250px" data-min="60">'
                     f'{hl(sc.get("title"), sc.get("highlight"))}</div></div>'
                     f'<div class="zone-mid" style="top:500px;height:820px"><div class="mapwrap">{map_svg(sc)}</div></div>')
    if t == "quote":
        return bg + (f'<div class="zone-center" style="top:200px;height:1120px"><div class="qmark pop">&ldquo;</div>'
                     f'<div class="quote fit rise" style="animation-delay:.15s;width:100%" data-min="56">{hl(sc.get("quote"), sc.get("highlight"))}</div>'
                     f'<div class="by fade" style="animation-delay:.6s">{esc(sc.get("by"))}</div></div>')
    if t == "versus":
        def card(lab, val, cls, d):
            ca = count_attrs(val, d + 0.2)
            return (f'<div class="card rise" style="animation-delay:{d}s"><div class="lab">{esc(lab)}</div>'
                    f'<div class="val fit {cls}" style="max-height:140px" {ca or ""}>{esc(str(val or "").upper())}</div></div>')
        return bg + (f'<div class="zone-top"><div class="h1 fit pop" style="font-size:110px" data-min="60">{hl(sc.get("title"), sc.get("highlight"))}</div></div>'
                     f'<div class="zone-mid" style="padding:0 60px"><div class="vs">{card(sc.get("left_label"), sc.get("left_value"), "", 0.2)}'
                     f'<div class="arrow pop" style="animation-delay:.55s">&rarr;</div>{card(sc.get("right_label"), sc.get("right_value"), "y", 0.7)}</div></div>')
    if t == "outro":
        logo = f'<img class="logo pop" src="{b64(assets["logo"], "image/jpeg")}">' if assets.get("logo") else ""
        return (f'<div class="zone-center" style="top:300px;height:1000px">{logo}'
                f'<div class="h1 rise" style="font-size:96px;margin-top:60px;animation-delay:.25s">{esc(sc.get("line1"))}</div>'
                f'<div class="h1 y rise" style="font-size:96px;animation-delay:.4s">{esc(sc.get("line2"))}</div></div>')
    # icon scene (default)
    icon = icon_svg(sc.get("icon") if sc.get("icon") in ICONS else "document", sc)
    k = f'<div class="kicker rise">{esc(sc.get("kicker"))}</div>' if sc.get("kicker") else ""
    col = {"red": "r", "green": "g"}.get(sc.get("verdict_color"), "y")
    verdict = f'<div class="verdict {col} pop" style="animation-delay:.7s">{esc(sc.get("verdict"))}</div>' if sc.get("verdict") else ""
    sub = f'<div class="sub fade" style="animation-delay:.9s">{esc(sc.get("sub"))}</div>' if sc.get("sub") else ""
    return bg + (f'<div class="zone-top" style="height:330px">{k}<div class="h1 fit pop" style="font-size:110px;max-height:250px" data-min="60">'
                 f'{hl(sc.get("title"), sc.get("highlight"))}</div></div>'
                 f'<div class="zone-mid" style="top:500px;height:820px;flex-direction:column">{icon}{verdict}{sub}</div>')


def scene_html(sc, brand_line, assets, caps):
    body = scene_body(sc, assets)
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{font_css()}{CSS}</style></head><body>'
            f'<div class="frame">{body}<div class="brand">{esc(brand_line)}</div><div class="cap" id="cap"></div></div>'
            f'<script>{JS}window.__caps={json.dumps(caps)};</script></body></html>')
    # .w (white) inside yellow hook headline


CSS += ".w{color:%s}" % THEME["ink"]


# ----------------------------------------------------------------------------- captions

def caption_chunks(words, text, dur, max_words=4, max_chars=26):
    """[(start_seconds, text)] - short caption groups timed to the voice."""
    if not words:
        toks = text.split()
        total = sum(len(t) + 1 for t in toks) or 1
        t, words = 0.0, []
        for tok in toks:
            d = dur * 0.95 * (len(tok) + 1) / total
            words.append((t, t + d, tok))
            t += d
    out, cur, start = [], [], None
    for (s, e, w) in words:
        if cur and (len(cur) >= max_words or len(" ".join(cur + [w])) > max_chars):
            out.append((round(start, 3), " ".join(cur)))
            cur, start = [], None
        if start is None:
            start = s
        cur.append(w)
        if re.search(r"[.!?]$", w):
            out.append((round(start, 3), " ".join(cur)))
            cur, start = [], None
    if cur:
        out.append((round(start, 3), " ".join(cur)))
    return out


# ----------------------------------------------------------------------------- render

def render(scenes, assets, brand_line, out_mp4, fps=30, log=print):
    """scenes: list of dicts with 'type', fields, '_dur' (seconds), '_words' [(s,e,w)], 'narration'.
    Writes a silent 1080x1920 video of exactly sum(_dur) seconds."""
    from playwright.sync_api import sync_playwright
    total = sum(s["_dur"] for s in scenes)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(fps), str(out_mp4)]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n_done = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--disable-gpu", "--font-render-hinting=none"])
            page = browser.new_page(viewport={"width": W, "height": HGT}, device_scale_factor=1)
            for i, sc in enumerate(scenes):
                caps = caption_chunks(sc.get("_words"), sc.get("narration", ""), sc["_dur"]) if sc.get("narration") else []
                page.set_content(scene_html(sc, brand_line, assets, caps), wait_until="load")
                page.evaluate("document.fonts.ready.then(() => window.__fit())")
                page.wait_for_timeout(50)
                frames = max(1, round(sc["_dur"] * fps))
                last, still = None, 0
                for f in range(frames):
                    t = f / fps
                    page.evaluate(f"window.__seek({t:.4f})")
                    jpg = page.screenshot(type="jpeg", quality=90)
                    ff.stdin.write(jpg)
                    n_done += 1
                log(f"scene {i + 1}/{len(scenes)} {sc.get('type')} {sc['_dur']:.1f}s rendered")
            browser.close()
    finally:
        ff.stdin.close()
        ff.wait()
    if ff.returncode != 0:
        raise RuntimeError("ffmpeg failed while encoding scenes")
    return out_mp4, total
