import os
import time
import subprocess
import threading
import datetime
import urllib.request
from tkinter import filedialog
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

# Preferred font settings
PRIMARY_FONT_NAME = "Cal Sans"
FONT_FILE = os.path.join("assets", "Fonts", "CalSans-SemiBold.ttf")

def ensure_font_exists():
    """Checks if the font exists locally; if not, tries to download it."""
    global FONT_FILE
    print(f"[DEBUG] Looking for font at: {os.path.abspath(FONT_FILE)}")
    
    if os.path.exists(FONT_FILE):
        print("[DEBUG] Local font found successfully!")
        return

    alternatives = [
        os.path.join("assets", "Fonts", "CalSans.ttf"),
        os.path.join("assets", "Fonts", "Cal Sans SemiBold.ttf"),
        os.path.join("assets", "Fonts", "calsans.ttf"),
        "CalSans-SemiBold.ttf"
    ]
    for alt in alternatives:
        if os.path.exists(alt):
            FONT_FILE = alt
            print(f"[DEBUG] Found font under alternative name: {alt}")
            return

    try:
        print("[DEBUG] Downloading Cal Sans font...")
        os.makedirs(os.path.join("assets", "Fonts"), exist_ok=True)
        url = "https://raw.githubusercontent.com/calcom/font/main/fonts/ttf/CalSans-SemiBold.ttf"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(FONT_FILE, 'wb') as out_file:
            out_file.write(response.read())
        print("[DEBUG] Cal Sans downloaded successfully!")
    except Exception as e:
        print(f"[DEBUG] Could not download Cal Sans font: {e}")

ensure_font_exists()

TEXT_REGION_X = config.CONTENT_X
TEXT_REGION_Y = int(config.WINDOW_H * 0.35)
TEXT_REGION_W = 600
TEXT_REGION_H = 200

def get_pil_font(size):
    font_candidates = [
        FONT_FILE,
        os.path.join("assets", "Fonts", "CalSans-SemiBold.ttf"),
        os.path.join("assets", "Fonts", "CalSans-SemiBold.otf"),
        os.path.join("assets", "Fonts", "CalSans.ttf"),
        r"C:\Windows\Fonts\CalSans-SemiBold.ttf",
        r"C:\Windows\Fonts\CalSans-SemiBold.otf"
    ]
    for font in font_candidates:
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

def draw_pill_button(canvas, x, y, width, height, text, fill_color, command=None, text_color="white", hover_color=None):
    group_tag = f"pill_{id(object())}"
    oval1 = canvas.create_oval(x, y, x + height, y + height, fill=fill_color, outline=fill_color, tags=group_tag)
    oval2 = canvas.create_oval(x + width - height, y, x + width, y + height, fill=fill_color, outline=fill_color, tags=group_tag)
    rect = canvas.create_rectangle(x + height / 2, y, x + width - height / 2, y + height, fill=fill_color, outline=fill_color, tags=group_tag)
    
    txt_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(txt_img)
    f = get_pil_font(25)
    bbox = d.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (width - tw) / 2 - bbox[0]
    ty = (height - th) / 2 - bbox[1]
    d.text((tx, ty), text, fill=text_color, font=f)
    
    pill_text_photo = ImageTk.PhotoImage(txt_img)
    card_image_refs[f"pill_txt_{group_tag}"] = pill_text_photo
    canvas.create_image(x, y, anchor="nw", image=pill_text_photo, tags=group_tag)

    if command:
        hit_area = canvas.create_rectangle(x, y, x + width, y + height, fill="", outline="")
        canvas.tag_bind(hit_area, "<Button-1>", lambda e: command())
        LIFT = 5
        def on_enter(e):
            if hover_color:
                for item in (oval1, oval2, rect):
                    canvas.itemconfig(item, fill=hover_color, outline=hover_color)
            animate_lift(canvas, group_tag, LIFT)
        def on_leave(e):
            if hover_color:
                for item in (oval1, oval2, rect):
                    canvas.itemconfig(item, fill=fill_color, outline=fill_color)
            animate_lift(canvas, group_tag, 0)
        canvas.tag_bind(hit_area, "<Enter>", on_enter)
        canvas.tag_bind(hit_area, "<Leave>", on_leave)
        canvas.tag_raise(hit_area)

