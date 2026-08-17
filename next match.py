import io
import os
import re
import unicodedata
from datetime import datetime, timedelta

import os
import time
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from PIL import Image, ImageDraw, ImageFont
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception as _e:
    print('  [heic] pillow-heif not available: %s' % _e)

import json
import time
import requests

# ============================================================
# CONFIG
# ============================================================
CREDENTIALS_FILE = 'credentials.json'

# --- Fixtures sheet ---
FIXTURES_SHEET_ID = '1j6ZN3N8aXnB9vKFdWeXhY-fyo8aH1JlmhWZWHwzgu-E'
FRIENDLY_TAB = 'Friendly Fixtures'
LEAGUECUP_TAB = 'League & Cup Fixtures'
INDEX_TAB = 'Index'

# Fixture columns (0-based): Date Time Home Away Location MatchType Round Status
FX_DATE, FX_TIME, FX_HOME, FX_AWAY, FX_LOC, FX_TYPE, FX_ROUND, FX_STATUS = range(8)

# Index columns: Team | Calendar ID | League | Training Calendar ID
IDX_TEAM, IDX_CAL, IDX_LEAGUE = 0, 1, 2

# Teams this script handles
OUR_TEAMS = ['11A', '11B', '11C']

# --- Drive folders ---
LOGO_FOLDER_ID = '19NNyf1trl1LoA7Tth7PFMbRAv65oXeeR'      # opposition + galaksia logos
ASSETS_FOLDER_ID = '1aQ1ay_nCSQptlPyigVvzwbkReIdKioZV'    # background + font
LEAGUE_LOGO_FOLDER_ID = '19NNyf1trl1LoA7Tth7PFMbRAv65oXeeR'  # league logos
POST_UPLOAD_FOLDER_ID = '1-MAJwpIAjQvzXQdsPdqmkX4NGrM8YFt5'

BACKGROUND_NAME = '11-a-side'
FONT_NAME = 'Etna'
GALAKSIA_LOGO_NAME = 'galaksia praha 23'

# --- Canvas (from the provided background ~768x960 -> scale up, keep 4:5) ---
CANVAS_W = 1536
CANVAS_H = 1920

# --- Colours ---
WHITE = (255, 255, 255)
GREEN = (75, 186, 105)  # #4bba69

# --- Output ---
OUTPUT_DIR = 'output'
_FONT_LOCAL = os.path.join(OUTPUT_DIR, '_etna.ttf')

IMG_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif')

# --- Layout (as fractions of canvas, tuned to the provided template) ---
# Header box (the empty space inside "NEXT GAME"): left, right, top, bottom
HEADER_LEFT = int(CANVAS_W * 0.17)
HEADER_RIGHT = int(CANVAS_W * 0.885)
HEADER_TOP = int(CANVAS_H * 0.145)
HEADER_BOTTOM = int(CANVAS_H * 0.205)
HEADER_SIZE = int(CANVAS_H * 0.075)   # base render size before stretching

# Logos row (lowered)
# Logos row - centered between header band and white name stripe.
# The header ends ~0.33 and the white stripe starts ~0.685 of the canvas.
LOGO_BAND_TOP = int(CANVAS_H * 0.35)
LOGO_BAND_BOTTOM = int(CANVAS_H * 0.70)
LOGO_CY = (LOGO_BAND_TOP + LOGO_BAND_BOTTOM) // 2   # = ~0.525
LOGO_MAX = int(CANVAS_W * 0.28)
LOGO_LEFT_CX = int(CANVAS_W * 0.27)
LOGO_RIGHT_CX = int(CANVAS_W * 0.73)

# Middle name band
NAME_CY = int(CANVAS_H * 0.78)
NAME_SIZE = int(CANVAS_H * 0.045)
NAME_LEFT_CX = int(CANVAS_W * 0.235)
NAME_RIGHT_CX = int(CANVAS_W * 0.765)
NAME_MAX_W = int(CANVAS_W * 0.42)
NAME_LINE_GAP = int(CANVAS_H * 0.050)

# Bottom band
BOTTOM_L1_CY = int(CANVAS_H * 0.885)
BOTTOM_L2_CY = int(CANVAS_H * 0.915)
BOTTOM_SIZE = int(CANVAS_H * 0.027)      # date | location
BOTTOM_SIZE2 = int(CANVAS_H * 0.024)     # kick off (smaller)

# League logo bottom-left
LEAGUE_LOGO_MAX = int(CANVAS_W * 0.10)
LEAGUE_LOGO_CX = int(CANVAS_W * 0.50)     # horizontally centered
LEAGUE_LOGO_CY = int(CANVAS_H * 0.965)    # under the kick-off line

# ============================================================
# AUTH
# ============================================================
def get_creds():
    scope = ['https://spreadsheets.google.com/feeds',
             'https://www.googleapis.com/auth/drive']
    return ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)

def load_meta_config():
    with open('meta_config.json') as f:
        return json.load(f)

def get_gspread_client():
    return gspread.authorize(get_creds())

def get_drive_service():
    return build('drive', 'v3', credentials=get_creds())

# Required scope for managing files created or opened by the app
SCOPES = ['https://www.googleapis.com/auth/drive']

