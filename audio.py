"""Minimal sound-effect layer for Respawn Point. Uses pygame.mixer instead of winsound
because it needs to overlap sounds (e.g. a bong on a button press while a switch sound
from a moment ago is still tailing off) - winsound is blocking and single-channel, so
overlapping playback would cut sounds off or freeze the UI thread.

Only two sound effects exist right now: "switch" (sidebar icon navigation + recent-games/
carousel card switching) and "bong" (every other action button - launch, settings, and
the add-game/settings/backup form buttons). No background music, no hover sounds - kept
deliberately minimal per design decision.

Every public function here is exception-safe: a missing pygame install, a missing sound
file, or a dead audio device should never crash or freeze the launcher. Worst case, sounds
just silently don't play and a [DEBUG] line gets logged.
"""

import helpers
import config

_initialized = False
_sounds = {}


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