def rounded_image(pil_img, w, h, radius=12, alpha=1.0, resample=Image.LANCZOS):
    w, h = max(1, int(w)), max(1, int(h))
    if pil_img is None:
        pil_img = Image.new("RGBA", (600, 900), (40, 40, 45, 255))
    src_w, src_h = pil_img.size
    scale = max(w / src_w, h / src_h)
    resized = pil_img.resize((max(1, int(src_w * scale)), max(1, int(src_h * scale))), resample)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    cropped = resized.crop((left, top, left + w, top + h)).convert("RGBA")

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

_hero_composite_cache = {}

def build_background_composite(hero_path, width, height):
    cache_key = (hero_path, width, height)
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
        except Exception as e:
            helpers.log(f"[DEBUG] Failed to load hero image {hero_path}: {e}")
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
    result = Image.composite(black, img, gradient)
    _hero_composite_cache[cache_key] = result
    return result

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
    if game_id == selected_game_id or animation_in_progress:
        return
    if selected_game_id is None:
        selected_game_id = game_id
        redraw_all()
        return

    animate_transition(game_id)

def animate_transition(new_game_id):
    global selected_game_id, animation_in_progress
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
    new_hero_pil = build_background_composite(new_hero_path, config.WINDOW_W, config.WINDOW_H)
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

