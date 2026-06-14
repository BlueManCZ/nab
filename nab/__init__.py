__version__ = "0.1.0"
APP_ID = "io.github.bluemancz.nab"

VIDEO_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v", ".flv", ".ts",
})
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".flac", ".ogg", ".opus", ".wav",
})
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | frozenset({".m3u8"})
