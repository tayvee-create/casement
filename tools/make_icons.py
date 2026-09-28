#!/usr/bin/env python3
"""Generate the Fluent-style SVG icons used by the app.

These are original drawings in the spirit of Windows 11 icons (flat, soft
gradients, rounded corners); run this script to regenerate them.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "casement", "icons", "hicolor", "scalable")
PLACES = os.path.join(ROOT, "places")
APPS = os.path.join(ROOT, "apps")

HEAD = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'

FOLDER_DEFS = """
<defs>
  <linearGradient id="front" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#FFD96A"/>
    <stop offset="1" stop-color="#FFC53D"/>
  </linearGradient>
  <linearGradient id="back" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#F2AE2B"/>
    <stop offset="1" stop-color="#E29A16"/>
  </linearGradient>
</defs>"""

FOLDER_BODY = """
<path d="M4 13a4 4 0 0 1 4-4h14.3a4 4 0 0 1 2.9 1.2L29.8 15H56a4 4 0 0 1 4 4v31a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="url(#back)"/>
<path d="M4 23a4 4 0 0 1 4-4h14.8a4 4 0 0 0 2.6-1l2.8-2.4a4 4 0 0 1 2.6-1H56a4 4 0 0 1 4 4v31a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="url(#front)"/>
<path d="M4 51v-1a4 4 0 0 0 4 4h48a4 4 0 0 0 4-4v1a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="#E8A21F" opacity=".35"/>
"""

# Glyphs drawn in a 64x64 box; when placed on a folder they are scaled into
# the front flap.
GLYPHS = {
    "desktop": """
<rect x="8" y="12" width="48" height="32" rx="4" fill="#1E88E5"/>
<rect x="11" y="15" width="42" height="26" rx="2" fill="#64B5F6"/>
<path d="M11 33l10-8 8 6 8-7 16 11v4a2 2 0 0 1-2 2H13a2 2 0 0 1-2-2z" fill="#1565C0" opacity=".55"/>
<rect x="26" y="44" width="12" height="6" fill="#90A4AE"/>
<rect x="18" y="50" width="28" height="4" rx="2" fill="#607D8B"/>""",
    "downloads": """
<path d="M26 8h12v24h10L32 50 16 32h10z" fill="#1B9E4B"/>
<path d="M26 8h12v24h10L32 50z" fill="#15803D" opacity=".35"/>
<rect x="12" y="52" width="40" height="5" rx="2.5" fill="#1B9E4B"/>""",
    "documents": """
<path d="M16 6h22l12 12v38a3 3 0 0 1-3 3H16a3 3 0 0 1-3-3V9a3 3 0 0 1 3-3z" fill="#FFFFFF" stroke="#90A4AE" stroke-width="1.5"/>
<path d="M38 6v9a3 3 0 0 0 3 3h9z" fill="#CFD8DC"/>
<rect x="20" y="26" width="24" height="3" rx="1.5" fill="#1E88E5"/>
<rect x="20" y="33" width="24" height="3" rx="1.5" fill="#1E88E5"/>
<rect x="20" y="40" width="24" height="3" rx="1.5" fill="#1E88E5"/>
<rect x="20" y="47" width="16" height="3" rx="1.5" fill="#1E88E5"/>""",
    "pictures": """
<rect x="6" y="12" width="52" height="40" rx="5" fill="#29B6F6"/>
<circle cx="44" cy="24" r="5" fill="#FFF59D"/>
<path d="M6 44l16-16 12 12 7-6 17 14v-1 1a5 5 0 0 1-5 5H11a5 5 0 0 1-5-5z" fill="#0277BD"/>
<path d="M6 46l16-12 20 18H11a5 5 0 0 1-5-5z" fill="#01579B" opacity=".6"/>""",
    "music": """
<path d="M24 14l28-6v34" fill="none" stroke="#F4511E" stroke-width="5" stroke-linejoin="round"/>
<path d="M24 14v34" stroke="#F4511E" stroke-width="5"/>
<ellipse cx="17" cy="49" rx="9" ry="7" fill="#F4511E"/>
<ellipse cx="45" cy="43" rx="9" ry="7" fill="#F4511E"/>
<path d="M24 14l28-6v7l-28 6z" fill="#BF360C"/>""",
    "videos": """
<rect x="6" y="12" width="52" height="40" rx="6" fill="#7E57C2"/>
<path d="M27 22v20l17-10z" fill="#FFFFFF"/>""",
    "home": """
<path d="M32 7L6 29h7v26a3 3 0 0 0 3 3h32a3 3 0 0 0 3-3V29h7z" fill="#FFC53D"/>
<path d="M32 7L6 29h5L32 11l21 18h5z" fill="#E29A16"/>
<path d="M4 30L32 6l28 24" fill="none" stroke="#1E88E5" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<rect x="26" y="38" width="12" height="20" rx="1.5" fill="#1E88E5"/>""",
    "thispc": """
<rect x="4" y="8" width="56" height="38" rx="4" fill="#37474F"/>
<rect x="7" y="11" width="50" height="32" rx="2" fill="#1E88E5"/>
<path d="M7 34c14-10 30-12 50-6v13a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2z" fill="#42A5F5"/>
<path d="M26 46h12l2 8H24z" fill="#90A4AE"/>
<rect x="18" y="54" width="28" height="4" rx="2" fill="#607D8B"/>""",
    "drive": """
<rect x="4" y="22" width="56" height="24" rx="5" fill="#B0BEC5"/>
<rect x="4" y="22" width="56" height="12" rx="5" fill="#CFD8DC"/>
<rect x="4" y="34" width="56" height="12" rx="0" fill="#90A4AE" opacity=".5"/>
<circle cx="50" cy="40" r="2.5" fill="#43A047"/>
<rect x="10" y="38" width="22" height="3" rx="1.5" fill="#607D8B"/>
""",
    "drive-removable": """
<rect x="16" y="6" width="32" height="18" rx="2" fill="#90A4AE"/>
<rect x="22" y="11" width="6" height="5" fill="#455A64"/>
<rect x="36" y="11" width="6" height="5" fill="#455A64"/>
<rect x="12" y="22" width="40" height="36" rx="5" fill="#455A64"/>
<rect x="12" y="22" width="40" height="10" rx="5" fill="#546E7A"/>
<circle cx="32" cy="45" r="4" fill="#26C6DA"/>""",
    "network": """
<rect x="4" y="8" width="30" height="22" rx="3" fill="#37474F"/>
<rect x="6.5" y="10.5" width="25" height="17" rx="1.5" fill="#29B6F6"/>
<rect x="30" y="28" width="30" height="22" rx="3" fill="#37474F"/>
<rect x="32.5" y="30.5" width="25" height="17" rx="1.5" fill="#29B6F6"/>
<path d="M19 30v12h11M45 50v8H10v-8" fill="none" stroke="#607D8B" stroke-width="3"/>""",
    "trash": """
<path d="M14 18h36l-3.4 36.6A4 4 0 0 1 42.6 58H21.4a4 4 0 0 1-4-3.4z" fill="#CFD8DC"/>
<path d="M14 18h36l-.6 6H14.6z" fill="#90A4AE"/>
<rect x="10" y="12" width="44" height="7" rx="3.5" fill="#78909C"/>
<rect x="26" y="7" width="12" height="6" rx="2" fill="#78909C"/>
<path d="M32 30l-5 8h3v7h4v-7h3z" fill="#43A047"/>
<path d="M26 47h12" stroke="#43A047" stroke-width="3" stroke-linecap="round"/>""",
}


def write(path, body):
    with open(path, "w") as f:
        f.write(body)


def folder_svg(glyph=None):
    inner = ""
    if glyph:
        # Place glyph in the lower-right of the front flap, like the
        # Windows 11 known-folder icons.
        inner = f'<g transform="translate(24 24) scale(0.5)">{GLYPHS[glyph]}</g>'
    return f"{HEAD}{FOLDER_DEFS}{FOLDER_BODY}{inner}</svg>\n"


def main():
    os.makedirs(PLACES, exist_ok=True)
    os.makedirs(APPS, exist_ok=True)
    write(os.path.join(PLACES, "w11-folder.svg"), folder_svg())
    for name in ("desktop", "downloads", "documents", "pictures", "music", "videos"):
        write(os.path.join(PLACES, f"w11-folder-{name}.svg"), folder_svg(name))
    for name, glyph in GLYPHS.items():
        write(os.path.join(PLACES, f"w11-{name}.svg"), f"{HEAD}{glyph}</svg>\n")
    # Zipped folder: the folder with a zipper down the middle.
    teeth = "".join(f'<rect x="{29 if i % 2 else 33}" y="{19 + i * 3}" width="3" height="2" fill="#5F6B73"/>'
                    for i in range(12))
    zipped = (f"{HEAD}{FOLDER_DEFS}{FOLDER_BODY}"
              '<rect x="31" y="15" width="3" height="40" fill="#8A969E"/>'
              f"{teeth}"
              '<rect x="28" y="52" width="9" height="7" rx="1.5" fill="#5F6B73"/></svg>\n')
    write(os.path.join(PLACES, "w11-folder-zip.svg"), zipped)
    # App icon: folder with the characteristic blue band.
    app = (f"{HEAD}{FOLDER_DEFS}{FOLDER_BODY}"
           '<rect x="4" y="30" width="56" height="9" fill="#1E88E5"/>'
           '<rect x="4" y="30" width="56" height="3" fill="#64B5F6"/></svg>\n')
    write(os.path.join(APPS, "io.github.tayvee_create.Casement.svg"), app)


if __name__ == "__main__":
    main()