# --- Game Launcher ---
def launch_game(game_id, name, exe_path, save_path):
    if not exe_path or not os.path.exists(exe_path):
        print(f"[DEBUG] Launch aborted - exe not found at: {exe_path}")
        return

    if active_sessions.get(game_id):
        print(f"[DEBUG] Ignoring click - {name} is already running")
        return

    active_sessions[game_id] = True
    refresh_all()

    def run():
        game_folder = os.path.dirname(exe_path)
        start_time = datetime.datetime.now()
        print(f"[DEBUG] Launching: {exe_path}")

        helpers.restore_if_missing(game_id, name, save_path)

        try:
            process = subprocess.Popen(exe_path, cwd=game_folder)
            print(f"[DEBUG] Process started with PID {process.pid}")
        except Exception as e:
            print(f"[DEBUG] Failed to launch process: {e}")
            active_sessions.pop(game_id, None)
            app.after(0, refresh_all)
            return

        time.sleep(2)
        if process.poll() is not None:
            print(f"[DEBUG] WARNING: process exited almost immediately (code {process.poll()}).")
            active_sessions.pop(game_id, None)
            app.after(0, refresh_all)
            return

        process.wait()
        minutes_played = max(1, int((datetime.datetime.now() - start_time).total_seconds() // 60))
        print(f"[DEBUG] Process exited after {minutes_played} min")

        helpers.backup_save(game_id, name, save_path)
        db.record_session(game_id, minutes_played)

        def finish():
            active_sessions.pop(game_id, None)
            refresh_all()
        app.after(0, finish)

    threading.Thread(target=run, daemon=True).start()

# --- Add Game Modal ---
def open_add_game_form():
    form = ctk.CTkToplevel(app)
    form.title("Add Game")
    form.geometry("450x520")
    form.grab_set()

    ctk.CTkLabel(form, text="Game Name", font=(PRIMARY_FONT_NAME, 13)).pack(pady=(20, 5))
    name_entry = ctk.CTkEntry(form, width=350, placeholder_text="e.g. Sekiro")
    name_entry.pack()

    ctk.CTkLabel(form, text="Game .exe Location", font=(PRIMARY_FONT_NAME, 13)).pack(pady=(15, 5))
    exe_frame = ctk.CTkFrame(form, fg_color="transparent")
    exe_frame.pack()
    exe_entry = ctk.CTkEntry(exe_frame, width=270)
    exe_entry.pack(side="left", padx=(0, 5))

    def browse_exe():
        path = filedialog.askopenfilename(filetypes=[("Executable files", "*.exe")])
        if path:
            exe_entry.delete(0, "end")
            exe_entry.insert(0, path)

    ctk.CTkButton(exe_frame, text="Browse", width=70, font=(PRIMARY_FONT_NAME, 12), command=browse_exe).pack(side="left")

    ctk.CTkLabel(form, text="Save Folder Location (optional)", font=(PRIMARY_FONT_NAME, 13)).pack(pady=(15, 5))
    ctk.CTkLabel(
        form,
        text="For offline single-player games only, so progress survives deletion.\n"
             "Leave this blank for online/live-service games (Genshin Impact,\n"
             "Wuthering Waves, etc.) - their progress lives on the developer's\n"
             "servers, not on your PC, so there's nothing here to back up.",
        font=(PRIMARY_FONT_NAME, 10), text_color="gray", justify="center"
    ).pack()
    save_frame = ctk.CTkFrame(form, fg_color="transparent")
    save_frame.pack(pady=(5, 0))
    save_entry = ctk.CTkEntry(save_frame, width=200)
    save_entry.pack(side="left", padx=(0, 5))

    def browse_save():
        path = filedialog.askdirectory()
        if path:
            save_entry.delete(0, "end")
            save_entry.insert(0, path)

    ctk.CTkButton(save_frame, text="Browse", width=70, font=(PRIMARY_FONT_NAME, 12), command=browse_save).pack(side="left", padx=(0, 5))

    detect_status = ctk.CTkLabel(form, text="", font=(PRIMARY_FONT_NAME, 11), wraplength=380)
    detect_status.pack(pady=(5, 0))

    def run_auto_detect():
        name = name_entry.get().strip()
        if not name:
            detect_status.configure(text="Type the game's name first.", text_color="orange")
            return

        detect_status.configure(text="Searching...", text_color="gray")

        def search():
            try:
                candidates = helpers.find_save_candidates(name)
            except Exception as e:
                print(f"[DEBUG] Auto-detect failed: {e}")
                candidates = []

            def show_result():
                if not candidates:
                    detect_status.configure(text="Couldn't find this game automatically - please browse manually.", text_color="orange")
                elif len(candidates) == 1:
                    save_entry.delete(0, "end")
                    save_entry.insert(0, candidates[0])
                    detect_status.configure(text="Found it and filled in the field above.", text_color="lightgreen")
                else:
                    detect_status.configure(text="Found multiple - pick one:", text_color="lightgreen")
                    for path in candidates:
                        def pick(p=path):
                            save_entry.delete(0, "end")
                            save_entry.insert(0, p)
                        ctk.CTkButton(form, text=path, command=pick, font=(PRIMARY_FONT_NAME, 10)).pack(pady=2)

            app.after(0, show_result)

        threading.Thread(target=search, daemon=True).start()

    ctk.CTkButton(save_frame, text="Auto-Detect", width=90, font=(PRIMARY_FONT_NAME, 12), command=run_auto_detect).pack(side="left")

    error_label = ctk.CTkLabel(form, text="", font=(PRIMARY_FONT_NAME, 11), text_color="red", wraplength=380)
    error_label.pack(pady=(10, 0))

    def submit():
        name = name_entry.get().strip()
        exe_path = exe_entry.get().strip()
        save_path = save_entry.get().strip()

        if not name or not exe_path:
            error_label.configure(text="Name and .exe path are required.")
            return

        if save_path and helpers.paths_conflict(save_path, exe_path):
            error_label.configure(
                text="That save folder is the game's install folder (or contains it). "
                     "This isn't allowed - point it at the real save location instead "
                     "(usually under AppData or Documents)."
            )
            return

        new_id = db.add_game_to_db(name, exe_path, save_path)
        form.destroy()
        refresh_all()

        def fetch_art():
            helpers.fetch_game_art(new_id, name)
            app.after(0, refresh_all)
        threading.Thread(target=fetch_art, daemon=True).start()

    ctk.CTkButton(form, text="Save Game", font=(PRIMARY_FONT_NAME, 13, "bold"), command=submit).pack(pady=20)

# --- Drawing Utilities ---
_sidebar_icon_cache = {}

def load_sidebar_icon(path, size):
    key = (path, size)
    if key in _sidebar_icon_cache:
        return _sidebar_icon_cache[key]
    try:
        img = Image.open(path).convert("RGBA")
        img.thumbnail((size, size), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
    except Exception as e:
        print(f"[DEBUG] Could not load sidebar icon '{path}': {e}")
        photo = None
    _sidebar_icon_cache[key] = photo
    return photo

def draw_sidebar_icons():
    sidebar_w = config.SIDEBAR_W
    icon_x = sidebar_w / 2

    # Draw the thin vertical separating line on the right edge of the sidebar
    main_canvas.create_line(sidebar_w, 0, sidebar_w, config.WINDOW_H, fill="#2a2a35", width=1)

    # Sidebar icons configuration & vertical positions
    # Index 0: Logo / Home (top) -> currently selected
    # Index 1: Clock
    # Index 2: Library
    # Index 3: Download
    # Index 4: User (bottom)
    
    logo_y = 45
    mid_ys = [config.WINDOW_H * 0.42, config.WINDOW_H * 0.42 + 55, config.WINDOW_H * 0.42 + 110]
    user_y = config.WINDOW_H - 45

    # Draw small white indicator line on top of the border, right in front of the selected icon (Logo/Home at index 0)
    selected_icon_y = logo_y
    indicator_height = 36
    main_canvas.create_line(
        sidebar_w, selected_icon_y - indicator_height / 2,
        sidebar_w, selected_icon_y + indicator_height / 2,
        fill="#ffffff", width=3
    )

    logo_photo = load_sidebar_icon(config.SIDEBAR_LOGO_PATH, 40)
    if logo_photo:
        main_canvas.create_image(icon_x, logo_y, image=logo_photo)

    mid_paths = [config.SIDEBAR_CLOCK_PATH, config.SIDEBAR_LIBRARY_PATH, config.SIDEBAR_DOWNLOAD_PATH]
    for path, y in zip(mid_paths, mid_ys):
        photo = load_sidebar_icon(path, 26)
        if photo:
            main_canvas.create_image(icon_x, y, image=photo)

    user_photo = load_sidebar_icon(config.SIDEBAR_USER_PATH, 26)
    if user_photo:
        main_canvas.create_image(icon_x, user_y, image=user_photo)

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

# --- Core Canvas Redraw ---
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
    global current_hero_pil, hero_bg_item, text_overlay_item
    if animation_in_progress:
        return
        
    main_canvas.delete("all")

    games_by_id = {g[0]: g for g in db.get_all_games()}
    game = games_by_id.get(selected_game_id)

    hero_path = helpers.get_hero_art_path(game[0], game[1]) if game else None
    composite = build_background_composite(hero_path, config.WINDOW_W, config.WINDOW_H)
    current_hero_pil = composite
    bg_photo = ImageTk.PhotoImage(composite)
    card_image_refs["_bg"] = bg_photo
    hero_bg_item = main_canvas.create_image(0, 0, anchor="nw", image=bg_photo)
    
    draw_window_controls()
    draw_sidebar_icons()

    if not game:
        main_canvas.create_text(config.CONTENT_X, config.WINDOW_H // 2, anchor="w", fill="#888888",
                                text="No games yet - use + Add Game to get started", font=(PRIMARY_FONT_NAME, 16))
        return

    game_id, name, exe_path, save_path, last_played, total_minutes = game

    header_img = Image.new("RGBA", (400, 40), (0, 0, 0, 0))
    d_hdr = ImageDraw.Draw(header_img)
    f_hdr = get_pil_font(20)
    
    part1 = "Respawn "
    d_hdr.text((0, 6), part1, fill=(250, 179, 1, 255), font=f_hdr)
    bbox1 = d_hdr.textbbox((0, 6), part1, font=f_hdr)
    w1 = bbox1[2] - bbox1[0]
    
    d_hdr.text((w1, 6), "Point", fill=(255, 255, 255, 255), font=f_hdr)
    
    header_photo = ImageTk.PhotoImage(header_img)
    card_image_refs["_header_title"] = header_photo
    main_canvas.create_image(config.CONTENT_X, 30, anchor="nw", image=header_photo)

    txt_pil = render_text_block(game, alpha=1.0)
    txt_photo = ImageTk.PhotoImage(txt_pil)
    card_image_refs["_text_static"] = txt_photo
    text_overlay_item = main_canvas.create_image(TEXT_REGION_X - 10, TEXT_REGION_Y - 10, anchor="nw", image=txt_photo)

    is_running = active_sessions.get(game_id)
    draw_pill_button(
        main_canvas,
        x=config.WINDOW_W - 240, y=config.WINDOW_H - 100, width=190, height=56,
        text=("RUNNING" if is_running else "LAUNCH"),
        fill_color=("#555555" if is_running else "#fab301"),
        hover_color=(None if is_running else "#d99b00"),
        text_color="#ffffff",
        command=(None if is_running else lambda: launch_game(game_id, name, exe_path, save_path))
    )

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

# --- App Setup ---
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
        import ctypes
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
    except Exception as e:
        print(f"[DEBUG] Could not apply rounded window corners: {e}")

app.after(50, apply_rounded_window_corners)

main_canvas = ctk.CTkCanvas(app, width=config.WINDOW_W, height=config.WINDOW_H, highlightthickness=0, bg="#0a0a0f")
main_canvas.pack(fill="both", expand=True)

def start_drag(event):
    app._drag_start = (event.x, event.y)

def do_drag(event):
    if hasattr(app, "_drag_start") and event.y < 40 and event.x < config.WINDOW_W - 90:
        dx = event.x - app._drag_start[0]
        dy = event.y - app._drag_start[1]
        app.geometry(f"+{app.winfo_x() + dx}+{app.winfo_y() + dy}")

main_canvas.bind("<ButtonPress-1>", start_drag, add="+")
main_canvas.bind("<B1-Motion>", do_drag, add="+")

minimized_state = {"active": False}

def minimize_window():
    minimized_state["active"] = True
    app.overrideredirect(False)
    app.iconify()

def handle_restore(event=None):
    if minimized_state["active"] and app.state() == "normal":
        minimized_state["active"] = False
        app.overrideredirect(True)
        app.after(30, lambda: apply_rounded_window_corners(20))

app.bind("<Map>", handle_restore)

periodic_refresh_id = {"id": None}

def close_app():
    if periodic_refresh_id["id"]:
        app.after_cancel(periodic_refresh_id["id"])
    card_image_refs.clear()
    app.quit()
    app.destroy()

refresh_all()

def fetch_missing_cover_art():
    for game in db.get_all_games():
        game_id, name, exe_path, save_path, last_played, total_minutes = game
        if not os.path.exists(helpers.get_cover_art_path(game_id, name)) or not os.path.exists(helpers.get_hero_art_path(game_id, name)):
            helpers.fetch_game_art(game_id, name)
            app.after(0, refresh_all)

threading.Thread(target=fetch_missing_cover_art, daemon=True).start()

def periodic_refresh():
    refresh_all()
    periodic_refresh_id["id"] = app.after(30000, periodic_refresh)

periodic_refresh_id["id"] = app.after(30000, periodic_refresh)

app.mainloop()