#!/usr/bin/env python3
"""MakeNightcore - turn any song into a nightcore version.

Nightcore is traditionally made by playing a song faster, like spinning a
record at a higher speed: tempo and pitch go up together. That is what this
tool does by default. The audio is resampled with a high-quality resampler,
so there are none of the "phasey" artifacts that time-stretching adds.

To set tempo and pitch independently, use --tempo and/or --pitch. The audio
is then time-stretched with Rubber Band (or ffmpeg's atempo as a fallback).

Only ffmpeg is required. Run with --help for usage, or see README.md.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import functools
import glob
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unicodedata
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

__version__ = "2.0.0"

DEFAULT_SPEED = 1.25
SPEED_RANGE = (0.1, 10.0)
PITCH_RANGE = (-36.0, 36.0)  # semitones
BASS_RANGE = (-24.0, 24.0)  # dB

SCRIPT_DIR = Path(__file__).resolve().parent
BUNDLED_RUBBERBAND_ZIP = SCRIPT_DIR / "rubberband-3.2.1-gpl-executable-windows.zip"

FORMATS = ("flac", "m4a", "mp3", "ogg", "opus", "wav")
COVER_ART_FORMATS = {".flac", ".m4a", ".mp3"}
# The channel layouts Opus can store.
OPUS_LAYOUTS = {"mono", "stereo", "3.0", "quad", "5.0", "5.1", "6.1", "7.1"}
# Layouts that Opus and AAC only take with their side channels relabelled as back ones.
SIDES_AS_BACK = {"5.0(side)": "5.0", "5.1(side)": "5.1"}

# Files picked up when a folder is given as input.
INPUT_EXTENSIONS = {f".{fmt}" for fmt in FORMATS} | {
    ".aac", ".aif", ".aifc", ".aiff", ".alac", ".ape", ".m4b", ".mka",
    ".mkv", ".mov", ".mp4", ".oga", ".tak", ".tta", ".webm", ".wma", ".wv",
}
LOSSLESS_CODECS = {"alac", "ape", "flac", "mlp", "mp4als", "ralf", "s302m", "shorten", "tak",
                   "truehd", "tta", "wavpack", "wmalossless"}
# Default output format for inputs whose own format can't be written back.
CODEC_EXTENSIONS = {"aac": ".m4a", "alac": ".m4a", "flac": ".flac", "mp3": ".mp3",
                    "opus": ".opus", "vorbis": ".ogg"}

# Tags that describe the original recording and would be wrong on the result.
STALE_TAGS = {
    "bps", "compatible_brands", "creation_time", "cuesheet", "duration", "encoder",
    "handler_name", "initialkey", "itunnorm", "itunsmpb", "key", "length",
    "major_brand", "minor_version", "number_of_bytes", "number_of_frames", "tkey",
    "tlen", "vendor_id",
}
STALE_TAG_PREFIXES = ("replaygain_", "r128_", "_statistics_")
BPM_TAGS = {"bpm", "tbpm", "tmpo"}
BPM_KEYS = {".flac": "BPM", ".m4a": "tmpo", ".mp3": "TBPM", ".ogg": "BPM", ".opus": "BPM"}
LYRICS_TAGS = ("lyrics", "unsyncedlyrics")

ENGINES = ("auto", "rubberband", "ffmpeg-rubberband", "atempo")

FFMPEG_HELP = """\
ffmpeg was not found. Install it and make sure it is on your PATH:
  Windows: winget install Gyan.FFmpeg   (or unzip a build from https://www.gyan.dev/ffmpeg/builds/
           next to MakeNightcore.py)
  macOS:   brew install ffmpeg
  Linux:   sudo apt install ffmpeg      (or your distribution's package manager)"""

RUBBERBAND_INSTALL = """\
  Windows: download it from https://breakfastquay.com/rubberband/, and add the folder
           with rubberband.exe and sndfile.dll to your PATH
  macOS:   brew install rubberband
  Linux:   sudo apt install rubberband-cli"""


class NightcoreError(Exception):
    """A problem to report to the user without a traceback."""


# --------------------------------------------------------------------------
# The effect
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Effect:
    """What to do to the audio.

    tempo:   playback speed multiplier (1.25 = 25% faster)
    pitch:   pitch shift in semitones
    bass:    low-shelf boost in dB (0 = off)
    stretch: False resamples, which ties pitch to tempo like a record player
             (classic nightcore); True time-stretches them independently.
    """

    tempo: float
    pitch: float
    bass: float = 0.0
    stretch: bool = False

    def __post_init__(self) -> None:
        _check_range("tempo", self.tempo, SPEED_RANGE)
        if self.stretch:
            _check_range("pitch", self.pitch, PITCH_RANGE)
        _check_range("bass", self.bass, BASS_RANGE)

    @classmethod
    def classic(cls, speed: float = DEFAULT_SPEED, bass: float = 0.0) -> Effect:
        """Speed up like a record player: pitch rises with the tempo."""
        _check_range("speed", speed, SPEED_RANGE)
        return cls(speed, 12 * math.log2(speed), bass, stretch=False)

    @classmethod
    def stretched(cls, tempo: float = 1.0, pitch: float = 0.0, bass: float = 0.0) -> Effect:
        """Change tempo and pitch independently."""
        return cls(tempo, pitch, bass, stretch=True)

    @property
    def label(self) -> str:
        """Suffix used for file names and titles."""
        return "Slowed" if self.tempo < 1 else "Nightcore"

    def describe(self) -> str:
        text = f"tempo x{self.tempo:g}, pitch {self.pitch:+.2f} semitones"
        if self.bass:
            text += f", bass {self.bass:+g} dB"
        return text


def make_effect(speed: float | None = None, tempo: float | None = None,
                pitch: float | None = None, bass: float = 0.0) -> Effect:
    """The effect for either a speed, or a tempo and/or pitch."""
    if speed is not None and (tempo is not None or pitch is not None):
        raise ValueError("use either speed, or tempo and/or pitch, not both")
    if tempo is None and pitch is None:
        return Effect.classic(DEFAULT_SPEED if speed is None else speed, bass)
    return Effect.stretched(1.0 if tempo is None else tempo, 0.0 if pitch is None else pitch, bass)


def _check_range(name: str, value: float, bounds: tuple) -> None:
    low, high = bounds
    if not low <= value <= high:  # also false for NaN
        raise ValueError(f"{name} must be between {low:g} and {high:g}, not {value!r}")


# --------------------------------------------------------------------------
# External programs
# --------------------------------------------------------------------------

def _search_dirs() -> list[Path]:
    """Places other than PATH where the programs may be."""
    dirs = [SCRIPT_DIR]
    for pattern in ("rubberband*", "ffmpeg*", "ffmpeg*/bin", "ffmpeg*/*/bin"):
        dirs += sorted(p for p in SCRIPT_DIR.glob(pattern) if p.is_dir())
    if os.name == "nt":
        dirs += sorted(p for p in _cache_dir().glob("rubberband*") if p.is_dir())
    return dirs


# Homebrew's complete ffmpeg build, which it keeps off PATH (Apple silicon, Intel).
FFMPEG_FULL_DIRS = ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin")


def _cache_dir() -> Path:
    """Where the bundled Rubber Band is unpacked (Windows only)."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local", "MakeNightcore")


def find_program(*names: str, near: str | None = None) -> str | None:
    """Find a program on PATH, or else next to `near` or next to this script."""
    for name in names:
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)  # (programs may run in another folder)
    extra = ([os.path.dirname(near)] if near else []) + [str(d) for d in _search_dirs()]
    for name in names:
        found = shutil.which(name, path=os.pathsep.join(extra))
        if found:
            return os.path.abspath(found)
    return None


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str

    @classmethod
    def find(cls) -> Tools:
        if sys.platform == "darwin":  # prefer Homebrew's complete build to its reduced one
            full = [shutil.which(name, path=os.pathsep.join(FFMPEG_FULL_DIRS))
                    for name in ("ffmpeg", "ffprobe")]
            if all(full):
                return cls(*full)
        ffmpeg = find_program("ffmpeg")
        if not ffmpeg:
            raise NightcoreError(FFMPEG_HELP)
        ffprobe = find_program("ffprobe", near=ffmpeg)
        if not ffprobe:
            raise NightcoreError(f"ffprobe, which comes with ffmpeg, was not found next to {ffmpeg}. "
                                 "Install the complete ffmpeg package.")
        return cls(ffmpeg, ffprobe)


def find_rubberband() -> str | None:
    """Find the Rubber Band program, unpacking the bundled copy on Windows."""
    found = find_program("rubberband", "rubberband-r3")
    if found is None and os.name == "nt" and BUNDLED_RUBBERBAND_ZIP.is_file():
        try:
            _unpack(BUNDLED_RUBBERBAND_ZIP, _cache_dir())
        except (OSError, zipfile.BadZipFile):
            return None
        found = find_program("rubberband", "rubberband-r3")
    return found


def _unpack(archive: Path, destination: Path) -> None:
    """Unpack a zip file's folders into `destination`, each in one step.

    That way an interrupted or concurrent first run can't leave half a copy.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with _work_folder(dir=destination) as unpacked:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(unpacked)
        for folder in unpacked.iterdir():
            try:
                os.replace(folder, destination / folder.name)
            except OSError:
                if not (destination / folder.name).is_dir():  # (unless another run was first)
                    raise


@contextlib.contextmanager
def _work_folder(**options):
    """A temporary folder. Unlike TemporaryDirectory, a file that can't be
    deleted yet (on Windows, while open) doesn't hide the real error."""
    folder = Path(tempfile.mkdtemp(**options))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _capture(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(cmd), stdin=subprocess.DEVNULL, capture_output=True)
    except OSError as error:
        raise NightcoreError(f"could not run {Path(cmd[0]).name}: {error.strerror or error}") from None


def _popen(cmd: Sequence[str], **kwargs) -> subprocess.Popen:
    try:
        return subprocess.Popen(list(cmd), stdin=subprocess.DEVNULL, **kwargs)
    except OSError as error:
        raise NightcoreError(f"could not run {Path(cmd[0]).name}: {error.strerror or error}") from None


@functools.lru_cache(maxsize=None)
def _ffmpeg_list(ffmpeg: str, what: str) -> frozenset:
    """The names ffmpeg lists for -filters or -encoders."""
    output = _capture([ffmpeg, "-hide_banner", what]).stdout.decode("utf-8", "replace")
    return frozenset(line.split()[1] for line in output.splitlines() if len(line.split()) > 2)


@functools.lru_cache(maxsize=None)
def _filter_options(ffmpeg: str, name: str) -> frozenset:
    output = _capture([ffmpeg, "-hide_banner", "-h", f"filter={name}"]).stdout.decode("utf-8", "replace")
    return frozenset(re.findall(r"^\s+-?(\w+)\s+<", output, re.MULTILINE))


@functools.lru_cache(maxsize=None)
def _has_soxr(ffmpeg: str) -> bool:
    """Whether ffmpeg was built with the SoX resampler (the best one)."""
    return _capture([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.01",
        "-af", "aresample=48000:resampler=soxr", "-f", "null", "-",
    ]).returncode == 0


@functools.lru_cache(maxsize=None)
def _rubberband_flags(rubberband: str) -> tuple:
    result = _capture([rubberband, "--full-help"])
    options = (result.stdout + result.stderr).decode("utf-8", "replace")
    flags = []
    if "--fine" in options:
        flags.append("--fine")  # the R3 engine, much better than the default R2
    if "--centre-focus" in options:
        flags.append("--centre-focus")  # keeps the stereo image (and mono playback) intact
    if "--ignore-clipping" in options:
        # Otherwise it restarts with less gain whenever the output clips.
        flags.append("--ignore-clipping")
    return tuple(flags)


def _run(cmd: Sequence[str], *, verbose: bool = False, duration: float | None = None,
         progress: Callable[[float], None] | None = None) -> None:
    """Run ffmpeg, raising NightcoreError with its message if it fails.

    Pass the expected output duration and a callback to receive progress.
    """
    cmd = [str(arg) for arg in cmd]
    if verbose:
        print("  $ " + _quote(cmd), flush=True)
    track = progress is not None and bool(duration)
    if track:
        cmd[1:1] = ["-progress", "pipe:1", "-nostats"]
    proc = _popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE if track else subprocess.DEVNULL)
    # Its messages are read on the side, so a chatty ffmpeg can't fill the pipe
    # and hang; only the last ones are kept.
    errors = collections.deque(maxlen=10)
    reader = threading.Thread(target=_drain, args=(proc.stderr, errors), daemon=True)
    reader.start()
    with _reaped(proc):
        if track:
            _follow_progress(proc.stdout, duration, progress)
        returncode = proc.wait()
        reader.join()
    if returncode != 0:
        _failed(cmd, returncode, [line.decode("utf-8", "replace") for line in errors])


