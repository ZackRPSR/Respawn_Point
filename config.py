import os

# --- Database & Storage Paths ---
DB_FILE = "library.db"
BACKUPS_ROOT = os.path.join("backups", "saves")
COVER_ART_ROOT = os.path.join("assets", "covers")
HERO_ART_ROOT = os.path.join("assets", "heroes")

# --- API Settings ---
STEAMGRIDDB_API_KEY = "ef10bd125bc7253fe8a686b5cdae6314"
STEAMGRIDDB_BASE = "https://www.steamgriddb.com/api/v2"
MANIFEST_URL = "https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/master/data/manifest.yaml"
MANIFEST_CACHE_FILE = "ludusavi_manifest_cache.yaml"

# --- UI Dimensions & Coordinates ---
WINDOW_W, WINDOW_H = 1200, 700
SIDEBAR_W = 70
CONTENT_X = SIDEBAR_W + 40
CARD_ROW_BOTTOM_Y = WINDOW_H - 40

SIDE_CARD_SIZE = (95, 130)
CENTER_CARD_SIZE = (145, 200)
CAROUSEL_GAP = 16
CARD_OFFSETS = (-2, -1, 0, 1, 2)

# --- Animation Settings ---
ANIMATION_STEPS = 8
ANIMATION_DELAY_MS = 16

# --- Sidebar Icons ---
SIDEBAR_LOGO_PATH = os.path.join("assets", "icons", "V2 variants", "Golden flame (with transparent background).png")
SIDEBAR_CLOCK_PATH = os.path.join("assets", "icons", "Misc Icons", "image (2).png")
SIDEBAR_LIBRARY_PATH = os.path.join("assets", "icons", "Misc Icons", "library.png")
SIDEBAR_DOWNLOAD_PATH = os.path.join("assets", "icons", "Misc Icons", "Download logo.png")
SIDEBAR_USER_PATH = os.path.join("assets", "icons", "Misc Icons", "User Logo.png")