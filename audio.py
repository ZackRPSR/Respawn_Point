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

# User-facing on/off preference (the "Music On" toggle), separate from
# _music_playing_state (which tracks whether the track is *actually* audible right now).
# Kept separate so the automatic minimize/game-launch logic in set_music_playing() and the
# manual toggle never fight each other: set_music_playing() always records the last
# window/game-driven desire (_last_should_play) even while the user has music switched
# off, so flipping the toggle back on immediately resumes the correct state with no extra
# wiring needed on the caller's side. In-memory only - resets to on each launch, same as
# every other runtime UI state in this app (no settings-persistence layer exists yet).
_music_user_enabled = True
_last_should_play = False


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
    restarting an already-fading track. Only acts when the *effective* desired state
    (should_play AND the user's Music On/Off toggle) actually differs from what's
    currently playing.

    should_play always reflects the window/game-launch logic in main.py regardless of
    the toggle - it's recorded as _last_should_play so set_music_enabled() below can
    re-derive the correct playing state the instant the user flips the toggle, without
    main.py having to recompute or resend its window/game state."""
    global _music_playing_state, _last_should_play
    if not _initialized:
        return
    _last_should_play = should_play
    effective_should_play = should_play and _music_user_enabled
    if effective_should_play == _music_playing_state:
        return
    try:
        import pygame
        if effective_should_play:
            if _ensure_music_loaded():
                pygame.mixer.music.play(loops=-1, fade_ms=fade_ms)
                _music_playing_state = True
        else:
            pygame.mixer.music.fadeout(fade_ms)
            _music_playing_state = False
    except Exception as e:
        helpers.log(f"[DEBUG] Music playback state change failed: {e}")


def is_music_enabled():
    """Current state of the user's Music On/Off toggle (not whether a track happens to
    be audible right now - e.g. it stays True while minimized, since minimizing fades
    the track out for a reason unrelated to the toggle)."""
    return _music_user_enabled


def is_music_actually_playing():
    """Whether the track is audible right now, independent of the toggle - e.g. the
    toggle can be On while this is False because a game is running or the window is
    minimized. This is what the popup's "Music: Playing/Paused" status line reflects."""
    return _music_playing_state


def set_music_enabled(enabled, fade_ms=1500):
    """Called by the toggle switch in the UI. Flips the user preference, then re-runs
    set_music_playing() against the last known window/game desire so the track
    fades in or out immediately - reusing that function's existing fade logic rather
    than duplicating it here."""
    global _music_user_enabled
    if enabled == _music_user_enabled:
        return
    _music_user_enabled = enabled
    set_music_playing(_last_should_play, fade_ms=fade_ms)
