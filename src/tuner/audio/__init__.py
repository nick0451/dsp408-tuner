"""Host-agnostic audio I/O over PortAudio."""

from .io import LoopbackConfig, play_record

__all__ = ["LoopbackConfig", "play_record"]