def _drain(pipe, lines: collections.deque) -> None:
    with contextlib.suppress(OSError, ValueError):  # closed when the process is killed
        lines.extend(pipe)


def _follow_progress(lines: Iterable[bytes], duration: float,
                     progress: Callable[[float], None]) -> None:
    """Report the progress in ffmpeg's -progress output as a fraction."""
    key = None
    for line in lines:
        name, _, value = line.decode("ascii", "replace").strip().partition("=")
        # Both keys hold microseconds; older versions only have out_time_ms.
        if name in ("out_time_us", "out_time_ms") and key in (None, name):
            key = name
            if value.isdigit():
                progress(min(int(value) / 1e6 / duration, 1.0))


def _run_rubberband(cmd: Sequence[str], *, cwd: Path, verbose: bool = False,
                    progress: Callable[[float], None] | None = None) -> None:
    """Run the Rubber Band program, following the progress it prints."""
    cmd = [str(arg) for arg in cmd]
    if verbose:
        print(f"  $ cd {_quote([str(cwd)])} && {_quote(cmd)}", flush=True)
    proc = _popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    messages, token, current = [], bytearray(), 0
    with _reaped(proc):
        # It prints "Pass 1: Studying...", "\r0% \r1% ...", "Pass 2: Processing...", ...
        for char in iter(lambda: proc.stderr.read(1), b""):
            if char not in b"\r\n":
                token += char
                continue
            text = token.decode("utf-8", "replace").strip()
            token.clear()
            pass_number = re.match(r"Pass (\d+)", text)
            percent = re.fullmatch(r"(\d+)%", text)
            if pass_number:
                current = int(pass_number.group(1))
            if percent:
                if progress and current:
                    progress(min((current - 1 + int(percent.group(1)) / 100) / 2, 1.0))
            elif text:
                messages.append(text)
        returncode = proc.wait()
    if returncode != 0:
        _failed(cmd, returncode, messages)


