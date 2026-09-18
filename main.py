import sys
import os
import sqlite3
import traceback

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import time
import subprocess
import threading
import datetime
import urllib.request
from tkinter import filedialog, StringVar
import customtkinter as ctk
from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageFilter

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import config
import database as db
import helpers

active_sessions = {}

selected_game_id = None
slot_occupants = {}
add_game_window_open = False
active_modal_close_fn = None  # set to the currently-open settings/add-game modal's close
                               # function while it's open; letting sidebar navigation (or
                               # opening a different modal) close it automatically instead of
                               # silently doing nothing until the user hits Cancel themselves
current_view = "carousel"  # "carousel", "library", or "focus" (single-game page opened from
                            # the library, shown via the flame/logo icon in the sidebar)
focus_game_id = None  # which game the "focus" view is showing - separate from selected_game_id
                       # so opening/closing it never disturbs whatever the carousel had selected
library_frame = None
library_search_query = ""
library_search_entry_widget = None  # tracked so live search updates don't have to touch it
library_content_wrap = None  # persistent parent for the title/search box/grid while in library view
library_grid_holder = None  # persistent parent frame holding either the card grid or the "no games" message
library_card_container = None  # persistent frame holding the actual card buttons - cards are added/removed/repositioned in place, not torn down every keystroke
library_no_games_lbl = None  # persistent empty-state label, shown/hidden as needed
library_grid_cards = {}  # game_id -> {"btn": CTkButton, "normal_art": PIL RGBA, "fade_gen": int} for every card currently on screen
library_grid_generation = 0  # bumped whenever a new card is created; stamped onto that card's "fade_gen" so its fade-in can tell later (unrelated) keystrokes apart from itself being removed/recreated

visible_card_items = []
text_item_ids = []
text_base_positions = []
card_image_refs = {}
current_hero_pil = None
hero_bg_item = None
text_overlay_item = None
animation_in_progress = False
_hover_offsets = {}
_hover_targets = {}

sidebar_indicator_item = None
current_indicator_y = None  # real starting value is set just below, once get_sidebar_mid_ys()
                             # exists - keeping this as its own literal (it used to be
                             # "config.WINDOW_H * 0.42" here directly) is exactly what caused
                             # the indicator to render in the wrong spot on startup after the
                             # sidebar icons were moved: this number and the one inside
                             # get_sidebar_mid_ys() drifted apart the moment only one of them
                             # got edited.

# Hover state variables for main screen buttons
launch_hover_progress = 0.0
launch_hover_target = 0.0
cogwheel_angle = 0.0
cogwheel_hovered = False

FONT_FILE = helpers.get_font_path()

def ensure_font_exists():
    global FONT_FILE
    FONT_FILE = helpers.get_font_path()
    
    if not os.path.exists(FONT_FILE):
        alternatives = [
            helpers.resource_path(os.path.join("assets", "Fonts", "Cal Sans SemiBold.ttf")),
            helpers.resource_path(os.path.join("assets", "Fonts", "CalSans-SemiBold.ttf")),
            helpers.resource_path("CalSans-SemiBold.ttf"),
            "CalSans-SemiBold.ttf"
        ]
        for alt in alternatives:
            if os.path.exists(alt):
                FONT_FILE = alt
                break
        else:
            try:
                os.makedirs(os.path.dirname(FONT_FILE), exist_ok=True)
                url = "https://raw.githubusercontent.com/calcom/font/main/fonts/ttf/CalSans-SemiBold.ttf"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as response, open(FONT_FILE, 'wb') as out_file:
                    out_file.write(response.read())
            except Exception as e:
                helpers.log(f"[DEBUG] Font download failed: {e}")

    try:
        if os.path.exists(FONT_FILE):
            FR_PRIVATE = 0x10
            ctypes.windll.gdi32.AddFontResourceExW(str(FONT_FILE), FR_PRIVATE, 0)
            
            HWND_BROADCAST = 0xFFFF
            WM_FONTCHANGE = 0x001D
            ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_FONTCHANGE, 0, 0)
    except Exception as e:
        helpers.log(f"[DEBUG] Font GDI registration failed: {e}")

ensure_font_exists()

TEXT_REGION_X = config.CONTENT_X
TEXT_REGION_Y = int(config.WINDOW_H * 0.35)
TEXT_REGION_W = 600
TEXT_REGION_H = 200

def get_ctk_font(size, weight="normal"):
    font_family_candidates = ["Cal Sans SemiBold", "Cal Sans", "Segoe UI"]
    for family in font_family_candidates:
        try:
            return ctk.CTkFont(family=family, size=size, weight=weight)
        except Exception:
            continue
    return ctk.CTkFont(size=size, weight=weight)

def get_pil_font(size):
    font_candidates = [
        FONT_FILE,
        helpers.resource_path(os.path.join("assets", "Fonts", "Cal Sans SemiBold.ttf")),
        helpers.resource_path(os.path.join("assets", "Fonts", "CalSans-SemiBold.ttf")),
        helpers.resource_path(os.path.join("assets", "Fonts", "CalSans-SemiBold.otf")),
        helpers.resource_path(os.path.join("assets", "Fonts", "CalSans.ttf")),
        r"C:\Windows\Fonts\CalSans-SemiBold.ttf",
        r"C:\Windows\Fonts\CalSans-SemiBold.otf"
    ]
    for font in font_candidates:
        if os.path.exists(font):
            try:
                return ImageFont.truetype(font, size)
            except Exception:
                continue

    try:
        return ImageFont.truetype("Cal Sans", size)
    except Exception:
        pass

    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()

def get_sidebar_mid_ys():
    """Single source of truth for the 4 mid-sidebar icon y-positions (clock/recent,
    library, add-game, backup). Used to be a literal list copy-pasted in several places -
    that's the exact class of duplicated-source-of-truth bug that's bitten this app before
    (see the library-view teardown bug), so every place that needs these positions calls
    this instead of retyping the list."""
    base = config.WINDOW_H * 0.42 - 30
    return [base, base + 55, base + 110, base + 165]

current_indicator_y = get_sidebar_mid_ys()[0]  # real startup value - see the comment on the
                                                # placeholder declaration above for why this
                                                # can't just be a literal here too

def get_indicator_y_for_current_view():
    """Where the sidebar indicator should sit for whatever current_view is, once a modal
    closes and there's no modal-specific icon to line up with instead."""
    mid_ys = get_sidebar_mid_ys()
    if current_view == "carousel":
        return mid_ys[0]
    elif current_view == "library":
        return mid_ys[1]
    else:  # "focus"
        return 45

