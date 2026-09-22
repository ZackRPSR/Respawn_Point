"""Minimal sound-effect layer for Respawn Point. Uses pygame.mixer instead of winsound
because it needs to overlap sounds (e.g. a bong on a button press while a switch sound
from a moment ago is still tailing off) - winsound is blocking and single-channel, so
overlapping playback would cut sounds off or freeze the UI thread.

Two sound effects exist ("switch" for sidebar icon navigation + recent-games/carousel
card switching, "bong" for every other action button), plus one looping background
music track that fades in/out based on window and game-launch state (see
set_music_playing below). No hover sounds - kept deliberately minimal per design
decision.

Every public function here is exception-safe: a missing pygame install, a missing sound
file, or a dead audio device should never crash or freeze the launcher. Worst case, sounds
just silently don't play and a [DEBUG] line gets logged.
"""

import helpers
import config

_initialized = False
_sounds = {}

# Background music uses pygame.mixer.music (a separate, dedicated streaming channel)
# rather than mixer.Sound like the SFX above - Sound loads the whole file into memory
# and has no built-in fade support, while mixer.music streams from disk and has
# fade-in (the fade_ms argument to play()) and fade-out (fadeout()) built in, which is
# exactly what a crossfading background track needs.
MUSIC_RELATIVE_PATH = "assets/sounds/ChillLofi.ogg"
_music_loaded = False
_music_playing_state = False


def init_audio():
    """Call once at startup, after config/helpers are ready and before the main loop.
    Safe to call more than once - subsequent calls are no-ops."""
    global _initialized
    if _initialized:
        return

    try:
        import pygame
    except Exception as e:
        helpers.log(f"[DEBUG] pygame not available - audio disabled: {e}")
        return

    try:
        pygame.mixer.init()
    except Exception as e:
        helpers.log(f"[DEBUG] Audio device init failed - audio disabled: {e}")
        return

    _initialized = True

    _load("switch", config.SFX_SWITCH_PATH)
    _load("bong", config.SFX_BONG_PATH)


def _load(key, relative_path):
    import pygame
    full_path = helpers.resource_path(relative_path)
    try:
        _sounds[key] = pygame.mixer.Sound(full_path)
    except Exception as e:
        helpers.log(f"[DEBUG] Failed to load sound '{key}' from {full_path}: {e}")
        _sounds[key] = None


def play_sfx(key):
    """Fire-and-forget playback - never raises, never blocks."""
    if not _initialized:
        return
    snd = _sounds.get(key)
    if snd is None:
        return
    try:
        snd.play()
    except Exception as e:
        helpers.log(f"[DEBUG] Failed to play sound '{key}': {e}")


def _ensure_music_loaded():
    global _music_loaded
    if _music_loaded:
        return True
    import pygame
    full_path = helpers.resource_path(MUSIC_RELATIVE_PATH)
    try:
        pygame.mixer.music.load(full_path)
        _music_loaded = True
        return True
    except Exception as e:
        helpers.log(f"[DEBUG] Failed to load background music from {full_path}: {e}")
        return False


def set_music_playing(should_play, fade_ms=1500):
    """The one entry point callers use for background music - idempotent, so it's safe
    to call every time something that might affect playback happens (window minimize/
    restore, a game launching/closing) without worrying about double-starting or
    restarting an already-fading track. Only acts when should_play actually differs
    from the current state."""
    global _music_playing_state
    if not _initialized:
        return
    if should_play == _music_playing_state:
        return
    try:
        import pygame
        if should_play:
            if _ensure_music_loaded():
                pygame.mixer.music.play(loops=-1, fade_ms=fade_ms)
                _music_playing_state = True
        else:
            pygame.mixer.music.fadeout(fade_ms)
            _music_playing_state = False
    except Exception as e:
        helpers.log(f"[DEBUG] Music playback state change failed: {e}")