@contextlib.contextmanager
def _reaped(proc: subprocess.Popen):
    """Kill the process if we are interrupted, and close its pipes."""
    try:
        yield
    except BaseException:
        if os.name == "nt" and proc.poll() is None:
            # Its children too: Chocolatey's ffmpeg.exe is a shim that runs the real one.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, capture_output=True)
        proc.kill()
        proc.wait()
        raise
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                pipe.close()


def _failed(cmd: Sequence[str], returncode: int, lines: Iterable[str]) -> None:
    lines = [line for line in (line.strip() for line in lines) if line]
    if not lines:  # ffmpeg's exit code is an error number when it can't say more
        reason = f" ({os.strerror(256 - returncode)})" if 128 < returncode < 256 else ""
        lines = [f"exit code {returncode}{reason}"]
    detail = "\n".join("  " + line for line in lines[-10:])
    raise NightcoreError(f"{Path(cmd[0]).stem} failed:\n{detail}")


def _quote(cmd: Sequence[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


# --------------------------------------------------------------------------
# Reading the source
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    path: Path
    codec: str
    sample_rate: int
    duration: float           # seconds, 0 if unknown
    bits: int                 # bit depth for lossless output: 16 or 24
    lossless: bool
    tags: dict
    floating: bool = False    # floating-point PCM
    channels: int = 2
    layout: str = ""          # channel layout, e.g. "5.1(side)"
    replaygain: bool = False  # ReplayGain in its tags or its header
    cover: int | None = None  # stream index of embedded cover art
    cover_codec: str = ""
    cover_type: str = ""      # e.g. "Cover (front)"


def probe(path: Path, tools: Tools) -> Source:
    result = _capture([tools.ffprobe, "-v", "error", "-print_format", "json",
                       "-show_format", "-show_streams", str(path)])
    if result.returncode != 0:
        lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = lines[-1] if lines else "unknown error"
        if reason.startswith(f"{path}: "):
            reason = reason[len(f"{path}: "):]
        raise NightcoreError(f"not an audio file ffmpeg can read ({reason})")
    info = json.loads(result.stdout.decode("utf-8", "replace"))
    streams = info.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None or not _number(audio.get("sample_rate")):
        raise NightcoreError("the file contains no audio")

    codec = audio.get("codec_name", "")
    lossless = codec.startswith("pcm_") or codec in LOSSLESS_CODECS
    is_float = codec.startswith("pcm_f")
    bits = int(_number(audio.get("bits_per_raw_sample")) or _number(audio.get("bits_per_sample"))
               or (24 if audio.get("sample_fmt") in ("s32", "s32p") else 0))
    pictures = [s for s in streams if s.get("codec_type") == "video"
                and s.get("disposition", {}).get("attached_pic")]
    fronts = [s for s in pictures if s.get("tags", {}).get("comment", "").lower() == "cover (front)"]
    picture = (fronts or pictures or [{}])[0]

    # Tags usually belong to the file. Ogg keeps them on the audio stream
    # instead; elsewhere, stream tags are technical (e.g. Matroska's stats).
    tags = info.get("format", {}).get("tags") or audio.get("tags") or {}
    channels = int(_number(audio.get("channels"))) or 2
    layout = audio.get("channel_layout", "unknown")
    if layout == "unknown":  # e.g. plain WAV and AIFF files
        layout = {1: "mono", 2: "stereo"}.get(channels, "")
    # A layout without a name, e.g. "2 channels (FC+LFE)", as old ffmpeg versions spell it.
    layout = re.sub(r"^\d+ channels \((.+)\)$", r"\1", layout)
    replaygain = (any(key.lower().startswith("replaygain_") for key in tags) or
                  any(d.get("side_data_type") == "Replay Gain" for d in audio.get("side_data_list", [])))

    return Source(
        path=path,
        codec=codec,
        sample_rate=int(_number(audio["sample_rate"])),
        duration=_number(info.get("format", {}).get("duration")) or _number(audio.get("duration")),
        bits=24 if lossless and (bits > 16 or is_float) else 16,
        lossless=lossless,
        tags=dict(tags),
        floating=is_float,
        channels=channels,
        layout=layout,
        replaygain=replaygain,
        cover=picture.get("index"),
        cover_codec=picture.get("codec_name", ""),
        cover_type=picture.get("tags", {}).get("comment", ""),
    )


def _number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def output_tags(source: Source, effect: Effect, ext: str) -> dict:
    """The source's tags, updated for the new version in the given format."""
    tags, bpm = {}, 0.0
    for key, value in source.tags.items():
        lower = key.lower()
        if lower in STALE_TAGS or lower.startswith(STALE_TAG_PREFIXES):
            continue
        if lower == "language" and str(value).lower() == "und":  # MP4's "undefined"
            continue
        if lower in BPM_TAGS:
            bpm = bpm or _number(value)
            continue
        if lower.startswith(LYRICS_TAGS):
            value = _retime_lyrics(str(value), effect.tempo)
        tags[key] = value
    if bpm and ext in BPM_KEYS:
        # Each format has its own name for it, and ffmpeg doesn't translate.
        tags[BPM_KEYS[ext]] = f"{bpm * effect.tempo:.0f}"
    title_key = next((key for key in tags if key.lower() == "title"), "title")
    # (A file name that isn't valid UTF-8 can't go into a tag as it is.)
    stem = os.fsencode(source.path.stem).decode("utf-8", "replace")
    title = str(tags.get(title_key, "")).strip() or stem
    suffix = f" ({effect.label})"
    tags[title_key] = title if title.endswith(suffix) else title + suffix
    return tags


def _retime_lyrics(text: str, tempo: float) -> str:
    """Move the [mm:ss.xx] timestamps of synced (LRC) lyrics to the new tempo."""
    def retime(match: re.Match) -> str:
        centiseconds = round((int(match.group(1)) * 60 + float(match.group(2))) / tempo * 100)
        minutes, centiseconds = divmod(centiseconds, 6000)
        return f"[{minutes:02d}:{centiseconds / 100:05.2f}]"
    return re.sub(r"\[(\d+):(\d\d(?:\.\d+)?)\]", retime, text)


def _ffmetadata(tags: dict) -> tuple[bytes, list[str]]:
    """Tags in ffmpeg's metadata file format, which has no length limits,
    plus -metadata arguments for the rare ones that format can't hold."""
    def escape(text: str) -> str:
        return re.sub(r"([=;#\\\n\r])", r"\\\1", text)
    lines, arguments = [";FFMETADATA1\n"], []
    for key, value in ((str(key), str(value)) for key, value in tags.items()):
        if value.endswith("\\"):  # its reader would run on into the next line
            arguments += ["-metadata", f"{key}={value}"]
        else:
            lines.append(f"{escape(key)}={escape(value)}\n")
    return "".join(lines).encode("utf-8", "replace"), arguments


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Codec:
    args: tuple           # ffmpeg encoder arguments
    lossless: bool = False
    floating: bool = False  # can store peaks above full scale
    opus: bool = False
    downmix: bool = False   # to stereo: the format can't store the source's channels
    sides_as_back: bool = False  # the format needs side channels relabelled as back ones


def output_codec(ext: str, source: Source, ffmpeg: str) -> Codec:
    """How to encode a format. The source's codec is kept where the format allows."""
    if ext == ".wav":
        pcm = "pcm_f32le" if source.floating else "pcm_s24le" if source.bits == 24 else "pcm_s16le"
        return Codec(("-c:a", pcm, "-rf64", "auto"), lossless=True, floating=source.floating)
    ogg_source = source.path.suffix.lower() in (".ogg", ".oga")
    if ext == ".flac" or (ext == ".ogg" and ogg_source and source.codec == "flac"):
        return Codec(("-c:a", "flac", "-compression_level", "8"), lossless=True)
    if ext == ".m4a" and source.codec == "alac":
        return Codec(("-c:a", "alac", "-movflags", "+faststart"), lossless=True)
    # Bitrates are per channel pair (256k/192k for stereo); VBR modes scale themselves.
    if ext == ".m4a":
        return Codec(("-c:a", "aac", "-b:a", f"{128 * source.channels}k", "-movflags", "+faststart"),
                     sides_as_back=True)
    encoders = _ffmpeg_list(ffmpeg, "-encoders")
    if ext == ".mp3":
        codec = Codec(("-c:a", "libmp3lame", "-q:a", "0"), downmix=source.channels > 2)  # LAME V0
    elif ext == ".ogg" and not (ogg_source and source.codec == "opus") and "libvorbis" in encoders:
        codec = Codec(("-c:a", "libvorbis", "-q:a", "6"))
    else:  # .opus, and .ogg when the source is Opus or ffmpeg lacks Vorbis
        downmix = source.layout not in OPUS_LAYOUTS and source.layout not in SIDES_AS_BACK
        bitrate = 96 * (2 if downmix else source.channels)
        codec = Codec(("-c:a", "libopus", "-b:a", f"{bitrate}k"), opus=True, downmix=downmix,
                      sides_as_back=True)
    if codec.args[1] not in encoders:
        raise NightcoreError(f"this ffmpeg can't write {ext[1:]} files "
                             f"(it has no {codec.args[1]} encoder); choose another format with -f")
    return codec


def choose_engine(effect: Effect, requested: str, tools: Tools) -> tuple[str, str | None]:
    """Pick how to process the audio. Returns (engine, rubberband_path)."""
    if not effect.stretch:
        return "resample", None
    if requested in ("auto", "rubberband"):
        rubberband = find_rubberband()
        if rubberband:
            return "rubberband", rubberband
        if requested == "rubberband":
            raise NightcoreError("Rubber Band was not found. To install it:\n" + RUBBERBAND_INSTALL)
    if requested in ("auto", "ffmpeg-rubberband"):
        if "rubberband" in _ffmpeg_list(tools.ffmpeg, "-filters"):
            return "ffmpeg-rubberband", None
        if requested == "ffmpeg-rubberband":
            raise NightcoreError("this ffmpeg was built without the rubberband filter")
    return "atempo", None


def _output_rate(rate: int, codec: Codec) -> int:
    if codec.opus:
        return 48000  # Opus only works at 48 kHz
    if codec.lossless or rate <= 48000:
        return rate
    return 48000 if rate % 48000 == 0 else 44100  # lossy codecs gain nothing above 48 kHz


def _resample(rate: int, soxr: bool, from_rate: int) -> str:
    if soxr:
        return f"aresample={rate}:resampler=soxr:precision=28"
    # ffmpeg's own resampler, filtering below the lower of the two Nyquist
    # frequencies, so nothing aliases (speeding up) or images (slowing down).
    cutoff = 0.94 * min(1.0, from_rate / rate)
    return f"aresample={rate}:filter_size=128:phase_shift=14:cutoff={cutoff:.6f}"


def _atempo(factor: float) -> list[str]:
    """atempo filters for any factor (old ffmpeg versions only allow 0.5-2 each)."""
    steps = []
    while factor > 2:
        steps.append(2.0)
        factor /= 2
    while factor < 0.5:
        steps.append(0.5)
        factor /= 0.5
    if abs(factor - 1) > 1e-9 or not steps:
        steps.append(factor)
    return [f"atempo={step:.8f}" for step in steps]


def audio_filters(source: Source, effect: Effect, engine: str, codec: Codec, ffmpeg: str) -> list[str]:
    """The ffmpeg filter chain that applies the effect and prepares the output."""
    soxr = _has_soxr(ffmpeg)
    rate = _output_rate(source.sample_rate, codec)
    ratio = 2 ** (effect.pitch / 12)  # the pitch change as a frequency ratio
    chain = ["aformat=sample_fmts=fltp"]  # process in floating point: no clipping along the way
    if engine == "rubberband" and source.layout:
        # The Rubber Band program worked on a WAV file, which lost the channel layout.
        chain.insert(0, f"channelmap=channel_layout={source.layout}")
    if codec.downmix:
        # Normalized, as ffmpeg's own downmix of floating-point audio can go far
        # over full scale, and first, so the limiter sees what is written.
        chain += ["aresample=rematrix_maxval=1", "aformat=channel_layouts=stereo"]
    elif codec.sides_as_back and source.layout in SIDES_AS_BACK:
        chain.append(f"channelmap=channel_layout={SIDES_AS_BACK[source.layout]}")

    # asetrate sets a rate, not a ratio: a later part of the file at another
    # sample rate (joined MP3s, say) must be converted to the first one's.
    same_rate = _resample(source.sample_rate, soxr, source.sample_rate)
    if engine == "resample":
        # Play the samples back faster, then convert to a standard sample rate.
        faster = round(source.sample_rate * effect.tempo)
        chain += [same_rate, f"asetrate={faster}", _resample(rate, soxr, faster)]
    elif engine == "atempo":
        if ratio != 1:
            higher = round(source.sample_rate * ratio)
            chain += [same_rate, f"asetrate={higher}", _resample(rate, soxr, higher)]
        elif rate != source.sample_rate:
            chain.append(_resample(rate, soxr, source.sample_rate))
        chain += _atempo(effect.tempo / ratio)
    else:
        if engine == "ffmpeg-rubberband":
            chain.append(f"rubberband=tempo={effect.tempo:.8f}:pitch={ratio:.8f}"
                         ":pitchq=quality:channels=together")  # together: keeps the stereo image
        else:
            chain.append("volume=4")  # it was given 12 dB of headroom
        if rate != source.sample_rate:
            chain.append(_resample(rate, soxr, source.sample_rate))
    if effect.bass:
        chain += ["aformat=sample_fmts=dblp", f"bass=g={effect.bass:g}"]  # double: no added noise
    # Keep peaks from going over full scale, where integer formats clip them.
    # Resampling can create small overs between the original samples. Boosting
    # and phase-vocoder time-stretching create big ones, and lossy codecs then
    # overshoot by up to 2 dB more, so those get the limiter whatever the format.
    big_overs = effect.bass or engine in ("rubberband", "ffmpeg-rubberband")
    if big_overs or (codec.lossless and not codec.floating):
        chain.append(_limiter(ffmpeg, 0.989 if codec.lossless else 0.794, rate))
    if codec.lossless and source.bits == 16:
        chain.append("aresample=osf=s16:dither_method=triangular")
    return chain


def _limiter(ffmpeg: str, ceiling: float, rate: int) -> str:
    """A peak limiter that keeps the level below `ceiling` (a sample value)."""
    options = _filter_options(ffmpeg, "alimiter")
    limiter = f"alimiter=limit={ceiling}:attack=5"
    if "level" in options:
        limiter += ":level=0"  # don't also normalize the volume
    if "latency" in options:
        return limiter + ":latency=1"  # keep the audio in sync
    # Before ffmpeg 5.1 it delays the audio by its 5 ms attack, less a sample.
    delay = int(rate * 0.005) - 1
    return f"apad=pad_len={delay},{limiter},atrim=start_sample={delay},asetpts=PTS-STARTPTS"


def _decode(source: Source, output: Path, tools: Tools, verbose: bool,
            progress: Callable[[float], None] | None, volume: float = 1) -> Path:
    """Decode the audio to a floating-point WAV file."""
    _run([tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
          "-i", source.path, "-map", "0:a:0", "-map_metadata", "-1", "-af", f"volume={volume:g}",
          "-c:a", "pcm_f32le", "-rf64", "auto", output],
         verbose=verbose, duration=source.duration, progress=progress)
    return output


def _stretch_with_rubberband(source: Source, effect: Effect, rubberband: str, workdir: Path,
                             tools: Tools, verbose: bool, steps: _Progress) -> Path:
    """Time-stretch with the Rubber Band program, which only reads WAV files."""
    # 12 dB of headroom: time-stretched audio peaks higher than the source.
    decoded = _decode(source, workdir / "decoded.wav", tools, verbose, steps.stage(0.05), 0.25)
    stretched = workdir / "stretched.wav"
    # File names relative to the work folder: on Windows, Rubber Band can only
    # open paths that fit the system code page, and the temp folder may not.
    _run_rubberband([rubberband, *_rubberband_flags(rubberband),
                     "--tempo", f"{effect.tempo:.8f}", "--pitch", f"{effect.pitch:.8f}",
                     decoded.name, stretched.name],
                    cwd=workdir, verbose=verbose, progress=steps.stage(0.85))
    # It reports success even when it couldn't write it all (a full disk).
    if _duration(stretched, tools) < 0.99 * _duration(decoded, tools) / effect.tempo:
        raise NightcoreError(f"Rubber Band stopped early (is the temp folder {workdir.parent} full?)")
    return stretched


class _Progress:
    """Splits one progress report (0 to 1) across several steps."""

    def __init__(self, report: Callable[[float], None] | None):
        self.report = report
        self.done = 0.0

    def stage(self, share: float) -> Callable[[float], None] | None:
        if self.report is None:
            return None
        start, self.done = self.done, self.done + share
        return lambda fraction: self.report(start + share * fraction)


def render(source: Source, output: Path, effect: Effect, engine: str, tools: Tools, *,
           rubberband: str | None = None, overwrite: bool = False, verbose: bool = False,
           progress: Callable[[float], None] | None = None) -> list[str]:
    """Write the effected audio to `output`. Returns any warnings."""
    if engine != "resample" and 0 < source.duration < 0.1:
        raise NightcoreError("the audio is too short to time-stretch (under 0.1 seconds)")
    ext = output.suffix.lower()
    codec = output_codec(ext, source, tools.ffmpeg)
    notes = []
    steps = _Progress(progress)

    # Write next to the destination first, so a failed or interrupted run
    # never leaves a half-written file behind under the real name. (And find
    # out now, not after a long stretch, if the folder can't be written to.)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=output.parent, prefix=".nightcore-", suffix=ext)
    except OSError as error:
        raise NightcoreError(f"can't write to {output.parent}: {error.strerror}") from None
    os.close(fd)
    partial = Path(name)
    partial.unlink()  # let ffmpeg create it, with normal permissions
    try:
        with _work_folder(prefix="nightcore-") as workdir:
            audio = source.path
            if engine == "rubberband":
                audio = _stretch_with_rubberband(source, effect, rubberband, workdir,
                                                 tools, verbose, steps)
            elif ext == ".mp3" and source.replaygain:
                # ffmpeg passes the source's ReplayGain on to the MP3 encoder, which
                # writes it (now wrong) into the file's header. A decoded copy has none.
                audio = _decode(source, workdir / "decoded.wav", tools, verbose,
                                steps.stage(0.1))
            tags = workdir / "tags.txt"
            tag_file, tag_arguments = _ffmetadata(output_tags(source, effect, ext))
            tags.write_bytes(tag_file)
            filters = audio_filters(source, effect, engine, codec, tools.ffmpeg)
            duration = source.duration / effect.tempo
            encoding = steps.stage(1 - steps.done)
            cover = source.cover if ext in COVER_ART_FORMATS else None
            while True:
                cmd = _encode_command(source, audio, tags, tag_arguments, filters, codec, cover,
                                      partial, tools)
                try:
                    _run(cmd, verbose=verbose, duration=duration, progress=encoding)
                    break
                except NightcoreError:
                    # A picture the format can't take fails before anything is
                    # written. Then it's worth trying again without it.
                    if cover is None or (partial.exists() and partial.stat().st_size > 0):
                        raise
                    notes.append(f"the cover art ({source.cover_codec}) can't be stored "
                                 f"in {ext[1:]} files, so it was left out")
                    cover = None
        if _duration(partial, tools) <= 0:
            raise NightcoreError("the result contains no audio (is the input too short?)")
        _publish(partial, output, overwrite)
    finally:
        with contextlib.suppress(OSError):
            partial.unlink()
    return notes


def _publish(partial: Path, output: Path, overwrite: bool) -> None:
    """Move the finished file into place, or copy it there by a hard link.

    A link can't replace a file, so without `overwrite`, a file that another
    run made in the meantime is left alone. (The caller removes `partial`.)
    """
    if overwrite:
        os.replace(partial, output)
        return
    exists = NightcoreError(f"{output.name} already exists (use -y to overwrite)")
    try:
        os.link(partial, output)
    except FileExistsError:
        raise exists from None
    except OSError:  # a file system without hard links, e.g. FAT
        if output.exists():
            raise exists from None
        os.replace(partial, output)


def _encode_command(source: Source, audio: Path, tags: Path, tag_arguments: list[str],
                    filters: list[str], codec: Codec, cover: int | None, output: Path,
                    tools: Tools) -> list[str]:
    cmd = [tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio)]
    original = 0
    if audio != source.path:
        cmd += ["-i", str(source.path)]
        original = 1
    cmd += ["-f", "ffmetadata", "-i", str(tags)]
    cmd += ["-filter_complex", f"[0:a:0]{','.join(filters)}[audio]", "-map", "[audio]"]
    if cover is not None:
        cmd += ["-map", f"{original}:{cover}", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        if not source.cover_type:
            cmd += ["-metadata:s:v:0", "comment=Cover (front)"]
    # Chapters would point at the wrong times after a tempo change.
    cmd += ["-map_metadata", str(original + 1), *tag_arguments, "-map_chapters", "-1",
            *codec.args, str(output)]
    return cmd


def _duration(path: Path, tools: Tools) -> float:
    """The length of an audio file in seconds; 0 if it has no audio."""
    result = _capture([tools.ffprobe, "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return _number(result.stdout.decode("ascii", "replace").strip()) if result.returncode == 0 else 0.0


# --------------------------------------------------------------------------
# Where the output goes
# --------------------------------------------------------------------------

def _default_extension(source: Source, fmt: str | None) -> str:
    """`fmt`'s extension, or else the source's own, when it can be written back."""
    if fmt:
        return "." + fmt
    ext = source.path.suffix.lower()
    if ext[1:] in FORMATS:
        return ext
    return CODEC_EXTENSIONS.get(source.codec, ".flac" if source.lossless else ".mp3")


def default_output(source: Source, directory: Path, fmt: str | None, effect: Effect) -> Path:
    """'<name> (Nightcore).<ext>', keeping the source's format when possible."""
    stem, suffix = source.path.stem, f" ({effect.label}){_default_extension(source, fmt)}"
    while len(os.fsencode(stem + suffix)) > 255 and len(stem) > 1:
        stem = stem[:-1]  # file names can't be longer than 255 bytes
    return directory / (stem.rstrip() + suffix)


def plan_output(source: Source, output: Path | None, fmt: str | None, effect: Effect,
                folder: bool = False) -> Path:
    """The output file: `output` itself, or a default name in the `output` folder.

    An `output` without an extension gets the one the default name would have.
    """
    if output is None or folder:
        return default_output(source, output or source.path.parent, fmt, effect)
    if not _check_extension(output, fmt):
        output = output.with_name(output.name + _default_extension(source, fmt))
    return output


def _extension(path: Path) -> str:
    """The file extension, if it looks like one ('Song ft. X' and 'Vol.2' have none)."""
    return path.suffix.lower() if re.fullmatch(r"\.(?!\d+$)\w{1,5}", path.suffix) else ""


def _check_extension(output: Path, fmt: str | None) -> str:
    """The extension of an output file ('' if none), which must be a format we write."""
    ext = _extension(output)
    if ext and ext[1:] not in FORMATS:
        raise NightcoreError(f"unsupported output format {ext!r} (choose from {', '.join(FORMATS)})")
    if ext and fmt and ext[1:] != fmt:
        raise NightcoreError(f"the format {fmt} does not match the output file {output.name}")
    return ext


def _is_folder(name: str | os.PathLike) -> bool:
    """Whether an output names a folder: an existing one, or one ending in a slash."""
    return os.path.isdir(name) or str(name).endswith(("/", os.sep))


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _check_target(source: Path, target: Path, overwrite: bool) -> None:
    if target == source or _same_file(target, source):
        raise NightcoreError("the output would replace the source file")
    if target.is_dir():
        raise NightcoreError(f"the output {target} is a folder")
    if target.exists() and not overwrite:
        raise NightcoreError(f"{target.name} already exists (use -y to overwrite)")


def nightcore(source: str | os.PathLike, output: str | os.PathLike | None = None, *,
              speed: float | None = None, tempo: float | None = None,
              pitch: float | None = None, bass: float = 0.0, format: str | None = None,
              engine: str = "auto", overwrite: bool = False) -> Path:
    """Make a nightcore version of `source` and return the output path.

    With no options, this speeds the song up by 1.25x like a record player.
    Pass `tempo` and/or `pitch` (semitones) instead of `speed` to change them
    independently. `output` is a file or a folder, like the command line's -o;
    by default the result is "<name> (Nightcore).<ext>" next to the source.
    """
    effect = make_effect(speed, tempo, pitch, bass)
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {', '.join(ENGINES)}")
    if engine != "auto" and not effect.stretch:
        raise ValueError("engine only applies when changing tempo and/or pitch")
    fmt = None if format is None else format.lower().lstrip(".")
    if fmt is not None and fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")

    tools = Tools.find()
    source_path = Path(os.path.abspath(source))
    info = probe(source_path, tools)
    folder = output is not None and _is_folder(output)
    target = plan_output(info, output and Path(os.path.abspath(output)), fmt, effect, folder)
    _check_target(source_path, target, overwrite)
    chosen, rubberband = choose_engine(effect, engine, tools)
    for message in render(info, target, effect, chosen, tools, rubberband=rubberband,
                          overwrite=overwrite):
        warnings.warn(message, stacklevel=2)
    return target


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

LABELS = (" (Nightcore)", " (Slowed)")


def _is_generated(path: Path) -> bool:
    return path.stem.endswith(LABELS)


def _skip_earlier_results(files: list[Path]) -> tuple[list[Path], list[Path]]:
    """Leave out earlier results that are given along with their originals
    (as when running `*.mp3` again). Returns (files to do, files left out)."""
    def key(path: Path, stem: str) -> str:
        return os.path.normcase(os.path.abspath(path.with_name(stem)))
    stems = {key(path, path.stem) for path in files}
    skipped = [path for path in files if _is_generated(path) and
               key(path, next(path.stem[:-len(label)] for label in LABELS
                              if path.stem.endswith(label))) in stems]
    return [path for path in files if path not in skipped], skipped


def _file_id(path: Path) -> tuple | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def expand_inputs(names: Iterable[str]) -> tuple[list[Path], list[str]]:
    """Files to process (folders are scanned for audio), plus any errors."""
    files, errors = [], []
    for name in names:
        path = Path(name)
        try:
            if path.is_dir():
                found = [p for p in sorted(path.iterdir())
                         if p.is_file() and p.suffix.lower() in INPUT_EXTENSIONS
                         and not p.name.startswith(".") and not _is_generated(p)]
                if not found:
                    errors.append(f"{name}: no audio files in this folder "
                                  "(subfolders are not searched)")
                files += found
            elif path.is_file():
                files.append(path)
            elif "*" in name or "?" in name:
                # Windows shells leave wildcards like *.mp3 for the program to expand.
                # Only * and ? count, as there: folder names often have [brackets].
                matches = [Path(match) for match in sorted(glob.glob(name.replace("[", "[[]")))
                           if os.path.isfile(match)]
                if not matches:
                    errors.append(f"{name}: no files match")
                files += matches
            else:
                errors.append(f"{name}: no such file or folder")
        except OSError as error:
            errors.append(f"{name}: {_describe(error)}")
    return files, errors


def _number_in(bounds: tuple) -> Callable[[str], float]:
    low, high = bounds

    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
        if not low <= value <= high:  # also false for NaN
            raise argparse.ArgumentTypeError(f"{text} is out of range ({low:g} to {high:g})")
        return value
    return parse


def _format(text: str) -> str:
    fmt = text.lower().lstrip(".")
    if fmt not in FORMATS:
        raise argparse.ArgumentTypeError(f"{text!r} is not supported (choose from {', '.join(FORMATS)})")
    return fmt


class _HelpFormatter(argparse.HelpFormatter):
    """Wraps descriptions as usual, but keeps the layout of the examples."""

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        if "\n" in text:
            return "".join(indent + line for line in text.splitlines(keepends=True))
        return super()._fill_text(text, width, indent)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Make nightcore versions of songs: faster and higher, like a record "
                    "played at the wrong speed.",
        epilog="examples:\n"
               "  %(prog)s song.mp3                        -> 'song (Nightcore).mp3'\n"
               "  %(prog)s song.mp3 --speed 1.35           faster and higher\n"
               "  %(prog)s song.mp3 --bass 6 -f flac       with a bass boost, as FLAC\n"
               "  %(prog)s *.mp3 -o nightcore/             several songs into a folder\n"
               "  %(prog)s song.mp3 --tempo 1.2 --pitch 2  tempo and pitch separately\n"
               "  %(prog)s song.mp3 --speed 0.8            slowed down instead",
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", metavar="INPUT",
                        help="audio files, or folders of audio files")
    parser.add_argument("-s", "--source", action="append", default=[], metavar="INPUT",
                        help="input file (same as giving it without -s)")
    parser.add_argument("-o", "--output", metavar="PATH",
                        help="output file or folder; it is a folder if it exists as one, ends "
                             "in '/', or there are several inputs (default: '<name> "
                             "(Nightcore).<ext>' next to each input)")
    parser.add_argument("-f", "--format", type=_format, metavar="FORMAT",
                        help=f"output format: {', '.join(FORMATS)} "
                             "(default: the input's format where possible)")

    effect = parser.add_argument_group(
        "effect",
        "By default the song is sped up by 1.25x with the pitch rising along with it, "
        "which is what nightcore traditionally is. Use --tempo and/or --pitch "
        "instead to change them independently (time-stretching).")
    effect.add_argument("--speed", type=_number_in(SPEED_RANGE), metavar="X",
                        help=f"speed-up factor; pitch rises with it (default: {DEFAULT_SPEED})")
    effect.add_argument("--tempo", type=_number_in(SPEED_RANGE), metavar="X",
                        help="tempo factor, without changing the pitch (default: 1)")
    effect.add_argument("--pitch", type=_number_in(PITCH_RANGE), metavar="SEMITONES",
                        help="pitch shift in semitones, without changing the tempo (default: 0)")
    effect.add_argument("--bass", type=_number_in(BASS_RANGE), default=0.0, metavar="DB",
                        help="bass boost in dB, e.g. 6 (default: off)")
    effect.add_argument("--engine", choices=ENGINES, default="auto",
                        help="time-stretcher for --tempo/--pitch: the Rubber Band program, "
                             "ffmpeg's rubberband filter, or ffmpeg's lower-quality atempo "
                             "(default: best available)")

    parser.add_argument("-y", "--overwrite", action="store_true",
                        help="overwrite existing output files")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only print errors and warnings")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show the commands being run")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _display_width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _fit(text: str, width: int) -> str:
    """Shorten text to a display width by cutting out its middle."""
    if _display_width(text) <= width:
        return text
    budget = max(width - 3, 2)
    head, tail = "", ""
    for char in text:
        if _display_width(head + char) > budget - budget // 2:
            break
        head += char
    for char in reversed(text):
        if _display_width(char + tail) > budget // 2:
            break
        tail = char + tail
    return head + "..." + tail


class _Status:
    """One line per file, with a live percentage when writing to a terminal."""

    def __init__(self, quiet: bool, verbose: bool):
        self.quiet = quiet
        self.live = not (quiet or verbose) and bool(sys.stdout) and sys.stdout.isatty()
        self.text = self.shown = ""

    def info(self, text: str) -> None:
        if not self.quiet:
            _say(text)

    def start(self, text: str) -> None:
        self.text, self.shown = text, ""
        if self.live:
            self._show("...")
        else:
            self.info(text)

    def progress(self, fraction: float) -> None:
        self._show(f"{fraction:4.0%}")

    def finish(self, ok: bool) -> None:
        if self.live:
            clear = "\r" + " " * _display_width(self.shown) + "\r"
            _say(f"{clear}{self.text}  {'done' if ok else 'FAILED'}")

    def _show(self, state: str) -> None:
        # Stay on one line: a wrapped line can't be redrawn with "\r".
        width = shutil.get_terminal_size().columns - 1
        line = f"{_fit(self.text, width - len(state) - 2)}  {state}"
        if line != self.shown:
            padding = " " * max(0, _display_width(self.shown) - _display_width(line))
            _say(f"\r{line}{padding}", end="")
            self.shown = line


def _say(text: str, end: str = "\n") -> None:
    """Print a progress message. A closed pipe (e.g. `| head`) doesn't stop the work."""
    try:
        print(text, end=end, flush=True)
    except BrokenPipeError:
        with contextlib.suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def _stop(signum, frame):
    raise KeyboardInterrupt


@contextlib.contextmanager
def _stoppable():
    """Clean up on termination signals like on Ctrl+C: on macOS and Linux, closing
    the terminal and kill. (On Windows, closing the console can't be caught.)"""
    previous = {}
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            with contextlib.suppress(ValueError, OSError):  # not in the main thread
                previous[name] = signal.signal(getattr(signal, name), _stop)
    try:
        yield
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)


def _describe(error: Exception) -> str:
    if isinstance(error, OSError) and error.strerror:
        # For a failed rename, the second file name is the one the user knows.
        filename = error.filename2 or error.filename
        return f"{error.strerror}: {filename}" if filename else error.strerror
    return str(error)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # never crash printing an odd file name
            stream.reconfigure(errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    names = list(args.inputs) + list(args.source)
    if not names:
        parser.error("no input files given")
    stretch = args.tempo is not None or args.pitch is not None
    if args.speed is not None and stretch:
        parser.error("use either --speed, or --tempo and/or --pitch, not both")
    if args.engine != "auto" and not stretch:
        parser.error("--engine only applies when using --tempo and/or --pitch")
    effect = make_effect(args.speed, args.tempo, args.pitch, args.bass)

    def fail(message: str) -> None:
        print(f"error: {message}", file=sys.stderr, flush=True)

    files, errors = expand_inputs(names)
    for message in errors:
        fail(message)
    files, skipped = _skip_earlier_results(files)
    if not files:
        return 1 if errors or not skipped else 0

    # The output is a file, or a folder: a named one, or when there are several inputs.
    output, folder = None, False
    if args.output:
        output = Path(os.path.abspath(args.output))
        named_folder = _is_folder(args.output)
        folder = named_folder or len(files) > 1 or any(map(os.path.isdir, names))
        is_file = os.path.isfile(output)
        if folder and not named_folder and _extension(output)[1:] in FORMATS:
            parser.error(f"--output {args.output} looks like a file, but there are several "
                         "inputs; give a folder (ending in '/')")
        if folder and is_file:
            parser.error(f"--output {args.output} is a file, not a folder")
        if not folder:
            try:
                _check_extension(output, args.format)
            except NightcoreError as error:
                parser.error(str(error))

    try:
        tools = Tools.find()
        engine, rubberband = choose_engine(effect, args.engine, tools)
    except NightcoreError as error:
        fail(str(error))
        return 1

    status = _Status(args.quiet, args.verbose)
    how = {"resample": "resampled", "rubberband": "time-stretched with Rubber Band",
           "ffmpeg-rubberband": "time-stretched with ffmpeg's rubberband filter",
           "atempo": "time-stretched with ffmpeg's atempo"}[engine]
    status.info(f"{effect.label}: {effect.describe()}, {how}")
    if engine == "atempo" and args.engine == "auto":
        print("warning: Rubber Band was not found, so the lower-quality atempo filter is used. "
              "To install Rubber Band:\n" + RUBBERBAND_INSTALL, file=sys.stderr)
    for path in skipped:
        status.info(f"skipping {path.name}: an earlier result")

    done, failed = 0, len(errors)
    planned, written = set(), set()
    inputs = {_file_id(path) for path in files} - {None}
    with _stoppable():
        try:
            for number, path in enumerate(files, 1):
                prefix = f"[{number}/{len(files)}] " if len(files) > 1 else ""
                source_path = Path(os.path.abspath(path))
                try:
                    source = probe(source_path, tools)
                    target = plan_output(source, output, args.format, effect, folder)
                    key, target_id = os.path.normcase(str(target)), _file_id(target)
                    if key in planned or (target_id and target_id in written):
                        raise NightcoreError(f"{target.name} would be written twice "
                                             "(two inputs have the same name)")
                    planned.add(key)
                    _check_target(source_path, target, args.overwrite)
                    if target_id in inputs:
                        raise NightcoreError(f"{target.name} is one of the inputs, "
                                             "so it won't be overwritten")
                    status.start(f"{prefix}{path.name} -> {target.name}")
                    try:
                        notes = render(source, target, effect, engine, tools,
                                       rubberband=rubberband, overwrite=args.overwrite,
                                       verbose=args.verbose,
                                       progress=status.progress if status.live else None)
                    except BaseException:
                        status.finish(False)
                        raise
                    status.finish(True)
                    written.add(_file_id(target))
                    for note in notes:
                        print(f"warning: {path.name}: {note}", file=sys.stderr)
                    done += 1
                except (NightcoreError, OSError) as error:
                    fail(f"{path.name}: {_describe(error)}")
                    failed += 1
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130

    if len(files) > 1 or errors:
        status.info(f"{done} done, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