def render_backup_icon(size):
    """Draws a simple shield+checkmark glyph for the backup/restore sidebar icon, so this
    feature doesn't need a new icon asset file to exist yet. Swap this out for
    load_sidebar_icon(config.SIDEBAR_BACKUP_PATH, size) later if a real icon gets made for
    the Icons/ folder to match the other 5."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = h = size
    shield_pts = [
        (w * 0.50, h * 0.04), (w * 0.88, h * 0.20), (w * 0.88, h * 0.52),
        (w * 0.50, h * 0.97), (w * 0.12, h * 0.52), (w * 0.12, h * 0.20),
    ]
    d.polygon(shield_pts, fill=(255, 255, 255, 255))
    check_pts = [(w * 0.32, h * 0.48), (w * 0.45, h * 0.62), (w * 0.70, h * 0.32)]
    d.line(check_pts, fill=(18, 18, 22, 255), width=max(2, int(w * 0.09)), joint="curve")
    return ImageTk.PhotoImage(img)

def compute_dynamic_slot_x():
    positions = {}
    x = config.CONTENT_X
    for offset in config.CARD_OFFSETS:
        positions[offset] = x
        w, h = config.CENTER_CARD_SIZE if offset == 0 else config.SIDE_CARD_SIZE
        x += w + config.CAROUSEL_GAP
    return positions

def animate_lift(canvas, item, target, delay_ms=16):
    _hover_targets[item] = target

    def step():
        if _hover_targets.get(item) != target:
            return
        current = _hover_offsets.get(item, 0)
        remaining = target - current
        if abs(remaining) < 0.3:
            if remaining != 0:
                canvas.move(item, 0, -remaining)
                _hover_offsets[item] = target
            return
        move_amt = remaining / 3
        canvas.move(item, 0, -move_amt)
        _hover_offsets[item] = current + move_amt
        canvas.after(delay_ms, step)

    step()

def rounded_image(pil_img, w, h, radius=12, alpha=1.0, resample=Image.LANCZOS):
    w, h = max(1, int(w)), max(1, int(h))
    if pil_img is None:
        pil_img = Image.new("RGBA", (600, 900), (40, 40, 45, 255))
    else:
        pil_img = pil_img.convert("RGBA")
        
    src_w, src_h = pil_img.size
    scale = max(w / src_w, h / src_h)
    resized = pil_img.resize((max(w, int(src_w * scale)), max(h, int(src_h * scale))), resample)
    
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    cropped = resized.crop((left, top, left + w, top + h))
    
    if cropped.size != (w, h):
        cropped = cropped.resize((w, h), resample)

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    
    if alpha < 1.0:
        alpha_val = max(0.0, min(1.0, alpha))
        mask_data = [int(px * alpha_val) for px in mask.getdata()]
        mask.putdata(mask_data)

    cropped.putalpha(mask)
    return cropped

def draw_outer_selection_border(canvas, x, y, w, h, radius=12, border_thickness=4, gap=3, color="#fab301", alpha=1.0, tags=None):
    if alpha <= 0:
        return []

    out_x1 = x - gap - border_thickness
    out_y1 = y - gap - border_thickness
    out_x2 = x + w + gap + border_thickness
    out_y2 = y + h + gap + border_thickness
    out_w = max(1, int(out_x2 - out_x1))
    out_h = max(1, int(out_y2 - out_y1))

    overlay = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    a_val = int(255 * max(0.0, min(1.0, alpha)))
    
    outer_radius = radius + gap + border_thickness
    half_th = border_thickness / 2
    
    draw.rounded_rectangle(
        (half_th, half_th, out_w - 1 - half_th, out_h - 1 - half_th),
        radius=outer_radius,
        outline=(r, g, b, a_val),
        width=border_thickness
    )

    photo = ImageTk.PhotoImage(overlay)
    ref_key = f"border_{id(photo)}"
    card_image_refs[ref_key] = photo
    
    border_item = canvas.create_image(out_x1, out_y1, anchor="nw", image=photo, tags=tags)
    return border_item

LIB_CARD_RADIUS = 8
LIB_CARD_PAD = 8           # transparent margin baked into every card photo (reserves room for the glow / inter-card gap)
LIB_GLOW_THICKNESS = 2
LIB_SCROLLBAR_RESERVE = 24 # CTkScrollableFrame eats part of its declared width for its own scrollbar -
                            # card sizing must subtract this or the last column overflows/gets clipped
LIB_NUM_COLS = 7
LIB_CARD_W = 112
LIB_CARD_H = 168
LIB_FADE_STEPS = 8         # number of frames in the grid's fade-in animation
LIB_FADE_STEP_MS = 18      # delay between fade-in frames (~144ms total)
LIBRARY_BG_HEX = "#1a1a1e"
LIBRARY_BG_RGB = (26, 26, 30)
LIB_GLOW_COLOR = (250, 179, 1, 255)  # matches the carousel's gold selection border

def build_library_card_art(pil_img, card_w, card_h):
    """Builds the normal + hover RGBA art for a library grid card (as PIL images, not yet
    PhotoImages). Kept separate from make_library_card_photos so the fade-in animation can
    generate intermediate opacity frames from the same source art instead of redrawing."""
    art = rounded_image(pil_img, card_w, card_h, radius=LIB_CARD_RADIUS, resample=Image.LANCZOS)

    pad = LIB_CARD_PAD
    outer_w, outer_h = card_w + pad * 2, card_h + pad * 2

    normal_canvas = Image.new("RGBA", (outer_w, outer_h), (0, 0, 0, 0))
    normal_canvas.paste(art, (pad, pad), art)

    hover_canvas = normal_canvas.copy()
    draw = ImageDraw.Draw(hover_canvas)
    half_th = LIB_GLOW_THICKNESS / 2
    hover_radius = LIB_CARD_RADIUS + pad * 0.6
    draw.rounded_rectangle(
        (half_th, half_th, outer_w - 1 - half_th, outer_h - 1 - half_th),
        radius=hover_radius,
        outline=LIB_GLOW_COLOR,
        width=LIB_GLOW_THICKNESS
    )
    return normal_canvas, hover_canvas

def make_library_card_photos(pil_img, card_w, card_h):
    """Builds the normal + hover PhotoImages for a library grid card at full opacity.
    Both are rendered at the same outer pixel size (card art + LIB_CARD_PAD on
    all sides) so swapping between them on hover never resizes/jitters the
    button - only the gold glow ring fades in around the identical artwork."""
    normal_canvas, hover_canvas = build_library_card_art(pil_img, card_w, card_h)
    return ImageTk.PhotoImage(normal_canvas), ImageTk.PhotoImage(hover_canvas)

def make_faded_card_photo(rgba_canvas, alpha_fraction):
    """Returns a PhotoImage of rgba_canvas with every pixel's alpha scaled by
    alpha_fraction (0..1) - the building block for fading grid cards in smoothly instead
    of popping in instantly on every search keystroke."""
    alpha_fraction = max(0.0, min(1.0, alpha_fraction))
    if alpha_fraction >= 0.999:
        return ImageTk.PhotoImage(rgba_canvas)
    r, g, b, a = rgba_canvas.split()
    a = a.point(lambda px: int(px * alpha_fraction))
    faded = Image.merge("RGBA", (r, g, b, a))
    return ImageTk.PhotoImage(faded)

def render_text_block(game_data, alpha=1.0):
    w, h = TEXT_REGION_W, TEXT_REGION_H
    pad = 10
    img_w, img_h = w + pad * 2, h + pad * 2
    
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if not game_data:
        return img

    game_id, name, exe_path, save_path, last_played, total_minutes = game_data

    a_val = int(255 * max(0.0, min(1.0, alpha)))
    c_white = (255, 255, 255, a_val)
    c_gray = (160, 160, 165, a_val)

    f_title = get_pil_font(44)
    f_lbl = get_pil_font(11)
    f_val = get_pil_font(20)

    max_title_w = w - 10
    display_name = name
    while f_title.getlength(display_name) > max_title_w and len(display_name) > 3:
        display_name = display_name[:-4] + "..."

    content_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(content_layer)

    cdraw.text((0, 0), display_name, fill=c_white, font=f_title)
    cdraw.text((0, 75), "PLAY TIME", fill=c_gray, font=f_lbl)
    time_str = f"{total_minutes // 60}H {total_minutes % 60}M" if total_minutes >= 60 else f"{total_minutes} MIN"
    cdraw.text((0, 95), time_str, fill=c_white, font=f_val)

    cdraw.text((220, 75), "LAST PLAYED", fill=c_gray, font=f_lbl)
    last_str = helpers.time_ago(last_played).upper()
    cdraw.text((220, 95), last_str, fill=c_white, font=f_val)

    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if content_layer.split():
        alpha_channel = content_layer.split()[3]
        shadow.paste((0, 0, 0, int(120 * alpha)), (0, 0), alpha_channel)
        shadow = shadow.filter(ImageFilter.GaussianBlur(3))

    img.paste(shadow, (pad + 2, pad + 3), shadow)
    img.paste(content_layer, (pad, pad), content_layer)

    return img

def create_cogwheel_icon(size=26, color="white", angle=0):
    import math
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    center = size / 2
    radius = size * 0.32
    
    draw.ellipse([center - radius, center - radius, center + radius, center + radius], outline=color, width=3)
    inner_r = radius * 0.45
    draw.ellipse([center - inner_r, center - inner_r, center + inner_r, center + inner_r], fill=(0, 0, 0, 0), outline=color, width=2)
    
    for i in range(8):
        rad_angle = angle + i * (2 * math.pi / 8)
        x1 = center + (radius - 1) * math.cos(rad_angle)
        y1 = center + (radius - 1) * math.sin(rad_angle)
        x2 = center + (radius + 4) * math.cos(rad_angle)
        y2 = center + (radius + 4) * math.sin(rad_angle)
        draw.line([x1, y1, x2, y2], fill=color, width=3)
    return img

_hero_composite_cache = {}

def build_background_composite(hero_path, width, height, game=None, is_running=False):
    cache_key = (hero_path, width, height, add_game_window_open, current_view, game[0] if game else None, is_running)
    if cache_key in _hero_composite_cache:
        return _hero_composite_cache[cache_key]

    if hero_path and os.path.exists(hero_path):
        try:
            img = Image.open(hero_path).convert("RGB")
            src_w, src_h = img.size
            scale = max(width / src_w, height / src_h)
            img = img.resize((int(src_w * scale), int(src_h * scale)), Image.LANCZOS)
            left = (img.width - width) // 2
            top = (img.height - height) // 2
            img = img.crop((left, top, left + width, top + height))
        except Exception:
            img = Image.new("RGB", (width, height), (18, 18, 22))
    else:
        img = Image.new("RGB", (width, height), (18, 18, 22))

    gradient = Image.new("L", (width, height), 0)
    gpix = gradient.load()
    for x in range(width):
        frac = min(1.0, x / (width * 0.62))
        left_alpha = int(235 * (1 - frac) + 40)
        for y in range(height):
            gpix[x, y] = left_alpha

    for y in range(height):
        bottom_frac = max(0.0, (y - height * 0.62) / (height * 0.38))
        bottom_add = int(190 * bottom_frac)
        top_frac = max(0.0, (height * 0.08 - y) / (height * 0.08))
        top_add = int(70 * top_frac)
        for x in range(width):
            gpix[x, y] = min(255, gpix[x, y] + bottom_add + top_add)
            right_frac = max(0.0, (x - width * 0.94) / (width * 0.06))
            gpix[x, y] = min(255, gpix[x, y] + int(60 * right_frac))

    black = Image.new("RGB", (width, height), (5, 5, 8))
    result = Image.composite(black, img, gradient).convert("RGB")

    # If in library view, darken background slightly for contrast
    if current_view == "library":
        darken_overlay = Image.new("RGB", (width, height), (15, 15, 20))
        result = Image.blend(result, darken_overlay, 0.4)

    _hero_composite_cache[cache_key] = result
    return result

def draw_launch_and_settings_buttons(canvas, width, height, game, is_running, hover_progress, cog_angle):
    if not game or current_view not in ("carousel", "focus"):
        return

    btn_w, btn_h = 190, 56
    btn_x = width - 240
    lift_offset = int(6 * hover_progress)
    btn_y = height - 100 - lift_offset
    
    r_base, g_base, b_base = 250, 179, 1
    r_target, g_target, b_target = 223, 156, 1
    
    if is_running:
        r_val, g_val, b_val = 85, 85, 85
        text_str = "RUNNING"
    else:
        r_val = int(r_base + (r_target - r_base) * hover_progress)
        g_val = int(g_base + (g_target - g_base) * hover_progress)
        b_val = int(b_base + (b_target - b_base) * hover_progress)
        text_str = "LAUNCH"
    
    btn_img = Image.new("RGBA", (btn_w, btn_h), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(btn_img)
    pdraw.rounded_rectangle((0, 0, btn_w - 1, btn_h - 1), radius=btn_h / 2, fill=(r_val, g_val, b_val, 255))
    
    f = get_pil_font(25)
    bbox = pdraw.textbbox((0, 0), text_str, font=f)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (btn_w - tw) / 2 - bbox[0]
    ty = (btn_h - th) / 2 - bbox[1]
    pdraw.text((tx, ty), text_str, fill="white", font=f)
    
    btn_photo = ImageTk.PhotoImage(btn_img)
    card_image_refs["_dyn_launch_btn"] = btn_photo
    canvas.create_image(btn_x, btn_y, anchor="nw", image=btn_photo, tags="dynamic_ui")

    settings_w, settings_h = 56, 56
    settings_x = width - 310
    settings_y = height - 100

    settings_img = Image.new("RGBA", (settings_w, settings_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(settings_img)
    sdraw.rounded_rectangle((0, 0, settings_w - 1, settings_h - 1), radius=settings_h / 2, fill=(43, 43, 43, 255))

    gear_icon = create_cogwheel_icon(size=26, color="white", angle=cog_angle)
    gx = (settings_w - gear_icon.width) // 2
    gy = (settings_h - gear_icon.height) // 2
    settings_img.paste(gear_icon, (gx, gy), gear_icon)

    settings_photo = ImageTk.PhotoImage(settings_img)
    card_image_refs["_dyn_settings_btn"] = settings_photo
    canvas.create_image(settings_x, settings_y, anchor="nw", image=settings_photo, tags="dynamic_ui")

    if not add_game_window_open and not is_running:
        game_id, name, exe_path, save_path, _, _ = game
        btn_hit = canvas.create_rectangle(btn_x, btn_y, btn_x + btn_w, btn_y + btn_h, fill="", outline="", tags="dynamic_ui")
        canvas.tag_bind(btn_hit, "<Button-1>", lambda e: launch_game(game_id, name, exe_path, save_path))
        canvas.tag_bind(btn_hit, "<Enter>", lambda e: [setattr(sys.modules[__name__], 'launch_hover_target', 1.0)])
        canvas.tag_bind(btn_hit, "<Leave>", lambda e: [setattr(sys.modules[__name__], 'launch_hover_target', 0.0)])

        settings_hit = canvas.create_rectangle(settings_x, settings_y, settings_x + settings_w, settings_y + settings_h, fill="", outline="", tags="dynamic_ui")
        canvas.tag_bind(settings_hit, "<Button-1>", lambda e: open_game_settings_form(game))
        canvas.tag_bind(settings_hit, "<Enter>", lambda e: [setattr(sys.modules[__name__], 'cogwheel_hovered', True)])
        canvas.tag_bind(settings_hit, "<Leave>", lambda e: [setattr(sys.modules[__name__], 'cogwheel_hovered', False)])

def _recompute_slot_occupants(center_idx, all_games):
    global slot_occupants
    slot_occupants = {}
    if not all_games:
        return
    n = len(all_games)
    for offset in config.CARD_OFFSETS:
        idx = (center_idx + offset) % n
        slot_occupants[offset] = all_games[idx][0]

def select_game(game_id):
    global selected_game_id, animation_in_progress
    if game_id == selected_game_id or animation_in_progress or add_game_window_open:
        return
    if selected_game_id is None:
        selected_game_id = game_id
        redraw_all()
        return

    animate_transition(game_id)

def animate_transition(new_game_id):
    global selected_game_id, animation_in_progress, current_view
    if current_view != "carousel":
        selected_game_id = new_game_id
        redraw_all()
        return

    all_games = db.get_all_games()
    if not all_games:
        return
    
    ids = [g[0] for g in all_games]
    old_center_idx = ids.index(selected_game_id) if selected_game_id in ids else 0
    new_center_idx = ids.index(new_game_id)
    
    n = len(all_games)
    raw_shift = new_center_idx - old_center_idx
    shift = (raw_shift + n // 2) % n - n // 2

    old_game = next((g for g in all_games if g[0] == selected_game_id), None)
    new_game = next((g for g in all_games if g[0] == new_game_id), None)
    slot_x = compute_dynamic_slot_x()

    exit_left = config.CONTENT_X - 220
    exit_right = slot_x[2] + 220

    animation_in_progress = True
    selected_game_id = new_game_id

    old_slot_occupants = dict(slot_occupants)
    _recompute_slot_occupants(new_center_idx, all_games)

    for item_id in visible_card_items:
        main_canvas.delete(item_id)
    visible_card_items.clear()

    raw_tracks = []
    seen_ids = set()

    for offset in config.CARD_OFFSETS:
        g_id = old_slot_occupants.get(offset)
        if not g_id:
            continue
        g = next((x for x in all_games if x[0] == g_id), None)
        if not g:
            continue
        
        new_offset = offset - shift
        start_x = slot_x[offset]

        if offset == 0:
            target_x = slot_x.get(new_offset, exit_left if new_offset < -2 else exit_right)
            raw_tracks.append(_make_card_track(g, start_x, target_x, start_size=config.CENTER_CARD_SIZE, end_size=config.SIDE_CARD_SIZE, start_border=True, end_border=False, fade="none"))
        elif new_offset == 0:
            target_x = slot_x[0]
            raw_tracks.append(_make_card_track(g, start_x, target_x, start_size=config.SIDE_CARD_SIZE, end_size=config.CENTER_CARD_SIZE, start_border=False, end_border=True, fade="none"))
        elif new_offset in slot_x:
            raw_tracks.append(_make_card_track(g, start_x, slot_x[new_offset], start_size=config.SIDE_CARD_SIZE, end_size=config.SIDE_CARD_SIZE, start_border=False, end_border=False, fade="none"))
        else:
            target_x = exit_left if new_offset < -2 else exit_right
            raw_tracks.append(_make_card_track(g, start_x, target_x, start_size=config.SIDE_CARD_SIZE, end_size=config.SIDE_CARD_SIZE, start_border=False, end_border=False, fade="out"))
        seen_ids.add(g[0])

    for offset in config.CARD_OFFSETS:
        g_id = slot_occupants.get(offset)
        if not g_id or g_id in seen_ids:
            continue
        g = next((x for x in all_games if x[0] == g_id), None)
        if not g:
            continue
        
        old_offset = offset + shift
        start_x = exit_left if old_offset < -2 else exit_right
        raw_tracks.append(_make_card_track(g, start_x, slot_x[offset], start_size=config.SIDE_CARD_SIZE, end_size=config.SIDE_CARD_SIZE, start_border=False, end_border=False, fade="in"))

    new_hero_path = helpers.get_hero_art_path(new_game[0], new_game[1]) if new_game else None
    is_running_new = bool(active_sessions.get(new_game[0])) if new_game else False
    new_hero_pil = build_background_composite(new_hero_path, config.WINDOW_W, config.WINDOW_H, game=new_game, is_running=is_running_new)
    old_hero_pil = current_hero_pil

    TOTAL_STEPS = 10
    bg_cache = []
    text_cache = []
    precomputed_tracks = []

    for s in range(TOTAL_STEPS + 1):
        t = s / TOTAL_STEPS
        eased = 1 - (1 - t) ** 3

        if old_hero_pil is not None and new_hero_pil is not None:
            blended = Image.blend(old_hero_pil, new_hero_pil, eased)
        else:
            blended = new_hero_pil or old_hero_pil
        bg_cache.append(ImageTk.PhotoImage(blended))

        if t < 0.5:
            text_alpha = 1.0 - (t * 2.0)
            active_game_data = old_game
        else:
            text_alpha = (t - 0.5) * 2.0
            active_game_data = new_game
        txt_pil = render_text_block(active_game_data, alpha=text_alpha)
        text_cache.append(ImageTk.PhotoImage(txt_pil))

    for track in raw_tracks:
        track_frames = []
        for s in range(TOTAL_STEPS + 1):
            t = s / TOTAL_STEPS
            eased = 1 - (1 - t) ** 3
            x = track["start_x"] + (track["target_x"] - track["start_x"]) * eased
            w = int(track["start_w"] + (track["end_w"] - track["start_w"]) * eased) if track["needs_scale"] else track["start_w"]
            h = int(track["start_h"] + (track["end_h"] - track["start_h"]) * eased) if track["needs_scale"] else track["start_h"]
            y_top = config.CARD_ROW_BOTTOM_Y - h

            alpha = 1.0
            if track["fade"] == "out":
                alpha = 1.0 - t
            elif track["fade"] == "in":
                alpha = t

            if track["needs_scale"] or alpha < 1.0:
                img = rounded_image(track["source_pil"], w, h, radius=12, alpha=alpha, resample=Image.BILINEAR)
            else:
                img = track["source_pil_resized"]

            if img is None:
                img = Image.new("RGBA", (w, h), (40, 40, 45, 255))

            photo = ImageTk.PhotoImage(img)

            border_alpha = 0.0
            if track["start_border"] or track["end_border"]:
                border_alpha = (1 - eased) if (track["start_border"] and not track["end_border"]) else (eased if (track["end_border"] and not track["start_border"]) else 1.0)

            track_frames.append((x, y_top, w, h, photo, border_alpha))
        
        init_frame = track_frames[0]
        init_photo = init_frame[4]
        canvas_item = main_canvas.create_image(init_frame[0], init_frame[1], anchor="nw", image=init_photo)
        card_image_refs[f"anim_init_{canvas_item}"] = init_photo

        border_item = None
        if init_frame[5] > 0:
            border_item = draw_outer_selection_border(main_canvas, init_frame[0], init_frame[1], init_frame[2], init_frame[3], radius=12, alpha=init_frame[5])

        precomputed_tracks.append({
            "item": canvas_item,
            "border_item": border_item,
            "frames": track_frames
        })

    _run_animation_frame(precomputed_tracks, bg_cache, text_cache, new_hero_pil, TOTAL_STEPS, 0)

def _make_card_track(game, start_x, target_x, start_size, end_size, start_border, end_border, fade):
    g_id, g_name = game[0], game[1]
    cover_path = helpers.get_cover_art_path(g_id, g_name)
    try:
        if cover_path and os.path.exists(cover_path):
            source_pil = Image.open(cover_path)
        else:
            source_pil = Image.new("RGB", (600, 900), (40, 40, 45))
    except Exception:
        source_pil = Image.new("RGB", (600, 900), (40, 40, 45))

    w, h = start_size
    card_img = rounded_image(source_pil, w, h, radius=12, resample=Image.LANCZOS)
    photo = ImageTk.PhotoImage(card_img) if card_img else None
    card_image_refs[f"anim_start_{g_id}"] = photo
    y_top = config.CARD_ROW_BOTTOM_Y - h

    needs_scale = (start_size != end_size)
    source_pil_resized = rounded_image(source_pil, end_size[0], end_size[1], radius=12, resample=Image.LANCZOS) if not needs_scale else card_img

    return {
        "source_pil": source_pil,
        "source_pil_resized": source_pil_resized,
        "start_x": start_x, "target_x": target_x,
        "start_w": start_size[0], "start_h": start_size[1],
        "end_w": end_size[0], "end_h": end_size[1],
        "start_border": start_border, "end_border": end_border,
        "needs_scale": needs_scale,
        "fade": fade,
    }

def _run_animation_frame(precomputed_tracks, bg_cache, text_cache, final_hero_pil, total_steps, step):
    global current_hero_pil, animation_in_progress, text_overlay_item

    main_canvas.itemconfig(hero_bg_item, image=bg_cache[step])
    main_canvas.itemconfig(text_overlay_item, image=text_cache[step])
    current_hero_pil = final_hero_pil

    for track in precomputed_tracks:
        x, y_top, w, h, photo, border_alpha = track["frames"][step]
        ref_key = f"t_{track['item']}_{step}"
        card_image_refs[ref_key] = photo

        main_canvas.itemconfig(track["item"], image=photo)
        main_canvas.coords(track["item"], x, y_top)

        if track["border_item"]:
            main_canvas.delete(track["border_item"])
            track["border_item"] = None

        if border_alpha > 0:
            track["border_item"] = draw_outer_selection_border(main_canvas, x, y_top, w, h, radius=12, alpha=border_alpha)

    if step < total_steps:
        app.after(16, lambda: _run_animation_frame(precomputed_tracks, bg_cache, text_cache, final_hero_pil, total_steps, step + 1))
    else:
        for track in precomputed_tracks:
            main_canvas.delete(track["item"])
            if track["border_item"]:
                main_canvas.delete(track["border_item"])
        animation_in_progress = False
        redraw_all()

def animate_launch_hover():
    global launch_hover_progress, launch_hover_target, cogwheel_angle, cogwheel_hovered
    diff = launch_hover_target - launch_hover_progress
    updated = False

    if abs(diff) > 0.01:
        launch_hover_progress += diff * 0.25
        updated = True
    elif launch_hover_progress != launch_hover_target:
        launch_hover_progress = launch_hover_target
        updated = True

    if cogwheel_hovered:
        cogwheel_angle += 0.08
        updated = True

    if updated and current_view in ("carousel", "focus"):
        main_canvas.delete("dynamic_ui")
        games_by_id = {g[0]: g for g in db.get_all_games()}
        game_id_to_show = focus_game_id if current_view == "focus" else selected_game_id
        game = games_by_id.get(game_id_to_show)
        is_running = bool(active_sessions.get(game[0])) if game else False
        draw_launch_and_settings_buttons(main_canvas, config.WINDOW_W, config.WINDOW_H, game, is_running, launch_hover_progress, cogwheel_angle)

    app.after(16, animate_launch_hover)

def launch_game(game_id, name, exe_path, save_path):
    if not exe_path or not os.path.exists(exe_path) or add_game_window_open:
        return

    if active_sessions.get(game_id):
        return

    active_sessions[game_id] = True
    refresh_all()

    def run():
        game_folder = os.path.dirname(exe_path)
        start_time = datetime.datetime.now()

        helpers.restore_if_missing(game_id, name, save_path)

        try:
            process = subprocess.Popen(exe_path, cwd=game_folder)
        except Exception:
            active_sessions.pop(game_id, None)
            app.after(0, refresh_all)
            return

        time.sleep(2)
        if process.poll() is not None:
            active_sessions.pop(game_id, None)
            app.after(0, refresh_all)
            return

        process.wait()
        minutes_played = max(1, int((datetime.datetime.now() - start_time).total_seconds() // 60))

        helpers.backup_save(game_id, name, save_path)
        db.record_session(game_id, minutes_played)

        def finish():
            active_sessions.pop(game_id, None)
            refresh_all()
        app.after(0, finish)

    threading.Thread(target=run, daemon=True).start()

def animate_sidebar_indicator(target_y):
    global current_indicator_y, sidebar_indicator_item
    start_y = current_indicator_y
    if abs(start_y - target_y) < 0.5:
        current_indicator_y = target_y
        return

    steps = 6
    indicator_height = 36
    sidebar_w = config.SIDEBAR_W

    def step_anim(s=0):
        global current_indicator_y, sidebar_indicator_item
        if s > steps:
            current_indicator_y = target_y
            return
        t = s / steps
        eased = 1 - (1 - t) ** 3
        cur_y = start_y + (target_y - start_y) * eased
        current_indicator_y = cur_y

        if sidebar_indicator_item:
            main_canvas.delete(sidebar_indicator_item)

        # Drawn 2px inside the sidebar (not exactly on the sidebar_w boundary) so its
        # stroke never straddles into the content area - in library view, the library
        # panel/frame starts exactly at sidebar_w and sits in front of the canvas there,
        # which was clipping the right half of this line and making it look thinner than
        # in carousel view. Fully inside the sidebar, it can never get covered like that.
        indicator_x = sidebar_w - 2
        sidebar_indicator_item = main_canvas.create_line(
            indicator_x, cur_y - indicator_height / 2,
            indicator_x, cur_y + indicator_height / 2,
            fill="#ffffff", width=3
        )
        app.after(16, lambda: step_anim(s + 1))

    step_anim(0)

def close_active_modal_if_any():
    """If a settings/add-game modal is currently open, closes it (with its normal fade-out
    animation) so the caller can then navigate or open a different modal immediately,
    instead of the click being silently swallowed until the user hits Cancel themselves."""
    global active_modal_close_fn
    if active_modal_close_fn is not None:
        fn = active_modal_close_fn
        active_modal_close_fn = None
        fn()

def switch_to_carousel_view():
    global current_view
    # Also proceed if a modal (e.g. the Add Game form) is open, even when current_view is
    # already "carousel" - opening a modal never changes current_view, so without this the
    # guard below would return early and silently skip closing the modal whenever it was
    # opened from the carousel view (the exact bug: recent-games click doing nothing while
    # Add Game was open, since "library" happened to not match and worked "by accident").
    if current_view == "carousel" and active_modal_close_fn is None:
        return
    close_active_modal_if_any()
    current_view = "carousel"
    # library_frame (and all its tracking globals - library_grid_cards etc.) get torn down
    # and reset by redraw_all() itself now, so this function doesn't duplicate that logic.
    # It used to destroy library_frame directly here, which left the newer tracking dicts
    # (library_grid_cards, library_card_container, ...) pointing at now-dead widgets, since
    # redraw_all()'s own teardown only runs when library_frame is still non-None - crashing
    # the next time the library view was opened and tried to reuse those stale references.
    # Indicator lines up with the clock/"recent" icon (not the logo) since carousel/home is
    # the recent-games page.
    animate_sidebar_indicator(get_sidebar_mid_ys()[0])
    redraw_all()

def switch_to_library_view():
    global current_view
    # Same fix as switch_to_carousel_view() above: also proceed if a modal is open, even
    # when current_view already equals "library" (e.g. Add Game opened while already in
    # the library view), or the modal would never get closed.
    if current_view == "library" and active_modal_close_fn is None:
        return
    close_active_modal_if_any()
    current_view = "library"
    animate_sidebar_indicator(get_sidebar_mid_ys()[1])
    redraw_all()

def open_game_focus_view(game_id):
    """Opens the single-game page (flame icon) for one specific game, clicked from the
    library grid. Deliberately separate from select_game()/switch_to_carousel_view(): this
    is its own view, not "select this game in the carousel then jump to the carousel" -
    focus_game_id is tracked independently of selected_game_id so it never disturbs whatever
    game was centered in the actual carousel."""
    global current_view, focus_game_id
    close_active_modal_if_any()
    focus_game_id = game_id
    current_view = "focus"
    animate_sidebar_indicator(45)  # logo_y - lines the indicator up with the flame icon
    redraw_all()

def handle_flame_click():
    """The flame icon has no navigation action of its own - clicking it does nothing unless
    the focus page (opened by clicking a game in the library) is already open, in which case
    it closes that page and goes back to the library. There's no way to open the focus page
    by clicking the flame directly; it only opens from a library card click."""
    global current_view, focus_game_id
    if current_view != "focus":
        return
    focus_game_id = None
    current_view = "library"
    animate_sidebar_indicator(get_sidebar_mid_ys()[1])
    redraw_all()

def submit_library_search(text):
    global library_search_query
    library_search_query = text.strip()
    render_library_grid()

def render_library_grid():
    """Filters the library grid by the current search query and updates it incrementally:
    a card that still matches after another letter is typed keeps its existing widget and
    is only repositioned (no re-fade, no flicker); cards that stop matching are removed;
    only genuinely new matches fade in from transparent. This is what runs on every
    keystroke in the search box - it never touches main_canvas, library_frame, the title,
    or the search entry itself."""
    global library_grid_holder, library_card_container, library_no_games_lbl
    global library_grid_cards, library_grid_generation

    if library_content_wrap is None:
        return

    all_games = db.get_all_games()
    q = library_search_query.strip().lower()
    display_games = [g for g in all_games if q in g[1].lower()] if q else all_games
    display_by_id = {g[0]: g for g in display_games}

    if library_grid_holder is None:
        library_grid_holder = ctk.CTkFrame(library_content_wrap, fg_color="transparent")
        library_grid_holder.pack()
        library_card_container = None
        library_no_games_lbl = None
        library_grid_cards = {}

    empty_msg = "No games match your search." if q else "No games in your library yet. Use + Add Game on the sidebar to get started!"

    if not display_games:
        # Nothing matches - drop any existing card grid, show the empty-state message.
        if library_card_container is not None:
            library_card_container.destroy()
            library_card_container = None
            library_grid_cards = {}
        if library_no_games_lbl is None:
            library_no_games_lbl = ctk.CTkLabel(
                library_grid_holder, text=empty_msg, font=get_ctk_font(14), text_color="#888899"
            )
            library_no_games_lbl.pack(padx=20, pady=40)
        else:
            library_no_games_lbl.configure(text=empty_msg)
        return

    if library_no_games_lbl is not None:
        library_no_games_lbl.destroy()
        library_no_games_lbl = None

    if library_card_container is None:
        library_card_container = ctk.CTkFrame(library_grid_holder, fg_color="transparent")
        library_card_container.pack(pady=(0, 0))
        library_grid_cards = {}

    pad = LIB_CARD_PAD
    num_cols = LIB_NUM_COLS
    card_w = LIB_CARD_W
    card_h = LIB_CARD_H

    # Drop cards for games that no longer match - everything else on screen is left alone.
    for gid in list(library_grid_cards.keys()):
        if gid not in display_by_id:
            library_grid_cards[gid]["btn"].destroy()
            del library_grid_cards[gid]
            card_image_refs.pop(f"lib_card_{gid}_hover", None)
            card_image_refs.pop(f"lib_card_{gid}_fade_current", None)

    library_grid_generation += 1
    my_generation = library_grid_generation
    fade_targets = []  # only newly-created cards go through the fade-in below

    for idx, g in enumerate(display_games):
        g_id, g_name, g_exe, g_save, g_last, g_total = g
        r = idx // num_cols
        c = idx % num_cols

        if g_id in library_grid_cards:
            # Already on screen and still matching - just reposition it, don't touch its image.
            library_grid_cards[g_id]["btn"].grid(row=r, column=c, padx=0, pady=0, sticky="n")
            continue

        cover_path = helpers.get_cover_art_path(g_id, g_name)
        try:
            if cover_path and os.path.exists(cover_path):
                pil_img = Image.open(cover_path)
            else:
                pil_img = Image.new("RGB", (600, 900), (40, 40, 45))
        except Exception:
            pil_img = Image.new("RGB", (600, 900), (40, 40, 45))

        normal_art, hover_art = build_library_card_art(pil_img, card_w, card_h)
        hover_photo = ImageTk.PhotoImage(hover_art)
        start_photo = make_faded_card_photo(normal_art, 0.0)
        card_image_refs[f"lib_card_{g_id}_hover"] = hover_photo
        card_image_refs[f"lib_card_{g_id}_fade_current"] = start_photo

        game_card_btn = ctk.CTkButton(
            library_card_container,
            image=start_photo,
            text="",
            width=card_w + pad * 2,
            height=card_h + pad * 2,
            fg_color="transparent",
            hover=False,
            corner_radius=0,
            border_width=0,
            cursor="hand2",
            command=lambda gid=g_id: open_game_focus_view(gid)
        )
        game_card_btn.grid(row=r, column=c, padx=0, pady=0, sticky="n")

        def on_lib_card_enter(e, btn=game_card_btn, photo=hover_photo):
            btn.configure(image=photo)
        def on_lib_card_leave(e, btn=game_card_btn, gid=g_id):
            btn.configure(image=card_image_refs.get(f"lib_card_{gid}_fade_current"))
        game_card_btn.bind("<Enter>", on_lib_card_enter)
        game_card_btn.bind("<Leave>", on_lib_card_leave)

        library_grid_cards[g_id] = {"btn": game_card_btn, "normal_art": normal_art, "fade_gen": my_generation}
        fade_targets.append((game_card_btn, normal_art, g_id))

    if fade_targets:
        _step_library_grid_fade(fade_targets, 1, my_generation)

def _step_library_grid_fade(fade_targets, step, batch_gen):
    """One frame of a fade-in for a specific batch of newly-created cards. Each card's
    dict entry carries the generation it was created with ("fade_gen"); if that card was
    removed (or removed-and-recreated) since this batch started, it's dropped from the
    animation here rather than the whole batch being cancelled - so an unrelated keystroke
    elsewhere in the grid never truncates a card's fade that's still legitimately running."""
    fraction = step / LIB_FADE_STEPS
    still_active = []
    for btn, normal_art, g_id in fade_targets:
        entry = library_grid_cards.get(g_id)
        if entry is None or entry.get("fade_gen") != batch_gen:
            continue
        photo = make_faded_card_photo(normal_art, fraction)
        card_image_refs[f"lib_card_{g_id}_fade_current"] = photo
        try:
            btn.configure(image=photo)
        except Exception:
            continue
        still_active.append((btn, normal_art, g_id))

    if step < LIB_FADE_STEPS and still_active:
        app.after(LIB_FADE_STEP_MS, lambda: _step_library_grid_fade(still_active, step + 1, batch_gen))

def open_game_settings_form(game):
    global add_game_window_open
    close_active_modal_if_any()  # if a modal is already open, swap it out instead of ignoring the click
    add_game_window_open = True
    
    game_id, name, exe_path, save_path, last_played, total_minutes = game

    redraw_all()

    form = ctk.CTkCTkToplevel(app) if hasattr(ctk, "CTkCTkToplevel") else ctk.CTkToplevel(app)
    form.title(f"Settings - {name}")
    form.geometry("593x445")
    form.resizable(False, True)
    # No form.grab_set() here on purpose - a grab blocks input to the whole app (including
    # sidebar clicks), which is why switching pages used to require hitting Cancel first.
    # The main-content actions that shouldn't fire while a modal is open (launching a game,
    # picking a different card, etc.) already separately check add_game_window_open.
    form.overrideredirect(True)
    
    form.attributes("-alpha", 0.0)

    form.update_idletasks()
    target_w, target_h = 593, 445
    target_x = app.winfo_x() + (app.winfo_width() - target_w) // 2
    target_y = app.winfo_y() + (app.winfo_height() - target_h) // 2
    form.geometry(f"593x445+{target_x}+{target_y}")

    form.configure(fg_color="#585858")

    def apply_form_rounded_corners(radius=20):
        try:
            form.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(form.winfo_id())
            
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = 2
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(ctypes.c_int(DWMWCP_ROUND)),
                ctypes.sizeof(ctypes.c_int)
            )
            
            region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, 594, 400, radius, radius)
            ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
        except Exception:
            pass

    form.after(30, lambda: apply_form_rounded_corners(20))

    pop_steps = 12
    def run_pop_in(step=0):
        if step > pop_steps:
            form.attributes("-alpha", 1.0)
            return
        t = step / pop_steps
        eased = 1 - pow(1 - t, 3)
        form.attributes("-alpha", eased)
        app.after(16, lambda: run_pop_in(step + 1))

    form.after(20, lambda: run_pop_in(0))

    def close_modal_with_animation(on_complete=None):
        def run_pop_out(step=0):
            if step > pop_steps:
                if on_complete:
                    on_complete()
                return
            t = step / pop_steps
            eased = pow(1 - t, 3)
            form.attributes("-alpha", max(0.0, eased))
            form.after(16, lambda: run_pop_out(step + 1))
        run_pop_out(0)

        global add_game_window_open, active_modal_close_fn
        add_game_window_open = False
        active_modal_close_fn = None
        # Indicator goes back to whichever icon matches the view underneath the modal -
        # carousel/home lines up with the clock icon, library with the library icon, and
        # focus (the single-game page opened from the library) with the flame/logo icon.
        animate_sidebar_indicator(get_indicator_y_for_current_view())
        redraw_all()

    def on_form_close():
        close_modal_with_animation(on_complete=lambda: form.destroy())

    global active_modal_close_fn
    active_modal_close_fn = on_form_close  # lets sidebar navigation / opening another modal close this one automatically

    modal_frame = ctk.CTkFrame(form, fg_color="#585858", corner_radius=20, border_width=0)
    modal_frame.pack(fill="both", expand=True, padx=0, pady=0)

    def start_modal_drag(event):
        form._drag_start = (event.x, event.y)

    def do_modal_drag(event):
        if hasattr(form, "_drag_start"):
            dx = event.x - form._drag_start[0]
            dy = event.y - form._drag_start[1]
            form.geometry(f"+{form.winfo_x() + dx}+{form.winfo_y() + dy}")

    modal_frame.bind("<ButtonPress-1>", start_modal_drag)
    modal_frame.bind("<B1-Motion>", do_modal_drag)

    title_lbl = ctk.CTkLabel(modal_frame, text=f"Settings: {name}", font=get_ctk_font(20), text_color="#ffffff")
    title_lbl.place(x=593//2, y=28, anchor="center")

    delete_btn = ctk.CTkButton(
        modal_frame,
        text="Delete Game",
        width=90,
        height=26,
        font=get_ctk_font(11),
        fg_color="transparent",
        hover_color="#6e3535",
        text_color="#e06666",
        corner_radius=13,
        border_width=1,
        border_color="#e06666",
        command=lambda: trigger_delete_confirm()
    )
    delete_btn.place(x=22, y=350, anchor="w")

    name_lbl = ctk.CTkLabel(modal_frame, text="Game Name", font=get_ctk_font(14), text_color="#ffffff")
    name_lbl.place(x=593//2, y=66, anchor="center")

    s1_left = (593 - (315 + 12 + 130)) // 2

    name_entry = ctk.CTkEntry(modal_frame, width=315, height=35, fg_color="#d9d9d9", text_color="#121216", border_width=0, corner_radius=17.5, font=get_ctk_font(11))
    name_entry.place(x=130, y=80, anchor="nw")
    name_entry.insert(0, name)

    dir_lbl = ctk.CTkLabel(modal_frame, text="Game Directory / Executable", font=get_ctk_font(14), text_color="#ffffff")
    dir_lbl.place(x=593//2, y=134, anchor="center")

    exe_entry = ctk.CTkEntry(modal_frame, width=315, height=35, fg_color="#d9d9d9", text_color="#121216", border_width=0, corner_radius=17.5, font=get_ctk_font(11))
    exe_entry.place(x=s1_left, y=154, anchor="nw")
    exe_entry.insert(0, exe_path)

    def browse_exe():
        path = filedialog.askopenfilename(filetypes=[("Executable files", "*.exe"), ("All files", "*.*")])
        if path:
            exe_entry.delete(0, "end")
            exe_entry.insert(0, path)

    browse_btn = ctk.CTkButton(
        modal_frame,
        text="Change Directory",
        width=130,
        height=35,
        font=get_ctk_font(12),
        fg_color="#2b2b2b",
        hover_color="#3b3b3b",
        text_color="#ffffff",
        corner_radius=17.5,
        command=browse_exe
    )
    browse_btn.place(x=s1_left + 315 + 12, y=154, anchor="nw")

    save_lbl = ctk.CTkLabel(modal_frame, text="Save File Path (optional)", font=get_ctk_font(14), text_color="#ffffff")
    save_lbl.place(x=593//2, y=210, anchor="center")

    s2_left = (593 - (150 * 3 + 24)) // 2

    save_entry = ctk.CTkEntry(modal_frame, width=150, height=35, fg_color="#d9d9d9", text_color="#121216", border_width=0, corner_radius=17.5, font=get_ctk_font(11))
    save_entry.place(x=s2_left, y=230, anchor="nw")
    if save_path:
        save_entry.insert(0, save_path)

    def run_auto_detect():
        cur_exe = exe_entry.get().strip()
        if not cur_exe:
            feedback_lbl.configure(text="*Please select a game .exe first.*", text_color="#f0ad4e")
            return
        g_name = os.path.splitext(os.path.basename(cur_exe))[0]
        feedback_lbl.configure(text="*Searching automatically...*", text_color="#888899")

        def search():
            try:
                candidates = helpers.find_save_candidates(g_name)
            except Exception:
                candidates = []

            def update_ui():
                if candidates:
                    save_entry.delete(0, "end")
                    save_entry.insert(0, candidates[0])
                    feedback_lbl.configure(text=f"*Found: {candidates[0]}*", text_color="#5cb85c")
                else:
                    feedback_lbl.configure(text="*No save file found*", text_color="#d9534f")
            app.after(0, update_ui)

        threading.Thread(target=search, daemon=True).start()

    auto_btn = ctk.CTkButton(
        modal_frame,
        text="Search Automatically",
        width=150,
        height=35,
        font=get_ctk_font(12),
        fg_color="#2b2b2b",
        hover_color="#3b3b3b",
        text_color="#ffffff",
        corner_radius=17.5,
        command=run_auto_detect
    )
    auto_btn.place(x=s2_left + 150 + 12, y=230, anchor="nw")

    def browse_save():
        try:
            path = filedialog.askdirectory()
            if path:
                save_entry.delete(0, "end")
                save_entry.insert(0, path)
                feedback_lbl.configure(text="*Save path added successfully*", text_color="#5cb85c")
        except Exception:
            print("[DEBUG] browse_save() (settings form) crashed:")
            traceback.print_exc()

    manual_btn = ctk.CTkButton(
        modal_frame,
        text="Search Manually",
        width=150,
        height=35,
        font=get_ctk_font(13),
        fg_color="#2b2b2b",
        hover_color="#3b3b3b",
        text_color="#ffffff",
        corner_radius=17.5,
        command=browse_save
    )
    manual_btn.place(x=s2_left + 300 + 24, y=230, anchor="nw")

    feedback_lbl = ctk.CTkLabel(modal_frame, text="", font=get_ctk_font(10), text_color="#d9534f")
    feedback_lbl.place(x=593//2, y=305, anchor="center")

    disclaimer_text = (
        "Update your game executable path or save file directory below.\n"
        "Changes will take effect immediately upon confirmation."
    )
    disc_lbl = ctk.CTkLabel(modal_frame, text=disclaimer_text, font=get_ctk_font(12), text_color="#b0b0b0", justify="center")
    disc_lbl.place(x=593//2, y=295, anchor="center")

    def trigger_delete_confirm():
        delete_btn.place_forget()
        disc_lbl.configure(
            text=f"Delete \"{name}\" from your library?\nThis only removes it from the launcher — save backups are kept.",
            text_color="#e06666"
        )
        cancel_plain_btn.configure(text="Keep Game", command=cancel_delete_confirm)
        confirm_fire_btn.configure(text="Delete Forever", fg_color="#c0392b", hover_color="#992d22", text_color="#ffffff", command=do_delete_game)

    def cancel_delete_confirm():
        delete_btn.place(x=22, y=350, anchor="w")
        disc_lbl.configure(text=disclaimer_text, text_color="#b0b0b0")
        cancel_plain_btn.configure(text="Cancel", command=cancel_form)
        confirm_fire_btn.configure(text="Confirm", fg_color="#d9d9d9", hover_color="#c0c0c0", text_color="#121216", command=confirm_form)

    def do_delete_game():
        try:
            if hasattr(db, "delete_game"):
                db.delete_game(game_id)
            else:
                conn = db.get_connection() if hasattr(db, "get_connection") else sqlite3.connect(helpers.resource_path("games.db"))
                cursor = conn.cursor()
                cursor.execute("DELETE FROM games WHERE id = ?", (game_id,))
                conn.commit()
                conn.close()
        except Exception as e:
            helpers.log(f"[DEBUG] Failed to delete game from DB: {e}")
            feedback_lbl.configure(text="*Failed to delete game.*", text_color="#d9534f")
            return

        # Cover/hero art is just downloaded display art (fetch_game_art() re-fetches it
        # from SteamGridDB automatically whenever a game needs it and doesn't have it) -
        # unlike save backups, which the confirmation dialog above correctly promises to
        # KEEP. get_cover_art_path()/get_hero_art_path() are keyed by game_id, and
        # re-adding a deleted game always gets a brand new id, so leaving these two files
        # behind on delete is exactly what was silently bloating assets/covers and
        # assets/heroes every time a game got removed and re-added. Safe to delete here:
        # nothing unique to a playthrough lives in these files.
        try:
            cover_path = helpers.get_cover_art_path(game_id, name)
            if os.path.isfile(cover_path):
                os.remove(cover_path)
            hero_path = helpers.get_hero_art_path(game_id, name)
            if os.path.isfile(hero_path):
                os.remove(hero_path)
        except Exception as e:
            helpers.log(f"[DEBUG] Failed to clean up art for deleted game {game_id}: {e}")

        global selected_game_id
        if selected_game_id == game_id:
            selected_game_id = None

        close_modal_with_animation(on_complete=lambda: [
            form.destroy(),
            refresh_all()
        ])

    def cancel_form():
        on_form_close()

    def confirm_form():
        new_name = name_entry.get().strip()
        new_exe = exe_entry.get().strip()
        new_save = save_entry.get().strip()

        if not new_name:
            feedback_lbl.configure(text="*Game name is required.*", text_color="#d9534f")
            return

        if not new_exe or not os.path.exists(new_exe):
            feedback_lbl.configure(text="*Valid game .exe path is required.*", text_color="#d9534f")
            return

        if new_save and helpers.paths_conflict(new_save, new_exe):
            feedback_lbl.configure(text="*Save folder conflict with game path.*", text_color="#d9534f")
            return

        try:
            if hasattr(db, "update_game"):
                db.update_game(game_id, new_name, new_exe, new_save)
            else:
                conn = db.get_connection() if hasattr(db, "get_connection") else sqlite3.connect(helpers.resource_path("games.db"))
                cursor = conn.cursor()
                cursor.execute("UPDATE games SET name = ?, exe_path = ?, save_path = ? WHERE id = ?", (new_name, new_exe, new_save, game_id))
                conn.commit()
                conn.close()
        except Exception as e:
            helpers.log(f"[DEBUG] Failed to update game in DB: {e}")

        close_modal_with_animation(on_complete=lambda: [
            form.destroy(),
            refresh_all()
        ])

        def fetch_art():
            helpers.fetch_game_art(game_id, new_name)
            app.after(0, refresh_all)
        threading.Thread(target=fetch_art, daemon=True).start()

    btn_block_left = (593 - (150 * 2 + 19)) // 2

    cancel_plain_btn = ctk.CTkButton(
        modal_frame,
        text="Cancel",
        width=150,
        height=39,
        font=get_ctk_font(17),
        fg_color="#2b2b2b",
        hover_color="#3b3b3b",
        text_color="#ffffff",
        corner_radius=19.5,
        command=cancel_form
    )
    cancel_plain_btn.place(x=btn_block_left, y=330, anchor="nw")

    confirm_fire_btn = ctk.CTkButton(
        modal_frame,
        text="Confirm",
        width=150,
        height=39,
        font=get_ctk_font(17),
        fg_color="#d9d9d9",
        hover_color="#c0c0c0",
        text_color="#121216",
        corner_radius=19.5,
        command=confirm_form
    )
    confirm_fire_btn.place(x=btn_block_left + 150 + 19, y=330, anchor="nw")

    form.protocol("WM_DELETE_WINDOW", on_form_close)

def open_add_game_form():
    global add_game_window_open, current_indicator_y
    close_active_modal_if_any()  # if a modal is already open, swap it out instead of ignoring the click
    add_game_window_open = True
    
    animate_sidebar_indicator(get_sidebar_mid_ys()[2])

    redraw_all()

    form = ctk.CTkCTkToplevel(app) if hasattr(ctk, "CTkCTkToplevel") else ctk.CTkToplevel(app)
    form.title("Add games to launcher")
    form.geometry("593x445")
    form.resizable(False, False)
    # No form.grab_set() here on purpose - a grab blocks input to the whole app (including
    # sidebar clicks), which is why switching pages used to require hitting Cancel first.
    # The main-content actions that shouldn't fire while a modal is open (launching a game,
    # picking a different card, etc.) already separately check add_game_window_open.
    form.overrideredirect(True)
    
    form.attributes("-alpha", 0.0)

    form.update_idletasks()
    target_w, target_h = 593, 445
    target_x = app.winfo_x() + (app.winfo_width() - target_w) // 2
    target_y = app.winfo_y() + (app.winfo_height() - target_h) // 2
    form.geometry(f"593x445+{target_x}+{target_y}")

    form.configure(fg_color="#585858")

    def apply_form_rounded_corners(radius=20):
        try:
            form.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(form.winfo_id())
            
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = 2
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(ctypes.c_int(DWMWCP_ROUND)),
                ctypes.sizeof(ctypes.c_int)
            )
            
            region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, 594, 446, radius, radius)
            ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
        except Exception:
            pass

    form.after(30, lambda: apply_form_rounded_corners(20))

    pop_steps = 12
    def run_pop_in(step=0):
        if step > pop_steps:
            form.attributes("-alpha", 1.0)
            return
        t = step / pop_steps
        eased = 1 - pow(1 - t, 3)
        form.attributes("-alpha", eased)
        app.after(16, lambda: run_pop_in(step + 1))

    form.after(20, lambda: run_pop_in(0))

    def close_modal_with_animation(on_complete=None):
        def run_pop_out(step=0):
            if step > pop_steps:
                if on_complete:
                    on_complete()
                return
            t = step / pop_steps
            eased = pow(1 - t, 3)
            form.attributes("-alpha", max(0.0, eased))
            form.after(16, lambda: run_pop_out(step + 1))
        run_pop_out(0)

        global add_game_window_open, active_modal_close_fn
        add_game_window_open = False
        active_modal_close_fn = None
        # Indicator goes back to whichever icon matches the view underneath the modal -
        # carousel/home lines up with the clock icon, library with the library icon, and
        # focus (the single-game page opened from the library) with the flame/logo icon.
        animate_sidebar_indicator(get_indicator_y_for_current_view())
        redraw_all()

    def on_form_close():
        close_modal_with_animation(on_complete=lambda: form.destroy())

    global active_modal_close_fn
    active_modal_close_fn = on_form_close  # lets sidebar navigation / opening another modal close this one automatically

    modal_frame = ctk.CTkFrame(form, fg_color="#585858", corner_radius=20, border_width=0)
    modal_frame.pack(fill="both", expand=True, padx=0, pady=0)

    def start_modal_drag(event):
        form._drag_start = (event.x, event.y)

    def do_modal_drag(event):
        if hasattr(form, "_drag_start"):
            dx = event.x - form._drag_start[0]
            dy = event.y - form._drag_start[1]
            form.geometry(f"+{form.winfo_x() + dx}+{form.winfo_y() + dy}")

    modal_frame.bind("<ButtonPress-1>", start_modal_drag)
    modal_frame.bind("<B1-Motion>", do_modal_drag)

    title_lbl = ctk.CTkLabel(modal_frame, text="Add games to launcher", font=get_ctk_font(20), text_color="#ffffff")
    title_lbl.place(x=593//2, y=28, anchor="center")

    name_lbl = ctk.CTkLabel(modal_frame, text="Game Name", font=get_ctk_font(14), text_color="#ffffff")
    name_lbl.place(x=593//2, y=75, anchor="center")

    s1_left = (593 - 315) // 2

    name_entry = ctk.CTkEntry(modal_frame, width=315, height=35, fg_color="#d9d9d9", text_color="#121216", border_width=0, corner_radius=17.5, font=get_ctk_font(11), placeholder_text="e.g. The No Existence of You and Me")
    name_entry.place(x=s1_left, y=95, anchor="nw")

    dir_lbl = ctk.CTkLabel(modal_frame, text="Add game directory", font=get_ctk_font(14), text_color="#ffffff")
    dir_lbl.place(x=593//2, y=153, anchor="center")

    dir_group_left = (593 - (315 + 12 + 150)) // 2

    exe_entry = ctk.CTkEntry(modal_frame, width=315, height=35, fg_color="#d9d9d9", text_color="#121216", border_width=0, corner_radius=17.5, font=get_ctk_font(11))
    exe_entry.place(x=dir_group_left, y=175, anchor="nw")

    def browse_exe():
        try:
            path = filedialog.askopenfilename(filetypes=[("Executable files", "*.exe"), ("All files", "*.*")])
            if path:
                exe_entry.delete(0, "end")
                exe_entry.insert(0, path)
                if not name_entry.get().strip():
                    suggested_name = os.path.splitext(os.path.basename(path))[0].replace("_", " ").title()
                    name_entry.delete(0, "end")
                    name_entry.insert(0, suggested_name)
        except Exception:
            print("[DEBUG] browse_exe() (add game form) crashed:")
            traceback.print_exc()

    add_btn = ctk.CTkButton(
        modal_frame, 
        text="Add Directory", 
        width=150, 
        height=35, 
        font=get_ctk_font(15), 
        fg_color="#2b2b2b", 
        hover_color="#3b3b3b", 
        text_color="#ffffff", 
        corner_radius=17.5,
        command=browse_exe
    )
    add_btn.place(x=dir_group_left + 315 + 12, y=175, anchor="nw")

    save_lbl = ctk.CTkLabel(modal_frame, text="Add save file (optional)", font=get_ctk_font(14), text_color="#ffffff")
    save_lbl.place(x=593//2, y=233, anchor="center")

    s2_left = (593 - (150 * 3 + 24)) // 2

    save_entry = ctk.CTkEntry(modal_frame, width=150, height=35, fg_color="#d9d9d9", text_color="#121216", border_width=0, corner_radius=17.5, font=get_ctk_font(11))
    save_entry.place(x=s2_left, y=255, anchor="nw")

    def run_auto_detect():
        exe_path = exe_entry.get().strip()
        if not exe_path:
            feedback_lbl.configure(text="*Please select a game .exe or directory first.*", text_color="#f0ad4e")
            return
        game_name = os.path.splitext(os.path.basename(exe_path))[0]
        feedback_lbl.configure(text="*Searching automatically...*", text_color="#888899")

        def search():
            try:
                candidates = helpers.find_save_candidates(game_name)
            except Exception:
                candidates = []

            def update_ui():
                if candidates:
                    save_entry.delete(0, "end")
                    save_entry.insert(0, candidates[0])
                    feedback_lbl.configure(text=f"*Found: {candidates[0]}*", text_color="#5cb85c")
                else:
                    feedback_lbl.configure(text="*No save file found*", text_color="#d9534f")
            app.after(0, update_ui)

        threading.Thread(target=search, daemon=True).start()

    auto_btn = ctk.CTkButton(
        modal_frame,
        text="Search Automatically",
        width=150,
        height=35,
        font=get_ctk_font(12),
        fg_color="#2b2b2b",
        hover_color="#3b3b3b",
        text_color="#ffffff",
        corner_radius=17.5,
        command=run_auto_detect
    )
    auto_btn.place(x=s2_left + 150 + 12, y=255, anchor="nw")

    def browse_save():
        try:
            path = filedialog.askdirectory()
            if path:
                save_entry.delete(0, "end")
                save_entry.insert(0, path)
                feedback_lbl.configure(text="*Save path added successfully*", text_color="#5cb85c")
        except Exception:
            print("[DEBUG] browse_save() (add game form) crashed:")
            traceback.print_exc()

    manual_btn = ctk.CTkButton(
        modal_frame,
        text="Search Manually",
        width=150,
        height=35,
        font=get_ctk_font(13),
        fg_color="#2b2b2b",
        hover_color="#3b3b3b",
        text_color="#ffffff",
        corner_radius=17.5,
        command=browse_save
    )
    manual_btn.place(x=s2_left + 300 + 24, y=255, anchor="nw")

    feedback_lbl = ctk.CTkLabel(modal_frame, text="", font=get_ctk_font(10), text_color="#d9534f")
    feedback_lbl.place(x=593//2, y=305, anchor="center")

    disclaimer_text = (
        "Please note that “Add save file” feature is only for offline singleplayer games.\n"
        "Online or server based games hold save files on their own servers and not on your\n"
        "computer"
    )
    disc_lbl = ctk.CTkLabel(modal_frame, text=disclaimer_text, font=get_ctk_font(12), text_color="#b0b0b0", justify="center")
    disc_lbl.place(x=593//2, y=345, anchor="center")

    def cancel_form():
        on_form_close()

    def confirm_form():
        try:
            name = name_entry.get().strip()
            exe_path = exe_entry.get().strip()
            save_path = save_entry.get().strip()

            if not name:
                feedback_lbl.configure(text="*Game name is required.*", text_color="#d9534f")
                return

            if not exe_path or not os.path.exists(exe_path):
                feedback_lbl.configure(text="*Valid game .exe path is required.*", text_color="#d9534f")
                return

            if save_path and helpers.paths_conflict(save_path, exe_path):
                feedback_lbl.configure(text="*Save folder conflict with game path.*", text_color="#d9534f")
                return

            new_id = db.add_game_to_db(name, exe_path, save_path)

            close_modal_with_animation(on_complete=lambda: [
                form.destroy(),
                refresh_all()
            ])

            def fetch_art():
                try:
                    helpers.fetch_game_art(new_id, name)
                except Exception:
                    print("[DEBUG] fetch_game_art() crashed:")
                    traceback.print_exc()
                app.after(0, refresh_all)
            threading.Thread(target=fetch_art, daemon=True).start()
        except Exception:
            print("[DEBUG] confirm_form() (add game form) crashed:")
            traceback.print_exc()

    btn_block_left = (593 - (150 * 2 + 19)) // 2

    cancel_plain_btn = ctk.CTkButton(
        modal_frame,
        text="Cancel",
        width=150,
        height=39,
        font=get_ctk_font(17),
        fg_color="#2b2b2b",
        hover_color="#3b3b3b",
        text_color="#ffffff",
        corner_radius=19.5,
        command=cancel_form
    )
    cancel_plain_btn.place(x=btn_block_left, y=385, anchor="nw")

    confirm_fire_btn = ctk.CTkButton(
        modal_frame,
        text="Confirm",
        width=150,
        height=39,
        font=get_ctk_font(17),
        fg_color="#d9d9d9",
        hover_color="#c0c0c0",
        text_color="#121216",
        corner_radius=19.5,
        command=confirm_form
    )
    confirm_fire_btn.place(x=btn_block_left + 150 + 19, y=385, anchor="nw")

    form.protocol("WM_DELETE_WINDOW", on_form_close)

def open_backup_info_window():
    """Backup/restore window (Option B: manual export/import - no accounts, no server,
    nothing leaves the user's PC unless they choose to move the exported file
    themselves). Explains how the system works, then wires the actual 'Backup All Data' /
    'Restore from Backup' buttons to helpers.create_backup_archive() /
    helpers.restore_backup_archive(). Follows the exact same modal chrome/animation
    pattern as open_add_game_form()/open_game_settings_form() (borderless popup, fade
    in/out, draggable, rounded corners) for visual consistency."""
    global add_game_window_open
    close_active_modal_if_any()
    add_game_window_open = True

    animate_sidebar_indicator(get_sidebar_mid_ys()[3])

    redraw_all()

    form = ctk.CTkCTkToplevel(app) if hasattr(ctk, "CTkCTkToplevel") else ctk.CTkToplevel(app)
    form.title("Backup & Restore")
    form.geometry("593x580")
    form.resizable(False, False)
    form.overrideredirect(True)

    form.attributes("-alpha", 0.0)

    form.update_idletasks()
    target_w, target_h = 593, 580
    target_x = app.winfo_x() + (app.winfo_width() - target_w) // 2
    target_y = app.winfo_y() + (app.winfo_height() - target_h) // 2
    form.geometry(f"593x580+{target_x}+{target_y}")

    form.configure(fg_color="#585858")

    def apply_form_rounded_corners(radius=20):
        try:
            form.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(form.winfo_id())

            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = 2
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(ctypes.c_int(DWMWCP_ROUND)),
                ctypes.sizeof(ctypes.c_int)
            )

            region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, 594, 581, radius, radius)
            ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
        except Exception:
            pass

    form.after(30, lambda: apply_form_rounded_corners(20))

    pop_steps = 12
    def run_pop_in(step=0):
        if step > pop_steps:
            form.attributes("-alpha", 1.0)
            return
        t = step / pop_steps
        eased = 1 - pow(1 - t, 3)
        form.attributes("-alpha", eased)
        app.after(16, lambda: run_pop_in(step + 1))

    form.after(20, lambda: run_pop_in(0))

    def close_modal_with_animation(on_complete=None):
        def run_pop_out(step=0):
            if step > pop_steps:
                if on_complete:
                    on_complete()
                return
            t = step / pop_steps
            eased = pow(1 - t, 3)
            form.attributes("-alpha", max(0.0, eased))
            form.after(16, lambda: run_pop_out(step + 1))
        run_pop_out(0)

        global add_game_window_open, active_modal_close_fn
        add_game_window_open = False
        active_modal_close_fn = None
        animate_sidebar_indicator(get_indicator_y_for_current_view())
        redraw_all()

    def on_form_close():
        close_modal_with_animation(on_complete=lambda: form.destroy())

    global active_modal_close_fn
    active_modal_close_fn = on_form_close

    modal_frame = ctk.CTkFrame(form, fg_color="#585858", corner_radius=20, border_width=0)
    modal_frame.pack(fill="both", expand=True, padx=0, pady=0)

    def start_modal_drag(event):
        form._drag_start = (event.x, event.y)

    def do_modal_drag(event):
        if hasattr(form, "_drag_start"):
            dx = event.x - form._drag_start[0]
            dy = event.y - form._drag_start[1]
            form.geometry(f"+{form.winfo_x() + dx}+{form.winfo_y() + dy}")

    modal_frame.bind("<ButtonPress-1>", start_modal_drag)
    modal_frame.bind("<B1-Motion>", do_modal_drag)

    title_lbl = ctk.CTkLabel(modal_frame, text="Backup & Restore", font=get_ctk_font(20), text_color="#ffffff")
    title_lbl.place(x=593 // 2, y=28, anchor="center")

    intro_text = (
        "Respawn Point never asks you to sign in, and nothing you do here ever leaves\n"
        "your PC on its own. There's no account and no server - your save files and your\n"
        "playtime/last-played stats just stay local, the same as they do now. This screen\n"
        "is how you make your own copy of that data, so it survives things Respawn Point\n"
        "itself can't protect you from: deleting a game to free up space, a drive failing,\n"
        "or moving to a new PC."
    )
    intro_lbl = ctk.CTkLabel(modal_frame, text=intro_text, font=get_ctk_font(12), text_color="#d9d9d9", justify="left")
    intro_lbl.place(x=593 // 2, y=100, anchor="center")

    scroll_area = ctk.CTkScrollableFrame(modal_frame, width=525, height=215, fg_color="#4d4d4d", corner_radius=14)
    scroll_area.place(x=593 // 2, y=295, anchor="center")

    def add_section(heading, steps):
        head_lbl = ctk.CTkLabel(scroll_area, text=heading, font=get_ctk_font(14, weight="bold"), text_color="#fab301", anchor="w", justify="left")
        head_lbl.pack(fill="x", padx=10, pady=(12, 4))
        for i, step_text in enumerate(steps, start=1):
            row = ctk.CTkFrame(scroll_area, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=2)
            num_lbl = ctk.CTkLabel(row, text=str(i), font=get_ctk_font(12, weight="bold"), text_color="#121216",
                                    fg_color="#d9d9d9", corner_radius=10, width=20, height=20)
            num_lbl.pack(side="left", anchor="n", pady=1)
            step_lbl = ctk.CTkLabel(row, text=step_text, font=get_ctk_font(12), text_color="#ffffff",
                                     anchor="w", justify="left", wraplength=440)
            step_lbl.pack(side="left", padx=(10, 0), fill="x")

    add_section("Backing up your data (before deleting a game, or just for safety)", [
        "Click \"Backup All Data\". Respawn Point gathers your save files and each game's "
        "playtime/last-played stats exactly as they are right now.",
        "It packs all of that into a single file and opens a normal Save-As window - the "
        "kind you already see when picking a save folder in Add Game.",
        "Choose where to save that file. If you save it inside a OneDrive or Google Drive "
        "folder, it backs itself up to the cloud automatically from there on. Otherwise "
        "it's still a portable file you can move to a USB stick or anywhere else yourself.",
    ])

    add_section("Restoring your data (after reinstalling a game, or on a new PC)", [
        "Add the game back to your library like normal (or reinstall Respawn Point first "
        "if this is a new PC).",
        "Click \"Restore from Backup\" and pick the backup file you saved earlier.",
        "Respawn Point puts the save files back where that game expects them, and restores "
        "the playtime/last-played stats - your 43 hours (or however long) come back exactly "
        "as they were, and \"last played\" picks up counting from that saved date again.",
    ])

    tip_lbl = ctk.CTkLabel(
        scroll_area,
        text="Tip: save your backup file somewhere outside this PC (a OneDrive/Google Drive "
             "folder, or a USB stick) - that's what actually protects it if this drive fails.",
        font=get_ctk_font(11), text_color="#b0b0b0", anchor="w", justify="left", wraplength=460
    )
    tip_lbl.pack(fill="x", padx=10, pady=(14, 12))

    status_lbl = ctk.CTkLabel(modal_frame, text="", font=get_ctk_font(11), text_color="#888899",
                               justify="center", wraplength=530)
    status_lbl.place(x=593 // 2, y=425, anchor="center")

    def run_backup():
        try:
            default_name = f"RespawnPoint_Backup_{datetime.datetime.now().strftime('%Y-%m-%d')}.rpbackup"
            dest_path = filedialog.asksaveasfilename(
                defaultextension=".rpbackup",
                filetypes=[("Respawn Point Backup", "*.rpbackup"), ("All files", "*.*")],
                initialfile=default_name,
                title="Save your Respawn Point backup"
            )
            if not dest_path:
                return
            status_lbl.configure(text="*Creating backup...*", text_color="#888899")

            def do_backup():
                try:
                    metadata = helpers.create_backup_archive(dest_path)
                    count = len(metadata)
                    def done():
                        status_lbl.configure(text=f"*Backup saved - {count} game(s) included.*", text_color="#5cb85c")
                    app.after(0, done)
                except Exception as e:
                    print(f"[DEBUG] create_backup_archive failed: {e}")
                    traceback.print_exc()
                    def fail():
                        status_lbl.configure(text="*Backup failed - check debug_log.txt.*", text_color="#d9534f")
                    app.after(0, fail)

            threading.Thread(target=do_backup, daemon=True).start()
        except Exception:
            print("[DEBUG] run_backup() crashed:")
            traceback.print_exc()

    def run_restore():
        try:
            zip_path = filedialog.askopenfilename(
                filetypes=[("Respawn Point Backup", "*.rpbackup;*.zip"), ("All files", "*.*")],
                title="Choose a Respawn Point backup to restore"
            )
            if not zip_path:
                return
            status_lbl.configure(text="*Restoring...*", text_color="#888899")

            def do_restore():
                try:
                    restored, skipped = helpers.restore_backup_archive(zip_path)

                    def done():
                        if restored:
                            msg = f"*Restored {len(restored)} game(s).*"
                            if skipped:
                                msg += f" Not in your library yet, so skipped: {', '.join(skipped)}."
                            status_lbl.configure(text=msg, text_color="#5cb85c")
                        elif skipped:
                            status_lbl.configure(
                                text=f"*None of these games are in your library yet - add them first, then restore again: {', '.join(skipped)}*",
                                text_color="#f0ad4e"
                            )
                        else:
                            status_lbl.configure(text="*That file doesn't look like a valid Respawn Point backup.*", text_color="#d9534f")
                        refresh_all()

                    app.after(0, done)
                except Exception as e:
                    print(f"[DEBUG] restore_backup_archive failed: {e}")
                    traceback.print_exc()
                    def fail():
                        status_lbl.configure(text="*Restore failed - check debug_log.txt.*", text_color="#d9534f")
                    app.after(0, fail)

            threading.Thread(target=do_restore, daemon=True).start()
        except Exception:
            print("[DEBUG] run_restore() crashed:")
            traceback.print_exc()

    action_btn_block_left = (593 - (220 * 2 + 20)) // 2

    backup_btn = ctk.CTkButton(
        modal_frame,
        text="Backup All Data",
        width=220,
        height=39,
        font=get_ctk_font(15),
        fg_color="#fab301",
        hover_color="#dd9d00",
        text_color="#121216",
        corner_radius=19.5,
        command=run_backup
    )
    backup_btn.place(x=action_btn_block_left, y=440, anchor="nw")

    restore_btn = ctk.CTkButton(
        modal_frame,
        text="Restore from Backup",
        width=220,
        height=39,
        font=get_ctk_font(15),
        fg_color="#2b2b2b",
        hover_color="#3b3b3b",
        text_color="#ffffff",
        corner_radius=19.5,
        command=run_restore
    )
    restore_btn.place(x=action_btn_block_left + 220 + 20, y=440, anchor="nw")

    close_btn = ctk.CTkButton(
        modal_frame,
        text="Close",
        width=150,
        height=39,
        font=get_ctk_font(17),
        fg_color="#d9d9d9",
        hover_color="#c0c0c0",
        text_color="#121216",
        corner_radius=19.5,
        command=on_form_close
    )
    close_btn.place(x=593 // 2, y=520, anchor="center")

    form.protocol("WM_DELETE_WINDOW", on_form_close)

_sidebar_icon_cache = {}

def load_sidebar_icon(path, size):
    key = (path, size, "canvas")
    if key in _sidebar_icon_cache:
        return _sidebar_icon_cache[key]
    try:
        img = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
    except Exception:
        photo = None
    _sidebar_icon_cache[key] = photo
    return photo

def draw_sidebar_icons():
    global sidebar_indicator_item
    sidebar_w = config.SIDEBAR_W
    icon_x = sidebar_w / 2

    main_canvas.create_line(sidebar_w, 0, sidebar_w, config.WINDOW_H, fill="#2a2a35", width=1)
    
    logo_y = 45
    mid_ys = get_sidebar_mid_ys()

    indicator_height = 36
    cur_y = current_indicator_y
    # Same 2px inward nudge as the animated version in animate_sidebar_indicator() - keeps
    # this line fully inside the sidebar so the library view's panel/frame (which starts
    # right at sidebar_w) never clips part of it. Must match that offset exactly, or the
    # indicator would jump sideways whenever an animation finishes and this static draw
    # takes over.
    indicator_x = sidebar_w - 2
    sidebar_indicator_item = main_canvas.create_line(
        indicator_x, cur_y - indicator_height / 2,
        indicator_x, cur_y + indicator_height / 2,
        fill="#ffffff", width=3
    )

    logo_photo = load_sidebar_icon(config.SIDEBAR_LOGO_PATH, 40)
    if logo_photo:
        card_image_refs["sidebar_logo"] = logo_photo
        main_canvas.create_image(icon_x, logo_y, image=logo_photo)
    
    logo_hit = main_canvas.create_rectangle(0, logo_y - 25, sidebar_w, logo_y + 25, fill="", outline="")
    main_canvas.tag_bind(logo_hit, "<Button-1>", lambda e: handle_flame_click())

    # Real icon now exists (backup and restore.png in the Misc Icons folder) - loaded via
    # config.SIDEBAR_BACKUP_PATH the same way the other 3 mid icons are. getattr fallback
    # keeps this from crashing if that constant hasn't been added to config.py yet; falls
    # back to the drawn placeholder shield in that case.
    backup_icon_path = getattr(config, "SIDEBAR_BACKUP_PATH", None)
    mid_paths = [config.SIDEBAR_CLOCK_PATH, config.SIDEBAR_LIBRARY_PATH, config.SIDEBAR_DOWNLOAD_PATH, backup_icon_path]
    for i, (path, y) in enumerate(zip(mid_paths, mid_ys)):
        photo = load_sidebar_icon(path, 26) if path else render_backup_icon(26)
        if photo:
            card_image_refs[f"sidebar_mid_{i}"] = photo
            main_canvas.create_image(icon_x, y, image=photo)
        
        hit = main_canvas.create_rectangle(0, y - 20, sidebar_w, y + 20, fill="", outline="")
        if i == 0:
            # Clock icon = "recent games" = the carousel/home view. This binding was
            # missing entirely, so clicking this icon never did anything at all,
            # regardless of which view was open - not just after opening the library.
            main_canvas.tag_bind(hit, "<Button-1>", lambda e: switch_to_carousel_view())
        elif i == 1:
            main_canvas.tag_bind(hit, "<Button-1>", lambda e: switch_to_library_view())
        elif i == 2:
            main_canvas.tag_bind(hit, "<Button-1>", lambda e: open_add_game_form())
        elif i == 3:
            main_canvas.tag_bind(hit, "<Button-1>", lambda e: open_backup_info_window())

def draw_window_controls():
    close_cx, close_cy = config.WINDOW_W - 20, 20
    main_canvas.create_line(close_cx - 6, close_cy - 6, close_cx + 6, close_cy + 6, fill="#dddddd", width=2, capstyle="round")
    main_canvas.create_line(close_cx - 6, close_cy + 6, close_cx + 6, close_cy - 6, fill="#dddddd", width=2, capstyle="round")
    close_hit = main_canvas.create_rectangle(config.WINDOW_W - 45, 0, config.WINDOW_W, 45, fill="", outline="")
    main_canvas.tag_bind(close_hit, "<Button-1>", lambda e: close_app())

    min_cx, min_cy = config.WINDOW_W - 60, 20
    main_canvas.create_line(min_cx - 6, min_cy, min_cx + 6, min_cy, fill="#dddddd", width=2, capstyle="round")
    min_hit = main_canvas.create_rectangle(config.WINDOW_W - 85, 0, config.WINDOW_W - 45, 45, fill="", outline="")
    main_canvas.tag_bind(min_hit, "<Button-1>", lambda e: minimize_window())

def refresh_all():
    games = db.get_all_games()
    global selected_game_id
    if not games:
        selected_game_id = None
    elif selected_game_id is None or not any(g[0] == selected_game_id for g in games):
        selected_game_id = games[0][0]
        _recompute_slot_occupants(0, games)
    redraw_all()

def redraw_all():
    global current_hero_pil, hero_bg_item, text_overlay_item, library_frame
    global library_search_entry_widget, library_content_wrap, library_grid_holder
    global library_card_container, library_no_games_lbl, library_grid_cards
    global focus_game_id
    if animation_in_progress:
        return
        
    main_canvas.delete("all")

    if library_frame is not None:
        library_frame.destroy()
        library_frame = None
        library_search_entry_widget = None
        library_content_wrap = None
        library_grid_holder = None
        library_card_container = None
        library_no_games_lbl = None
        library_grid_cards = {}

    games_by_id = {g[0]: g for g in db.get_all_games()}
    # Focus view shows focus_game_id (set only by clicking a game in the library) instead of
    # selected_game_id (the carousel's own centered game) - kept separate so opening/closing
    # the focus page never disturbs whatever was centered in the actual carousel.
    game_id_to_show = focus_game_id if current_view == "focus" else selected_game_id
    game = games_by_id.get(game_id_to_show)

    hero_path = helpers.get_hero_art_path(game[0], game[1]) if game else None
    is_running = bool(active_sessions.get(game[0])) if game else False
    
    composite = build_background_composite(hero_path, config.WINDOW_W, config.WINDOW_H, game=game, is_running=is_running)
    current_hero_pil = composite

    bg_photo = ImageTk.PhotoImage(composite)
    card_image_refs["_bg"] = bg_photo
    hero_bg_item = main_canvas.create_image(0, 0, anchor="nw", image=bg_photo)

    if current_view == "library":
        # Full-height panel so the top strip (above the scrollable frame, reserved for the
        # window controls/drag zone) is covered too - same exact color as the frame below,
        # so the two layers are indistinguishable and there's no seam between them.
        panel_img = Image.new("RGB", (config.WINDOW_W - config.SIDEBAR_W, config.WINDOW_H), LIBRARY_BG_RGB)
        panel_photo = ImageTk.PhotoImage(panel_img)
        card_image_refs["_library_panel"] = panel_photo
        main_canvas.create_image(config.SIDEBAR_W, 0, anchor="nw", image=panel_photo)

    draw_window_controls()
    draw_sidebar_icons()

    if add_game_window_open:
        overlay_img = Image.new("RGBA", (config.WINDOW_W - config.SIDEBAR_W, config.WINDOW_H), (15, 15, 18, 230))
        overlay_photo = ImageTk.PhotoImage(overlay_img)
        card_image_refs["_modal_overlay"] = overlay_photo
        main_canvas.create_image(config.SIDEBAR_W, 0, anchor="nw", image=overlay_photo)
        return

    # Render Library View if active
    if current_view == "library":
        # Dense grid flush against the sidebar, matching his mockup - the frame IS the flat gray
        # background now (single layer, no separate canvas panel to fall out of sync with)
        lib_x = config.SIDEBAR_W
        lib_y = 60
        lib_w = config.WINDOW_W - config.SIDEBAR_W
        lib_h = config.WINDOW_H - lib_y

        library_frame = ctk.CTkScrollableFrame(
            master=app,
            width=lib_w,
            height=lib_h,
            fg_color=LIBRARY_BG_HEX,
            scrollbar_button_color="#3a3a45",
            scrollbar_button_hover_color="#50505d"
        )
        library_frame.place(x=lib_x, y=lib_y)

        total_row_w = LIB_NUM_COLS * (LIB_CARD_W + LIB_CARD_PAD * 2)

        # --- Title position: change these two numbers freely, nothing else needs to change ---
        TITLE_Y = 25        # vertical position of the title, in pixels from the top of the library panel
        TITLE_X_NUDGE = -8  # small horizontal correction so the title's center lines up with the search
                             # bar/grid below (they're nudged right by padx=(0, 15) to make room for the
                             # scrollbar) - you likely won't need to touch this one
        # The title is a direct child of library_frame, positioned with .place() instead of .pack().
        # That's what makes it independent: dragging TITLE_Y up or down only ever moves the title -
        # it can never drag the search bar or card grid with it, because .place() doesn't participate
        # in content_wrap's pack() stack below at all.
        title_lbl = ctk.CTkLabel(
            library_frame,
            text="Games Library",
            font=get_ctk_font(24, weight="bold"),
            text_color="#ffffff",
            width=total_row_w
        )
        title_lbl.place(relx=0.5, x=TITLE_X_NUDGE, y=TITLE_Y, anchor="n")

        # A plain frame packed with no fill/expand gets auto-centered horizontally by
        # Tkinter's own layout math (real widths at render time) - no need to guess the
        # scrollbar's exact width like the old margin_x approach did. padx=(0, 15) is the
        # nudge he dialed in by eye to land the row dead-center for his window size.
        # pady=(70, 0) is a fixed gap that leaves room for the title above - it does NOT
        # depend on TITLE_Y, so moving the title never shifts the search bar/grid. If you
        # push TITLE_Y far enough down that the title visually overlaps the search bar,
        # bump this number up to make more room - it just won't happen automatically.
        content_wrap = ctk.CTkFrame(library_frame, fg_color="transparent")
        content_wrap.pack(pady=(70, 0), padx=(0, 15))
        library_content_wrap = content_wrap

        search_var = StringVar(value=library_search_query)
        search_entry = ctk.CTkEntry(
            content_wrap,
            width=total_row_w,
            height=40,
            corner_radius=8,
            fg_color="#242429",
            border_width=1,
            border_color="#3a3a45",
            text_color="#ffffff",
            placeholder_text="Search your library",
            textvariable=search_var
        )
        search_entry.pack(pady=(0, 20))
        search_entry.bind("<Return>", lambda e: submit_library_search(search_var.get()))

        # Live search: filter as-you-type instead of waiting for Enter. render_library_grid()
        # only rebuilds the card grid below (a sibling widget) - it never touches this entry,
        # so typing never loses focus/cursor position and never flickers the rest of the
        # window (canvas/sidebar/background aren't touched either).
        def _on_search_var_changed(*_args):
            global library_search_query
            # Deliberately NOT stripped here - stripping on every keystroke silently deletes
            # a trailing space the instant it's typed, which is what made the spacebar look
            # broken (type "call", hit space, and the space vanished before it ever showed).
            library_search_query = search_var.get()
            render_library_grid()
        search_var.trace_add("write", _on_search_var_changed)

        library_search_entry_widget = search_entry

        render_library_grid()
        return

    if not game:
        main_canvas.create_text(config.CONTENT_X, config.WINDOW_H // 2, anchor="w", fill="#888888",
                                text="No games yet - use + Add Game to get started", font=get_pil_font(16))
        return

    game_id, name, exe_path, save_path, last_played, total_minutes = game

    header_img = Image.new("RGBA", (500, 40), (0, 0, 0, 0))
    d_hdr = ImageDraw.Draw(header_img)
    f_hdr = get_pil_font(20)
    
    part1 = "Respawn "
    d_hdr.text((0, 2), part1, fill=(250, 179, 1, 255), font=f_hdr)
    bbox1 = d_hdr.textbbox((0, 2), part1, font=f_hdr)
    w1 = bbox1[2] - bbox1[0]
    
    d_hdr.text((w1, 2), "Point", fill=(255, 255, 255, 255), font=f_hdr)

    header_photo = ImageTk.PhotoImage(header_img)
    card_image_refs["_header_title"] = header_photo
    main_canvas.create_image(config.CONTENT_X, 34, anchor="nw", image=header_photo)

    txt_pil = render_text_block(game, alpha=1.0)
    txt_photo = ImageTk.PhotoImage(txt_pil)
    card_image_refs["_text_static"] = txt_photo
    text_overlay_item = main_canvas.create_image(TEXT_REGION_X - 10, TEXT_REGION_Y - 10, anchor="nw", image=txt_photo)

    draw_launch_and_settings_buttons(main_canvas, config.WINDOW_W, config.WINDOW_H, game, is_running, launch_hover_progress, cogwheel_angle)

    if current_view != "carousel":
        # Focus view (opened from the library) shows only this one game's panel above -
        # no side-card row - but it does get its own single cover-art card with the same
        # gold selection border the carousel's center card uses, so this area isn't left
        # empty. Uses CENTER_CARD_SIZE (same size as the carousel's center card) but
        # left-aligned under the title/stats (CONTENT_X) instead of centered in the row,
        # since there are no side cards to center it among here.
        w, h = config.CENTER_CARD_SIZE
        x_cursor = config.CONTENT_X
        y_top = config.CARD_ROW_BOTTOM_Y - h

        cover_path = helpers.get_cover_art_path(game_id, name)
        if cover_path and os.path.exists(cover_path):
            try:
                pil_img = Image.open(cover_path)
            except Exception:
                pil_img = Image.new("RGB", (600, 900), (40, 40, 45))
        else:
            pil_img = Image.new("RGB", (600, 900), (40, 40, 45))

        card_img = rounded_image(pil_img, w, h, radius=12, resample=Image.LANCZOS)
        card_photo = ImageTk.PhotoImage(card_img)
        card_image_refs["_focus_card"] = card_photo
        main_canvas.create_image(x_cursor, y_top, anchor="nw", image=card_photo)
        draw_outer_selection_border(main_canvas, x_cursor, y_top, w, h, radius=12, alpha=1.0)
        return

    all_games = db.get_all_games()
    games_by_id_for_slots = {g[0]: g for g in all_games}
    
    ids = [g[0] for g in all_games]
    center_idx = ids.index(selected_game_id) if selected_game_id in ids else 0
    _recompute_slot_occupants(center_idx, all_games)

    dynamic_slot_x = compute_dynamic_slot_x()

    visible_card_items.clear()
    for offset, g_id in list(slot_occupants.items()):
        g = games_by_id_for_slots.get(g_id)
        if not g:
            continue
        w, h = config.CENTER_CARD_SIZE if offset == 0 else config.SIDE_CARD_SIZE
        x_cursor = dynamic_slot_x[offset]
        g_id, g_name, g_exe, g_save, g_last, g_total = g
        cover_path = helpers.get_cover_art_path(g_id, g_name)

        if cover_path and os.path.exists(cover_path):
            try:
                pil_img = Image.open(cover_path)
            except Exception:
                pil_img = Image.new("RGB", (600, 900), (40, 40, 45))
        else:
            pil_img = Image.new("RGB", (600, 900), (40, 40, 45))

        card_img = rounded_image(pil_img, w, h, radius=12, resample=Image.LANCZOS)
        card_photo = ImageTk.PhotoImage(card_img)
        card_image_refs[g_id] = card_photo

        y_top = config.CARD_ROW_BOTTOM_Y - h
        item = main_canvas.create_image(x_cursor, y_top, anchor="nw", image=card_photo)

        if offset == 0:
            border_item = draw_outer_selection_border(main_canvas, x_cursor, y_top, w, h, radius=12, alpha=1.0)
            visible_card_items.append(border_item)

        hit_zone = main_canvas.create_rectangle(x_cursor, y_top, x_cursor + w, y_top + h, fill="", outline="")
        main_canvas.tag_bind(hit_zone, "<Button-1>", lambda e, gid=g_id: select_game(gid))

        CARD_LIFT = 8
        def on_card_enter(e, itm=item, card_offset=offset):
            if not animation_in_progress and card_offset != 0:
                animate_lift(main_canvas, itm, CARD_LIFT)
        def on_card_leave(e, itm=item, card_offset=offset):
            if not animation_in_progress and card_offset != 0:
                animate_lift(main_canvas, itm, 0)
        main_canvas.tag_bind(hit_zone, "<Enter>", on_card_enter)
        main_canvas.tag_bind(hit_zone, "<Leave>", on_card_leave)

        visible_card_items.append(item)
        visible_card_items.append(hit_zone)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

db.init_db()

app = ctk.CTk()
app.title("Respawn Point")
app.geometry(f"{config.WINDOW_W}x{config.WINDOW_H}")
app.resizable(False, False)
app.overrideredirect(True)

def apply_rounded_window_corners(radius=20):
    try:
        app.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(app.winfo_id())
        
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(ctypes.c_int(DWMWCP_ROUND)),
            ctypes.sizeof(ctypes.c_int)
        )

        region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, config.WINDOW_W + 1, config.WINDOW_H + 1, radius, radius)
        ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
    except Exception:
        pass

def force_taskbar_icon():
    # overrideredirect(True) (used for the borderless rounded-corner look above) has a
    # side effect on Windows: it marks the window as a "tool window", which Explorer
    # excludes from the taskbar entirely - that's why the main launcher never showed an
    # icon there. This explicitly overrides that window style back to a normal app window.
    try:
        app.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(app.winfo_id())

        GWL_EXSTYLE = -20
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080

        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

        # Explorer only re-reads a window's taskbar-presence style when that window is
        # shown/hidden, not just when the style bits change - so briefly hide and re-show
        # it to force that refresh.
        app.withdraw()
        app.after(10, app.deiconify)
    except Exception:
        pass

app.after(30, force_taskbar_icon)
app.after(80, lambda: apply_rounded_window_corners(20))

main_canvas = ctk.CTkCanvas(app, width=config.WINDOW_W, height=config.WINDOW_H, highlightthickness=0, bg="#121216")
main_canvas.pack(fill="both", expand=True)

def start_window_drag(event):
    if event.y < 45 and event.x > config.SIDEBAR_W:
        app._drag_start = (event.x, event.y)
    else:
        app._drag_start = None

def do_window_drag(event):
    if hasattr(app, "_drag_start") and app._drag_start:
        dx = event.x - app._drag_start[0]
        dy = event.y - app._drag_start[1]
        app.geometry(f"+{app.winfo_x() + dx}+{app.winfo_y() + dy}")

def close_app():
    app.destroy()

def minimize_window():
    try:
        ctypes.windll.user32.ShowWindow(ctypes.windll.user32.GetParent(app.winfo_id()), 6)
    except Exception:
        app.iconify()

main_canvas.bind("<ButtonPress-1>", start_window_drag)
main_canvas.bind("<B1-Motion>", do_window_drag)

refresh_all()
app.after(16, animate_launch_hover)

app.mainloop()