def get_user_drive_service(client_secrets_file='client_secret.json'):
    """Authenticates the user using OAuth 2.0 and returns the Drive API service instance."""
    creds = None
    
    # token.json stores the user's access and refresh tokens
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
    # If there are no valid credentials, trigger authorization flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
            creds = flow.run_local_server(port=0)
            
        # Save credentials for future unattended runs
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('drive', 'v3', credentials=creds)

# ============================================================
# NORMALIZE / MATCH
# ============================================================
def _norm(s):
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '', s)
    return s

def _tokens(s):
    if not s:
        return []
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    return [t for t in re.split(r'[^a-z0-9]+', s) if t]

# ============================================================
# DRIVE HELPERS
# ============================================================
def list_folder(drive, folder_id):
    out = []
    page_token = None
    while True:
        resp = drive.files().list(
            q="'%s' in parents and trashed = false" % folder_id,
            fields='nextPageToken, files(id,name,mimeType)',
            pageToken=page_token).execute()
        out.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return out

def download_bytes(drive, file_id):
    return drive.files().get_media(fileId=file_id).execute()


def upload_public_image(drive, image_path, folder_id='1-MAJwpIAjQvzXQdsPdqmkX4NGrM8YFt5'):
    """Uploads an image to Google Drive using your account quota and makes it publicly accessible."""
    last_err = None
    file_name = os.path.basename(image_path)
    
    file_metadata = {
        'name': file_name,
        'parents': [folder_id]
    }
    
    media = MediaFileUpload(image_path, mimetype='image/png', resumable=True)

    for attempt in range(4):
        try:
            # 1. Upload file
            file = drive.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink, webContentLink'
            ).execute()
            
            file_id = file.get('id')

            # 2. Make file publicly readable via link
            drive.permissions().create(
                fileId=file_id,
                body={'type': 'anyone', 'role': 'reader'}
            ).execute()

            # Return direct link or web link
            public_url = file.get('webContentLink') or file.get('webViewLink')
            return public_url, file_id

        except Exception as e:
            last_err = str(e)
            time.sleep(5 * (attempt + 1))

    raise RuntimeError('Google Drive image upload failed after retries: %s' % last_err)

def find_by_basename(files, name):
    """Match ignoring extension, accents, case, punctuation."""
    target = _norm(name)
    for f in files:
        base = os.path.splitext(f['name'])[0]
        if _norm(base) == target:
            return f
    return None

def download_image_from(drive, files, name):
    f = find_by_basename(files, name)
    if not f:
        return None
    data = download_bytes(drive, f['id'])
    return Image.open(io.BytesIO(data)).convert('RGBA')

def ensure_font(drive, league_files):
    if os.path.exists(_FONT_LOCAL):
        return _FONT_LOCAL
    f = find_by_basename(league_files, FONT_NAME)
    if not f:
        print('  Font "%s" not found; default font.' % FONT_NAME)
        return None
    data = download_bytes(drive, f['id'])
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(_FONT_LOCAL, 'wb') as fh:
        fh.write(data)
    return _FONT_LOCAL

def find_logo(drive, logo_files, sheet_name):
    """Fuzzy logo match. Try full name, then strip trailing single-letter
    team-level token (A-D / VET / VETS) and retry."""
    def try_match(name):
        target = _norm(name)
        if not target:
            return None
        # exact-ish
        f = find_by_basename(logo_files, name)
        if f:
            return f
        # token subset / contains
        tset = set(_tokens(name))
        best = None
        for lf in logo_files:
            base = os.path.splitext(lf['name'])[0]
            lset = set(_tokens(base))
            if not lset:
                continue
            if tset == lset or tset <= lset or lset <= tset:
                return lf
            # overlap heuristic
            overlap = len(tset & lset)
            if overlap and overlap >= max(1, min(len(tset), len(lset)) - 0):
                best = lf
        return best

    f = try_match(sheet_name)
    if f:
        return f
    # strip trailing team-level token
    toks = _tokens(sheet_name)
    if toks and (toks[-1] in ('a', 'b', 'c', 'd', 'vet', 'vets')):
        stripped = ' '.join(toks[:-1])
        f = try_match(stripped)
        if f:
            return f
    return None

# ============================================================
# COLOUR EXTRACTION
# ============================================================
def remove_edge_background(img, tol=40):
    """Remove the background that is connected to the image border, preserving
    interior colours (e.g. white inside a logo). Flood-fills from all edges."""
    img = img.convert('RGBA')
    w, h = img.size
    px = img.load()

    # sample border colour (average of corners)
    corners = [px[0, 0], px[w-1, 0], px[0, h-1], px[w-1, h-1]]
    br = sum(c[0] for c in corners) // 4
    bg = sum(c[1] for c in corners) // 4
    bb = sum(c[2] for c in corners) // 4

    def close(c):
        return (abs(c[0]-br) <= tol and abs(c[1]-bg) <= tol and abs(c[2]-bb) <= tol)

    from collections import deque
    visited = bytearray(w * h)
    dq = deque()
    for x in range(w):
        for y in (0, h-1):
            dq.append((x, y))
    for y in range(h):
        for x in (0, w-1):
            dq.append((x, y))

    while dq:
        x, y = dq.popleft()
        idx = y * w + x
        if visited[idx]:
            continue
        visited[idx] = 1
        c = px[x, y]
        if c[3] == 0 or close(c):
            px[x, y] = (c[0], c[1], c[2], 0)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h and not visited[ny * w + nx]:
                    dq.append((nx, ny))
    # crop to content
    cb = img.getbbox()
    return img.crop(cb) if cb else img

def dominant_two_colours(logo_img):
    """Two dominant *chromatic* colours (ignores near-white/near-black/greys
    so e.g. Přední Kopanina -> blue & red, not black->white)."""
    img = logo_img.convert('RGBA')
    img.thumbnail((160, 160), Image.LANCZOS)
    px = img.load()
    w, h = img.size
    buckets = {}
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 128:
                continue
            mx, mn = max(r, g, b), min(r, g, b)
            sat = mx - mn
            lum = 0.299*r + 0.587*g + 0.114*b
            # skip near-white, near-black, and low-saturation greys
            if lum > 235 or lum < 25 or sat < 40:
                continue
            key = (r // 24, g // 24, b // 24)
            d = buckets.setdefault(key, [0, 0, 0, 0])
            d[0] += r; d[1] += g; d[2] += b; d[3] += 1
    if not buckets:
        # fall back to any colours (old behaviour) if nothing chromatic
        return _dominant_any(logo_img)

    cols = [((s[0]//s[3], s[1]//s[3], s[2]//s[3]), s[3]) for s in buckets.values()]
    cols.sort(key=lambda t: t[1], reverse=True)

    def lum(c):
        return 0.299*c[0] + 0.587*c[1] + 0.114*c[2]

    top = [c for c, _ in cols]
    # pick the most dominant, then the most different-hued from it
    first = top[0]
    second = None
    for c in top[1:]:
        if abs(lum(c) - lum(first)) > 30 or \
           (abs(c[0]-first[0]) + abs(c[1]-first[1]) + abs(c[2]-first[2])) > 120:
            second = c
            break
    if second is None:
        return first, None
    # darker -> lighter left to right
    if lum(first) <= lum(second):
        return first, second
    return second, first

def _dominant_any(logo_img):
    img = logo_img.convert('RGBA')
    img.thumbnail((120, 120), Image.LANCZOS)
    px = img.load(); w, h = img.size
    buckets = {}
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 128:
                continue
            key = (r // 32, g // 32, b // 32)
            d = buckets.setdefault(key, [0, 0, 0, 0])
            d[0]+=r; d[1]+=g; d[2]+=b; d[3]+=1
    if not buckets:
        return WHITE, None
    cols = [((s[0]//s[3], s[1]//s[3], s[2]//s[3]), s[3]) for s in buckets.values()]
    cols.sort(key=lambda t: t[1], reverse=True)
    top = [c for c, _ in cols[:6]]
    if len(top) == 1:
        return top[0], None
    lum = lambda c: 0.299*c[0]+0.587*c[1]+0.114*c[2]
    top.sort(key=lum)
    if abs(lum(top[0]) - lum(top[-1])) < 25:
        return top[0], None
    return top[0], top[-1]

# ============================================================
# GRADIENT TEXT
# ============================================================
def gradient_text(_unused, text, font, color_left, color_right):
    tmp = Image.new('RGBA', (10, 10))
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), text, font=font)
    tw = int(bbox[2] - bbox[0])
    th = int(bbox[3] - bbox[1])
    pad = 6
    W = tw + pad * 2
    H = th + pad * 2

    mask = Image.new('L', (W, H), 0)
    md = ImageDraw.Draw(mask)
    md.text((pad, pad), text, font=font, fill=255, anchor='lt')

    if color_right is None:
        grad = Image.new('RGBA', (W, H), color_left + (255,))
    else:
        grad = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        gpx = grad.load()
        for x in range(W):
            t = x / max(1, W - 1)
            r = int(color_left[0] + (color_right[0] - color_left[0]) * t)
            g = int(color_left[1] + (color_right[1] - color_left[1]) * t)
            b = int(color_left[2] + (color_right[2] - color_left[2]) * t)
            for y in range(H):
                gpx[x, y] = (r, g, b, 255)

    out = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out

def stretch_to_width(img, target_w):
    """Horizontally stretch an RGBA image to target_w, keeping height."""
    if img.width == target_w:
        return img
    return img.resize((target_w, img.height), Image.LANCZOS)

def render_name(font_path, text, max_w, base_size, col_l, col_r):
    tmp = Image.new('RGBA', (10, 10))
    d = ImageDraw.Draw(tmp)
    f = load_font(font_path, base_size)
    letter_sp = max(1, int(base_size * 0.06))

    def sp_width(dd, s, fnt):
        if not s:
            return 0
        total = 0
        for ch in s:
            total += dd.textbbox((0, 0), ch, font=fnt)[2] + letter_sp
        return total - letter_sp

    def draw_sp(dd, xy, s, fnt, fill, stroke_w=0):
        x, y = xy
        for ch in s:
            if stroke_w:
                dd.text((x, y), ch, font=fnt, fill=fill,
                        stroke_width=stroke_w, stroke_fill=fill)
            else:
                dd.text((x, y), ch, font=fnt, fill=fill)
            x += dd.textbbox((0, 0), ch, font=fnt)[2] + letter_sp

    if sp_width(d, text, f) <= max_w:
        lines = [text]
    else:
        words = text.split()
        if len(words) < 2:
            f = fit_font(font_path, text, max_w, base_size)
            letter_sp = max(1, int(base_size * 0.06))
            lines = [text]
        else:
            best = None
            for k in range(1, len(words)):
                a = ' '.join(words[:k]); b = ' '.join(words[k:])
                diff = abs(sp_width(d, a, f) - sp_width(d, b, f))
                if best is None or diff < best[0]:
                    best = (diff, a, b)
            lines = [best[1], best[2]]
            while max(sp_width(d, ln, f) for ln in lines) > max_w and base_size > 20:
                base_size -= 2
                f = load_font(font_path, base_size)
                letter_sp = max(1, int(base_size * 0.06))

    asc, desc = f.getmetrics()
    line_h = asc + desc
    gap = int(base_size * 0.15)
    W = max(int(sp_width(d, ln, f)) for ln in lines) + 20
    H = line_h * len(lines) + gap * (len(lines) - 1) + 20

    # gradient-filled text (alpha mask)
    txt = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(txt)
    y = 10
    for ln in lines:
        w = int(sp_width(td, ln, f))
        draw_sp(td, ((W - w) // 2, y), ln, f, (255, 255, 255, 255))
        y += line_h + gap

    if col_r is None:
        grad = Image.new('RGBA', (W, H), col_l + (255,))
    else:
        grad = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        gpx = grad.load()
        for x in range(W):
            t = x / max(1, W - 1)
            r = int(col_l[0] + (col_r[0]-col_l[0])*t)
            g = int(col_l[1] + (col_r[1]-col_l[1])*t)
            b = int(col_l[2] + (col_r[2]-col_l[2])*t)
            for yy in range(H):
                gpx[x, yy] = (r, g, b, 255)

    alpha = txt.split()[3]

    # white halo behind
    # Outline colour: white normally, but black if either name colour is near-white.
    def is_near_white(c):
        return c is not None and c[0] >= 220 and c[1] >= 220 and c[2] >= 220

    outline_col = (255, 255, 255, 255)
    if is_near_white(col_l) or is_near_white(col_r):
        outline_col = (0, 0, 0, 255)

    sw = max(4, int(base_size * 0.08))
    halo = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    y2 = 10
    for ln in lines:
        w = int(sp_width(hd, ln, f))
        draw_sp(hd, ((W - w) // 2, y2), ln, f, outline_col, stroke_w=sw)
        y2 += line_h + gap

    gradient_text_img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    gradient_text_img.paste(grad, (0, 0), alpha)

    out = Image.alpha_composite(halo, gradient_text_img)
    cb = out.getbbox()
    if cb:
        out = out.crop(cb)
    return out

def paste_centered(bg, img, cx, cy):
    x = int(cx - img.width / 2)
    y = int(cy - img.height / 2)
    bg.alpha_composite(img, (x, y))

def fit_font(font_path, text, max_w, start_size, min_size=20):
    """Shrink font until text fits in max_w."""
    size = start_size
    tmp = Image.new('RGBA', (10, 10))
    d = ImageDraw.Draw(tmp)
    while size > min_size:
        f = load_font(font_path, size)
        b = d.textbbox((0, 0), text, font=f)
        if (b[2] - b[0]) <= max_w:
            return f
        size -= 2
    return load_font(font_path, min_size)

def load_font(font_path, size):
    if font_path and os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    return ImageFont.load_default()

# ============================================================
# LABELS
# ============================================================
def galaksia_label(team):
    """11A -> 'GALAKSIA PRAHA 23', 11B -> '... 23 B', 11C -> '... 23 C'."""
    base = 'GALAKSIA PRAHA 23'
    if team.upper().endswith('B'):
        return base + ' B'
    if team.upper().endswith('C'):
        return base + ' C'
    return base

def header_text(match_type, league, round_num):
    mt = (match_type or '').strip().lower()
    if mt == 'friendly':
        return 'FRIENDLY'
    if mt == 'cup':
        base = (league + ' CUP').strip()
        if round_num:
            return '%s GAME %s' % (base, round_num)
        return base
    # league
    base = league.strip()
    if round_num:
        return ('%s GAME %s' % (base, round_num)).strip()
    return base if base else 'NEXT GAME'

def ordinal(n):
    if 10 <= n % 100 <= 20:
        suf = 'TH'
    else:
        suf = {1: 'ST', 2: 'ND', 3: 'RD'}.get(n % 10, 'TH')
    return '%d%s' % (n, suf)

def date_line(match_date):
    months = ['JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE',
              'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER']
    days = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY',
            'SATURDAY', 'SUNDAY']
    return '%s %s %s' % (days[match_date.weekday()],
                         ordinal(match_date.day),
                         months[match_date.month - 1])

def clean_team_name(name):
    s = (name or '').strip()
    s = re.sub(r'\s*,?\s*(z\.s\.|a\.s\.)\s*$', '', s, flags=re.I)
    return s.strip()

# ============================================================
# IMAGE BUILD
# ============================================================
def build_image(fixture, bg_src, font_path,
                gp_logo, opp_logo, league_logo,
                gp_side, opp_name, gp_label,
                gp_colors, opp_colors, header_str,
                date_str, loc_str, time_str, match_type):
    bg = bg_src.copy().convert('RGBA')
    if bg.size != (CANVAS_W, CANVAS_H):
        bg = bg.resize((CANVAS_W, CANVAS_H))
    draw = ImageDraw.Draw(bg)

    # --- Header (white -> green, stretched to fill an explicit box) ---
    hfont = load_font(font_path, HEADER_SIZE)
    htxt = gradient_text(None, header_str, hfont, WHITE, GREEN)
    box_w = HEADER_RIGHT - HEADER_LEFT
    box_h = HEADER_BOTTOM - HEADER_TOP
    htxt = htxt.resize((box_w, box_h), Image.LANCZOS)
    bg.alpha_composite(htxt, (HEADER_LEFT, HEADER_TOP))

    # --- Logos row: equal size, background removed ---
    if gp_side == 'left':
        left_logo, right_logo = gp_logo, opp_logo
    else:
        left_logo, right_logo = opp_logo, gp_logo

    def prep_logo(lg):
        if lg is None:
            return None
        l = remove_edge_background(lg)
        # hard-crop any remaining transparent margin
        cb = l.getbbox()
        if cb:
            l = l.crop(cb)
        return l

    ll = prep_logo(left_logo)
    rl = prep_logo(right_logo)

    # Normalize both logos to the same target HEIGHT so they look equal size,
    # then cap width to LOGO_MAX.
    def scale_logo(l):
        if l is None:
            return None
        target_h = LOGO_MAX
        scale = target_h / l.height
        new_w = max(1, int(l.width * scale))
        new_h = max(1, int(l.height * scale))
        l = l.resize((new_w, new_h), Image.LANCZOS)
        if l.width > LOGO_MAX:  # too wide -> cap by width instead
            scale = LOGO_MAX / l.width
            l = l.resize((LOGO_MAX, max(1, int(l.height * scale))), Image.LANCZOS)
        return l

    ll = scale_logo(ll)
    rl = scale_logo(rl)
    if ll is not None:
        paste_centered(bg, ll, LOGO_LEFT_CX, LOGO_CY)
    if rl is not None:
        paste_centered(bg, rl, LOGO_RIGHT_CX, LOGO_CY)

    # --- Names row (bigger, vertically centered, up to 2 lines) ---
    if gp_side == 'left':
        gp_l, gp_r = (GREEN, WHITE)
        gp_cx, opp_cx = NAME_LEFT_CX, NAME_RIGHT_CX
    else:
        gp_l, gp_r = (WHITE, GREEN)
        gp_cx, opp_cx = NAME_RIGHT_CX, NAME_LEFT_CX

    opp_dark, opp_light = opp_colors
    opp_l, opp_r = opp_dark, opp_light

    gp_img = render_name(font_path, gp_label, NAME_MAX_W, NAME_SIZE, gp_l, gp_r)
    opp_img = render_name(font_path, opp_name.upper(), NAME_MAX_W, NAME_SIZE, opp_l, opp_r)
    gp_img = gp_img.rotate(-0.5, expand=True, resample=Image.BICUBIC)
    opp_img = opp_img.rotate(-0.5, expand=True, resample=Image.BICUBIC)
    paste_centered(bg, gp_img, gp_cx, NAME_CY)
    paste_centered(bg, opp_img, opp_cx, NAME_CY)

    # --- Bottom band (fixed size, centered) ---
    l1 = '%s | %s' % (date_str, loc_str.upper())
    l2 = 'KICK OFF %s' % time_str
    f1 = load_font(font_path, BOTTOM_SIZE)
    f2 = load_font(font_path, BOTTOM_SIZE2)
    i1 = gradient_text(None, l1, f1, WHITE, WHITE)
    i2 = gradient_text(None, l2, f2, WHITE, WHITE)
    paste_centered(bg, i1, CANVAS_W // 2, BOTTOM_L1_CY)
    paste_centered(bg, i2, CANVAS_W // 2, BOTTOM_L2_CY)

    # --- League logo bottom-center, under kick-off (not for friendlies) ---
    if league_logo is not None and (match_type or '').strip().lower() != 'friendly':
        lg = remove_edge_background(league_logo)
        cb = lg.getbbox()
        if cb:
            lg = lg.crop(cb)
        lg.thumbnail((LEAGUE_LOGO_MAX, LEAGUE_LOGO_MAX), Image.LANCZOS)
        lx = int(LEAGUE_LOGO_CX - lg.width / 2)
        ly = int(LEAGUE_LOGO_CY - lg.height / 2)
        bg.alpha_composite(lg, (lx, ly))

    return bg.convert('RGB')

STORY_W = 1080
STORY_H = 1920

def make_story_version(feed_img_path):
    """Build a 9:16 story image: blurred zoomed background + centered feed image.
    Returns the saved story file path."""
    from PIL import ImageFilter
    feed = Image.open(feed_img_path).convert('RGB')

    # Blurred background: cover the whole 9:16 canvas
    bg = feed.copy()
    scale = max(STORY_W / bg.width, STORY_H / bg.height)
    bg = bg.resize((int(bg.width * scale), int(bg.height * scale)), Image.LANCZOS)
    left = (bg.width - STORY_W) // 2
    top = (bg.height - STORY_H) // 2
    bg = bg.crop((left, top, left + STORY_W, top + STORY_H))
    bg = bg.filter(ImageFilter.GaussianBlur(40))

    # Foreground: fit the feed image within the story width, centered
    fg = feed.copy()
    fscale = min(STORY_W / fg.width, STORY_H / fg.height) * 0.92
    fg = fg.resize((int(fg.width * fscale), int(fg.height * fscale)), Image.LANCZOS)
    fx = (STORY_W - fg.width) // 2
    fy = (STORY_H - fg.height) // 2
    bg.paste(fg, (fx, fy))

    out_path = feed_img_path.replace('.png', '_story.png')
    bg.save(out_path, 'PNG', quality=95)
    return out_path

# ============================================================
# CAPTION
# ============================================================
def build_caption(gp_label, opp_name, gp_side, match_type, league,
                  round_num, date_str, loc_str, time_str):
    home_away = 'at home' if gp_side == 'left' else 'away'
    if gp_side == 'left':
        matchup = '%s vs %s' % (gp_label.title(), opp_name)
    else:
        matchup = '%s vs %s' % (opp_name, gp_label.title())

    mt = (match_type or '').lower()
    if mt == 'friendly':
        comp = 'Friendly'
    elif mt == 'cup':
        comp = '%s Cup%s' % (league, (' – Game %s' % round_num) if round_num else '')
    else:
        comp = '%s%s' % (league, (' – Game %s' % round_num) if round_num else '')

    caption = (
        "⚫️⚪️🟢 NEXT GAME!\n\n"
        "%s\n"
        "🏆 %s\n"
        "📅 %s\n"
        "🕒 Kick off %s\n"
        "📍 %s (%s)\n\n"
        "Come support the boys! 💪\n\n"
        "#GalaksiaPraha23 #GP23 #NextGame #Prague #Praha #PragueFootball "
        "#Fotbal #BlackWhiteGreen #GreenArmy #Matchday #COYG #FootballFamily"
        % (matchup, comp, date_str.title(), time_str, loc_str, home_away)
    )
    return caption

# ============================================================
# META (STUB - wire up later)
# ============================================================
GRAPH = 'https://graph.facebook.com/v20.0'

def _fb_page_photo(page_id, token, image_url, caption, published=True):
    r = requests.post('%s/%s/photos' % (GRAPH, page_id),
                      data={'url': image_url, 'caption': caption,
                            'published': 'true' if published else 'false',
                            'access_token': token})
    r.raise_for_status()
    return r.json()

def _fb_story(page_id, token, photo_id):
    r = requests.post('%s/%s/photo_stories' % (GRAPH, page_id),
                      data={'photo_id': photo_id, 'access_token': token})
    r.raise_for_status()
    return r.json()

def _ig_publish(ig_id, token, image_url, caption=None, is_story=False):
    data = {'image_url': image_url, 'access_token': token}
    if is_story:
        data['media_type'] = 'STORIES'
    elif caption:
        data['caption'] = caption
    c = requests.post('%s/%s/media' % (GRAPH, ig_id), data=data)
    c.raise_for_status()
    creation_id = c.json()['id']
    p = requests.post('%s/%s/media_publish' % (GRAPH, ig_id),
                      data={'creation_id': creation_id, 'access_token': token})
    p.raise_for_status()
    return p.json()

def _get_page_token(page_id, user_token):
    """Get the Page access token for a system-user token via me/accounts."""
    r = requests.get('%s/me/accounts' % GRAPH,
                     params={'access_token': user_token, 'limit': 200})
    r.raise_for_status()
    for p in r.json().get('data', []):
        if str(p.get('id')) == str(page_id):
            return p['access_token']
    raise RuntimeError('Page %s not found in me/accounts' % page_id)

def post_to_meta(image_path, caption, image_url=None, story_url=None):
    """FB feed + IG feed use image_url (4:5). FB/IG stories use story_url (9:16)."""
    cfg = load_meta_config()
    page_id = cfg['page_id']
    ig_id = cfg['ig_user_id']
    user_token = cfg['page_access_token']

    if not image_url:
        print('    [meta] ERROR: no public image_url provided; cannot post.')
        return

    try:
        token = _get_page_token(page_id, user_token)  # Page token for FB
    except Exception as e:
        print('    [meta] could not derive Page token: %s' % e)
        token = user_token
    ig_token = user_token

    try:
        _fb_page_photo(page_id, token, image_url, caption, published=True)
        print('    [meta] FB feed OK')
    except Exception as e:
        print('    [meta] FB feed FAILED: %s' % e)

    try:
        su = story_url or image_url
        photo = _fb_page_photo(page_id, token, su, '', published=False)
        _fb_story(page_id, token, photo['id'])
        print('    [meta] FB story OK')
    except Exception as e:
        print('    [meta] FB story FAILED: %s' % e)

    try:
        _ig_publish(ig_id, ig_token, image_url, caption=caption, is_story=False)
        print('    [meta] IG feed OK')
    except Exception as e:
        print('    [meta] IG feed FAILED: %s' % e)

    try:
        _ig_publish(ig_id, ig_token, story_url or image_url, is_story=True)
        print('    [meta] IG story OK')
    except Exception as e:
        print('    [meta] IG story FAILED: %s' % e)

# ============================================================
# ERROR EMAIL (STUB - wire up later)
# ============================================================
def send_error_email(errors):
    """TODO: send `errors` list to info@galaksia23.com."""
    if not errors:
        return
    print('  [error-email] (stub) would email %d error(s) to info@galaksia23.com:'
          % len(errors))
    for e in errors:
        print('    - %s' % e)

# ============================================================
# HELPERS
# ============================================================
def parse_date(value):
    s = (value or '').strip()
    if not s:
        return None
    for fmt in ('%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def parse_time(value):
    s = (value or '').strip()
    if not s:
        return None
    s2 = s.replace('h', ':')
    m = re.match(r'^(\d{1,2}):(\d{2})', s2)
    if m:
        return '%02d:%02d' % (int(m.group(1)), int(m.group(2)))
    m = re.match(r'^(\d{1,2})$', s2)
    if m:
        return '%02d:00' % int(m.group(1))
    return None

def build_team_league_map(client):
    ws = client.open_by_key(FIXTURES_SHEET_ID).worksheet(INDEX_TAB)
    data = ws.get_all_values()
    mapping = {}
    for row in data[1:]:
        if len(row) <= max(IDX_TEAM, IDX_LEAGUE):
            continue
        team = (row[IDX_TEAM] or '').strip()
        league = (row[IDX_LEAGUE] or '').strip()
        if team:
            mapping[team.lower()] = league
    return mapping

def which_side(home, away):
    """Return (gp_teams, opp_name, gp_side, is_derby).
    gp_side: 'left' if GP is home, 'right' if GP is away."""
    home_is = home.strip().upper() in [t.upper() for t in OUR_TEAMS]
    away_is = away.strip().upper() in [t.upper() for t in OUR_TEAMS]
    if home_is and away_is:
        return home.strip(), away.strip(), 'left', True
    if home_is:
        return home.strip(), away.strip(), 'left', False
    if away_is:
        return away.strip(), home.strip(), 'right', False
    return None, None, None, False

# ============================================================
# MAIN
# ============================================================
def run_next_game_generator():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    errors = []

    print('Auth...')
    client = get_gspread_client()
    drive = get_drive_service()
    user_drive = get_user_drive_service()

    print('Reading Index...')
    team_league = build_team_league_map(client)

    print('Listing Drive folders...')
    logo_files = list_folder(drive, LOGO_FOLDER_ID)
    asset_files = list_folder(drive, ASSETS_FOLDER_ID)
    league_files = list_folder(drive, LEAGUE_LOGO_FOLDER_ID)

    print('Loading background + font...')
    background = download_image_from(drive, asset_files, BACKGROUND_NAME)
    if background is None:
        msg = 'FATAL: background "%s" not found.' % BACKGROUND_NAME
        print(msg)
        errors.append(msg)
        send_error_email(errors)
        return
    font_files = list_folder(drive, '1-MAJwpIAjQvzXQdsPdqmkX4NGrM8YFt5')
    font_path = ensure_font(drive, font_files)

    print('Loading Galaksia logo...')
    gp_logo = download_image_from(drive, logo_files, GALAKSIA_LOGO_NAME)
    if gp_logo is None:
        errors.append('Galaksia logo "%s" not found in logo folder.' % GALAKSIA_LOGO_NAME)
    gp_colors = (GREEN, WHITE)

    today = datetime.now().date()
    print('Today: %s' % today)

    opp_logo_cache = {}
    league_logo_cache = {}
    generated = 0

    # ---- Phase 1: collect ALL Completed fixtures for our teams (both tabs) ----
    all_fx = []
    ss = client.open_by_key(FIXTURES_SHEET_ID)
    for tab in (FRIENDLY_TAB, LEAGUECUP_TAB):
        ws = ss.worksheet(tab)
        data = ws.get_all_values()
        for i, row in enumerate(data[1:], start=2):
            if len(row) <= FX_STATUS:
                continue
            m_date = parse_date(row[FX_DATE])
            if not m_date:
                continue
            if (row[FX_STATUS] or '').strip() != 'Completed':
                continue
            home = (row[FX_HOME] or '').strip()
            away = (row[FX_AWAY] or '').strip()
            gp_team, opp_name, gp_side, is_derby = which_side(home, away)
            if gp_team is None:
                continue
            all_fx.append({
                'tab': tab, 'row_i': i, 'row': row, 'date': m_date,
                'home': home, 'away': away, 'gp_team': gp_team.upper(),
                'opp_name': opp_name, 'gp_side': gp_side, 'is_derby': is_derby,
                'match_type': (row[FX_TYPE] or '').strip(),
                'round': (row[FX_ROUND] or '').strip(),
                'loc': (row[FX_LOC] or '').strip(),
            })

    # ---- The posting rule ----
    def should_post(fx):
        days = (fx['date'] - today).days
        # Condition 1: exactly 3 days away AND no match between today and F
        if days == 3:
            between = [g for g in all_fx
                       if g['gp_team'] == fx['gp_team']
                       and today <= g['date'] < fx['date']]
            if not between:
                return True
        # Condition 2: within 3 days AND most recent previous match was yesterday
        if 0 <= days <= 3:
            prev = [g['date'] for g in all_fx
                    if g['gp_team'] == fx['gp_team'] and g['date'] < today]
            if prev and (today - max(prev)).days == 1:
                return True
        return False

    # ---- Phase 2: build + post ----
    for fx in all_fx:
        if not should_post(fx):
            continue

        tab = fx['tab']; i = fx['row_i']; row = fx['row']
        gp_team = fx['gp_team']; opp_name = clean_team_name(fx['opp_name'])
        gp_side = fx['gp_side']; is_derby = fx['is_derby']
        match_type = fx['match_type']; round_num = fx['round']; loc = fx['loc']
        m_date = fx['date']

        tag = '%s row %d [%s vs %s]' % (tab, i, fx['home'], fx['away'])
        print('Processing %s' % tag)

        time_str = parse_time(row[FX_TIME])
        if not time_str:
            errors.append('%s: missing/invalid kick-off time.' % tag)
            continue

        league = team_league.get(gp_team.lower(), '')
        if not league:
            errors.append('%s: no league found in Index for team %s.' % (tag, gp_team))

        if is_derby:
            opp_logo = gp_logo
            opp_colors = (GREEN, WHITE)
        else:
            key = _norm(opp_name)
            if key not in opp_logo_cache:
                lf = find_logo(drive, logo_files, opp_name)
                if not lf:
                    # Fall back to the "no logo" placeholder file.
                    errors.append('%s: NO LOGO found for opponent "%s" - using placeholder.' % (tag, opp_name))
                    lf = find_by_basename(logo_files, 'no logo')
                if lf:
                    try:
                        d = download_bytes(drive, lf['id'])
                        opp_logo_cache[key] = Image.open(io.BytesIO(d)).convert('RGBA')
                    except Exception as e:
                        opp_logo_cache[key] = None
                        errors.append('%s: opponent logo load failed (%s): %s' % (tag, opp_name, e))
                else:
                    opp_logo_cache[key] = None
                    errors.append('%s: "no logo" placeholder also not found.' % tag)
            opp_logo = opp_logo_cache[key]
            opp_colors = dominant_two_colours(opp_logo) if opp_logo else (WHITE, None)

        if opp_logo is None:
            continue

        if league:
            lk = _norm(league)
            if lk not in league_logo_cache:
                lf = find_logo(drive, league_files, league)
                if lf:
                    try:
                        d = download_bytes(drive, lf['id'])
                        league_logo_cache[lk] = Image.open(io.BytesIO(d)).convert('RGBA')
                    except Exception:
                        league_logo_cache[lk] = None
                else:
                    league_logo_cache[lk] = None
                    errors.append('%s: no league logo for "%s".' % (tag, league))
            league_logo = league_logo_cache[lk]
        else:
            league_logo = None

        gp_label = galaksia_label(gp_team)
        header_str = header_text(match_type, league, round_num)
        d_str = date_line(m_date)

        try:
            img = build_image(
                row, background, font_path,
                gp_logo, opp_logo, league_logo,
                gp_side, opp_name, gp_label,
                gp_colors, opp_colors, header_str,
                d_str, loc, time_str, match_type)
        except Exception as e:
            errors.append('%s: image build failed: %s' % (tag, e))
            continue

        safe = re.sub(r'[^A-Za-z0-9]+', '_', '%s_%s' % (gp_team, opp_name))
        out_path = os.path.join(
            OUTPUT_DIR, 'nextgame_%s_%s.png' % (safe, m_date.isoformat()))
        img.save(out_path, 'PNG', quality=95)
        print('  saved %s' % out_path)
        generated += 1

        caption = build_caption(gp_label, opp_name, gp_side, match_type,
                                league, round_num, d_str, loc, time_str)
        feed_id = story_id = None
        try:
            feed_url, feed_id = upload_public_image(user_drive, out_path, POST_UPLOAD_FOLDER_ID)
            print('  feed url: %s' % feed_url)
            story_path = make_story_version(out_path)
            story_url, story_id = upload_public_image(user_drive, story_path, POST_UPLOAD_FOLDER_ID)
            print('  story url: %s' % story_url)
            post_to_meta(out_path, caption, image_url=feed_url, story_url=story_url)
        except Exception as e:
            errors.append('%s: Meta posting failed: %s' % (tag, e))
        finally:
            for fid in (feed_id, story_id):
                if fid:
                    try:
                        user_drive.files().delete(fileId=fid).execute()
                        print('  deleted temp Drive file %s' % fid)
                    except Exception as e:
                        print('  could not delete %s: %s' % (fid, e))

    print('Done. Generated %d image(s), %d error(s).' % (generated, len(errors)))
    send_error_email(errors)


if __name__ == '__main__':
    run_next_game_generator()
