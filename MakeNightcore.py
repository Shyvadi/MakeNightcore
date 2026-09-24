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
import contextlib
import functools
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

__version__ = "2.0.0"

DEFAULT_SPEED = 1.25

SCRIPT_DIR = Path(__file__).resolve().parent
BUNDLED_RUBBERBAND_ZIP = SCRIPT_DIR / "rubberband-3.2.1-gpl-executable-windows.zip"

# Output format (file extension) -> ffmpeg encoder arguments. The settings
# are all transparent, so the only audible change is the effect itself.
ENCODERS = {
    ".mp3": ["-c:a", "libmp3lame", "-q:a", "0"],  # LAME V0
    ".m4a": ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    ".opus": ["-c:a", "libopus", "-b:a", "192k"],
    ".flac": ["-c:a", "flac", "-compression_level", "8"],
    ".wav": [],  # PCM; the bit depth follows the source (see _encoder_args)
}
FORMATS = sorted(ext[1:] for ext in ENCODERS)
LOSSLESS_OUTPUTS = {".flac", ".wav"}
COVER_ART_OUTPUTS = {".mp3", ".m4a", ".flac"}

# Files picked up when a folder is given as input.
INPUT_EXTENSIONS = set(ENCODERS) | {
    ".aac", ".aif", ".aifc", ".aiff", ".alac", ".ape", ".m4b", ".mka",
    ".mkv", ".mov", ".mp4", ".oga", ".tta", ".webm", ".wma", ".wv",
}
LOSSLESS_CODECS = {"alac", "ape", "flac", "mlp", "shorten", "truehd", "tta", "wavpack"}
# Default output format for inputs whose own format can't be written back.
CODEC_EXTENSIONS = {"aac": ".m4a", "mp3": ".mp3", "opus": ".opus", "vorbis": ".ogg"}

# Tags that describe the original recording and would be wrong on the result.
STALE_TAGS = {
    "compatible_brands", "creation_time", "duration", "encoder", "handler_name",
    "initialkey", "itunnorm", "itunsmpb", "key", "length", "major_brand",
    "minor_version", "tkey", "tlen", "vendor_id",
}
STALE_TAG_PREFIXES = ("replaygain_", "r128_")
BPM_TAGS = {"bpm", "tbpm", "tmpo"}

ENGINES = ("auto", "rubberband", "ffmpeg-rubberband", "atempo")

FFMPEG_HELP = """\
ffmpeg was not found. Install it and make sure it is on your PATH:
  Windows: winget install Gyan.FFmpeg   (or https://www.gyan.dev/ffmpeg/builds/)
  macOS:   brew install ffmpeg
  Linux:   sudo apt install ffmpeg      (or your distribution's package manager)"""

RUBBERBAND_HELP = """\
Rubber Band was not found. Install it and make sure it is on your PATH:
  Windows: extract rubberband-3.2.1-gpl-executable-windows.zip next to MakeNightcore.py
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
        if not (math.isfinite(self.tempo) and self.tempo > 0):
            raise ValueError(f"tempo must be a positive number, not {self.tempo}")
        if not math.isfinite(self.pitch) or not math.isfinite(self.bass):
            raise ValueError("pitch and bass must be finite numbers")

    @classmethod
    def classic(cls, speed: float = DEFAULT_SPEED, bass: float = 0.0) -> Effect:
        """Speed up like a record player: pitch rises with the tempo."""
        if not (math.isfinite(speed) and speed > 0):
            raise ValueError(f"speed must be a positive number, not {speed}")
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


# --------------------------------------------------------------------------
# External programs
# --------------------------------------------------------------------------

def _search_dirs() -> list[Path]:
    """Folders next to this script where bundled programs may live."""
    dirs = [SCRIPT_DIR]
    for pattern in ("rubberband*", "ffmpeg*", "ffmpeg*/bin"):
        dirs += sorted(p for p in SCRIPT_DIR.glob(pattern) if p.is_dir())
    return dirs


def find_program(*names: str) -> str | None:
    """Find a program on PATH, or else next to this script."""
    for search_path in (None, os.pathsep.join(map(str, _search_dirs()))):
        for name in names:
            found = shutil.which(name, path=search_path)
            if found:
                return found
    return None


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str

    @classmethod
    def find(cls) -> Tools:
        ffmpeg, ffprobe = find_program("ffmpeg"), find_program("ffprobe")
        if not (ffmpeg and ffprobe):
            raise NightcoreError(FFMPEG_HELP)
        return cls(ffmpeg, ffprobe)


def find_rubberband() -> str | None:
    """Find the Rubber Band program, unpacking the bundled copy on Windows."""
    found = find_program("rubberband", "rubberband-r3")
    if found is None and os.name == "nt" and BUNDLED_RUBBERBAND_ZIP.is_file():
        try:
            with zipfile.ZipFile(BUNDLED_RUBBERBAND_ZIP) as bundle:
                bundle.extractall(SCRIPT_DIR)
        except (OSError, zipfile.BadZipFile):
            return None
        found = find_program("rubberband", "rubberband-r3")
    return found


def _capture(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), stdin=subprocess.DEVNULL, capture_output=True)


@functools.lru_cache(maxsize=None)
def _ffmpeg_filters(ffmpeg: str) -> frozenset:
    output = _capture([ffmpeg, "-hide_banner", "-filters"]).stdout.decode("utf-8", "replace")
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
    """Rubber Band 3 has a much better engine (R3) that must be asked for."""
    result = _capture([rubberband, "--version"])
    version = re.search(rb"(\d+)\.\d+", result.stdout + result.stderr)
    return ("-3",) if version and int(version.group(1)) >= 3 else ()


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
    # stderr goes to a file so a chatty program can never fill a pipe and hang.
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stderr=errors,
                                stdout=subprocess.PIPE if track else subprocess.DEVNULL)
        with _reaped(proc):
            if track:
                for line in proc.stdout:
                    key, _, value = line.decode("ascii", "replace").strip().partition("=")
                    if key in ("out_time_us", "out_time_ms") and value.isdigit():
                        progress(min(int(value) / 1e6 / duration, 1.0))
            returncode = proc.wait()
        if returncode != 0:
            errors.seek(0)
            _failed(cmd, returncode, errors.read().decode("utf-8", "replace").splitlines())


def _run_rubberband(cmd: Sequence[str], *, verbose: bool = False,
                    progress: Callable[[float], None] | None = None) -> None:
    """Run the Rubber Band program, following the progress it prints."""
    cmd = [str(arg) for arg in cmd]
    if verbose:
        print("  $ " + _quote(cmd), flush=True)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    messages, token, passes = [], bytearray(), 0
    with _reaped(proc):
        # It prints "Pass 1: Studying...", "\r0% \r1% ...", "Pass 2: Processing...", ...
        for char in iter(lambda: proc.stderr.read(1), b""):
            if char not in b"\r\n":
                token += char
                continue
            text = token.decode("utf-8", "replace").strip()
            token.clear()
            if text.startswith("Pass "):
                passes += 1
            percent = re.fullmatch(r"(\d+)%", text)
            if percent:
                if progress and passes:
                    progress(min((passes - 1 + int(percent.group(1)) / 100) / 2, 1.0))
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
        proc.kill()
        proc.wait()
        raise
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                pipe.close()


def _failed(cmd: Sequence[str], returncode: int, lines: list[str]) -> None:
    lines = [line for line in (line.strip() for line in lines) if line]
    detail = "\n".join("  " + line for line in lines[-10:]) or f"  exit code {returncode}"
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
    duration: float | None
    bits: int                 # bit depth for lossless output: 16 or 24
    lossless: bool
    cover: int | None         # stream index of embedded cover art
    tags: dict


def probe(path: Path, tools: Tools) -> Source:
    result = _capture([tools.ffprobe, "-v", "error", "-print_format", "json",
                       "-show_format", "-show_streams", str(path)])
    if result.returncode != 0:
        lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = lines[-1] if lines else "unknown error"
        raise NightcoreError(f"cannot read the file: {reason}")
    info = json.loads(result.stdout.decode("utf-8", "replace"))
    streams = info.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None or not _number(audio.get("sample_rate")):
        raise NightcoreError("the file contains no audio")

    codec = audio.get("codec_name", "")
    lossless = codec.startswith("pcm_") or codec in LOSSLESS_CODECS
    bits = int(_number(audio.get("bits_per_raw_sample")) or _number(audio.get("bits_per_sample")))
    if codec.startswith("pcm_f"):
        bits = 32
    cover = next((s["index"] for s in streams if s.get("codec_type") == "video"
                  and s.get("disposition", {}).get("attached_pic")), None)

    # Containers like Ogg keep tags on the audio stream rather than the file.
    tags: dict = {}
    for key, value in [*info.get("format", {}).get("tags", {}).items(),
                       *audio.get("tags", {}).items()]:
        if not any(key.lower() == existing.lower() for existing in tags):
            tags[key] = value

    return Source(
        path=path,
        codec=codec,
        sample_rate=int(_number(audio["sample_rate"])),
        duration=_number(info.get("format", {}).get("duration")) or _number(audio.get("duration")),
        bits=24 if lossless and bits > 16 else 16,
        lossless=lossless,
        cover=cover,
        tags=tags,
    )


def _number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def output_tags(source: Source, effect: Effect) -> dict:
    """The source's tags, updated for the new version."""
    tags = {}
    for key, value in source.tags.items():
        lower = key.lower()
        if lower in STALE_TAGS or lower.startswith(STALE_TAG_PREFIXES):
            continue
        if lower in BPM_TAGS:
            bpm = _number(value)
            if not bpm:
                continue
            value = f"{bpm * effect.tempo:.0f}"
        tags[key] = value
    title_key = next((key for key in tags if key.lower() == "title"), "title")
    title = str(tags.get(title_key, "")).strip() or source.path.stem
    suffix = f" ({effect.label})"
    tags[title_key] = title if title.endswith(suffix) else title + suffix
    return tags


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------

def choose_engine(effect: Effect, requested: str, tools: Tools) -> tuple[str, str | None]:
    """Pick how to process the audio. Returns (engine, rubberband_path)."""
    if not effect.stretch:
        return "resample", None
    if requested in ("auto", "rubberband"):
        rubberband = find_rubberband()
        if rubberband:
            return "rubberband", rubberband
        if requested == "rubberband":
            raise NightcoreError(RUBBERBAND_HELP)
    if requested in ("auto", "ffmpeg-rubberband"):
        if "rubberband" in _ffmpeg_filters(tools.ffmpeg):
            return "ffmpeg-rubberband", None
        if requested == "ffmpeg-rubberband":
            raise NightcoreError("this ffmpeg was built without the rubberband filter")
    return "atempo", None


def _output_rate(rate: int, ext: str) -> int:
    if ext == ".opus":
        return 48000  # Opus only works at 48 kHz
    if ext in LOSSLESS_OUTPUTS or rate <= 48000:
        return rate
    return 48000 if rate % 48000 == 0 else 44100  # lossy codecs gain nothing above 48 kHz


def _resample(rate: int, soxr: bool) -> str:
    return f"aresample={rate}:resampler=soxr:precision=28" if soxr else f"aresample={rate}"


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


def audio_filters(source: Source, effect: Effect, engine: str, ext: str, ffmpeg: str) -> list[str]:
    """The ffmpeg filter chain that applies the effect and prepares the output."""
    soxr = _has_soxr(ffmpeg)
    rate = _output_rate(source.sample_rate, ext)
    chain = ["aformat=sample_fmts=fltp"]  # process in floating point: no clipping along the way
    if engine == "resample":
        # Play the samples back faster, then convert to a standard sample rate.
        chain += [f"asetrate={round(source.sample_rate * effect.tempo)}", _resample(rate, soxr)]
    elif engine == "atempo":
        ratio = 2 ** (effect.pitch / 12)
        if ratio != 1:
            chain += [f"asetrate={round(source.sample_rate * ratio)}", _resample(rate, soxr)]
        elif rate != source.sample_rate:
            chain.append(_resample(rate, soxr))
        chain += _atempo(effect.tempo / ratio)
    else:
        if engine == "ffmpeg-rubberband":
            ratio = 2 ** (effect.pitch / 12)
            chain.append(f"rubberband=tempo={effect.tempo:.8f}:pitch={ratio:.8f}:pitchq=quality")
        # (for "rubberband", the Rubber Band program has already done the work)
        if rate != source.sample_rate:
            chain.append(_resample(rate, soxr))
    if effect.bass:
        chain += [f"bass=g={effect.bass:g}", _limiter(ffmpeg)]
    if ext in LOSSLESS_OUTPUTS and source.bits == 16:
        chain.append("aresample=osf=s16:dither_method=triangular")
    return chain


def _limiter(ffmpeg: str) -> str:
    """A limiter that keeps a bass boost from clipping (ceiling: -1 dBFS)."""
    options = _filter_options(ffmpeg, "alimiter")
    limiter = "alimiter=limit=0.891"
    if "level" in options:
        limiter += ":level=0"  # don't also normalize the volume
    if "latency" in options:
        limiter += ":latency=1"  # keep the audio in sync
    return limiter


def _encoder_args(ext: str, source: Source) -> list[str]:
    if ext == ".wav":
        return ["-c:a", "pcm_s24le" if source.bits == 24 else "pcm_s16le", "-rf64", "auto"]
    return list(ENCODERS[ext])


def _stretch_with_rubberband(source: Source, effect: Effect, rubberband: str, workdir: Path,
                             tools: Tools, verbose: bool, progress: _Progress) -> Path:
    """Time-stretch with the Rubber Band program, which only reads WAV files."""
    decoded, stretched = workdir / "decoded.wav", workdir / "stretched.wav"
    _run([tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
          "-i", source.path, "-map", "0:a:0", "-map_metadata", "-1",
          "-c:a", "pcm_f32le", "-rf64", "auto", decoded],
         verbose=verbose, duration=source.duration, progress=progress.stage(0.05))
    _run_rubberband([rubberband, *_rubberband_flags(rubberband),
                     "--tempo", f"{effect.tempo:.8f}", "--pitch", f"{effect.pitch:.8f}",
                     decoded, stretched], verbose=verbose, progress=progress.stage(0.85))
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
           rubberband: str | None = None, verbose: bool = False,
           progress: Callable[[float], None] | None = None) -> list[str]:
    """Write the effected audio to `output`. Returns any warnings."""
    ext = output.suffix.lower()
    steps = _Progress(progress)
    warnings = []
    with tempfile.TemporaryDirectory(prefix="nightcore-") as workdir:
        audio = source.path
        if engine == "rubberband":
            audio = _stretch_with_rubberband(source, effect, rubberband, Path(workdir),
                                             tools, verbose, steps)
        filters = audio_filters(source, effect, engine, ext, tools.ffmpeg)
        tags = output_tags(source, effect)
        duration = source.duration / effect.tempo if source.duration else None
        encoding = steps.stage(1 - steps.done)

        # Write next to the destination first, so a failed or interrupted run
        # never leaves a half-written file behind under the real name.
        fd, name = tempfile.mkstemp(dir=output.parent, prefix=f".{output.stem}.", suffix=ext)
        os.close(fd)
        partial = Path(name)
        partial.unlink()  # let ffmpeg create it, with normal permissions
        try:
            with_cover = source.cover is not None and ext in COVER_ART_OUTPUTS
            while True:
                cmd = _encode_command(source, audio, partial, filters, tags, ext, tools, with_cover)
                try:
                    _run(cmd, verbose=verbose, duration=duration, progress=encoding)
                    break
                except NightcoreError:
                    if not with_cover:
                        raise
                    # Some cover images can't go into some formats; retry without it.
                    with_cover = False
                    warnings.append("the cover art could not be copied")
            os.replace(partial, output)
        finally:
            if partial.exists():
                partial.unlink()
    return warnings


def _encode_command(source: Source, audio: Path, output: Path, filters: list[str], tags: dict,
                    ext: str, tools: Tools, with_cover: bool) -> list[str]:
    cmd = [tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio)]
    original = 0
    if audio != source.path:
        cmd += ["-i", str(source.path)]
        original = 1
    cmd += ["-map", "0:a:0"]
    if with_cover:
        cmd += ["-map", f"{original}:{source.cover}", "-c:v", "copy",
                "-disposition:v:0", "attached_pic"]
    # Chapters would point at the wrong times after a tempo change.
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1"]
    if with_cover:
        cmd += ["-map_metadata:s:v:0", f"{original}:s:{source.cover}"]  # e.g. "Cover (front)"
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd += ["-af", ",".join(filters), *_encoder_args(ext, source), str(output)]
    return cmd


def nightcore(source: str | os.PathLike, output: str | os.PathLike | None = None, *,
              speed: float | None = None, tempo: float | None = None,
              pitch: float | None = None, bass: float = 0.0, engine: str = "auto",
              overwrite: bool = False) -> Path:
    """Make a nightcore version of `source` and return the output path.

    With no options, this speeds the song up by 1.25x like a record player.
    Pass `tempo` and/or `pitch` (semitones) instead of `speed` to change them
    independently. `output` defaults to "<name> (Nightcore).<ext>" next to
    the source; its extension picks the format.
    """
    if speed is not None and (tempo is not None or pitch is not None):
        raise ValueError("use either speed, or tempo and/or pitch, not both")
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {', '.join(ENGINES)}")
    if tempo is None and pitch is None:
        effect = Effect.classic(DEFAULT_SPEED if speed is None else speed, bass)
    else:
        effect = Effect.stretched(1.0 if tempo is None else tempo, pitch or 0.0, bass)

    tools = Tools.find()
    source_path = Path(source).resolve()
    info = probe(source_path, tools)
    if output is None:
        target = default_output(info, source_path.parent, None, effect)
    else:
        target = Path(output).resolve()
        if target.suffix.lower() not in ENCODERS:
            raise NightcoreError(f"unsupported output format {target.suffix!r} "
                                 f"(choose from {', '.join(FORMATS)})")
    _check_target(source_path, target, overwrite)
    chosen, rubberband = choose_engine(effect, engine, tools)
    render(info, target, effect, chosen, tools, rubberband=rubberband)
    return target


def default_output(source: Source, directory: Path, fmt: str | None, effect: Effect) -> Path:
    """'<name> (Nightcore).<ext>', keeping the source's format when possible."""
    ext = source.path.suffix.lower()
    if fmt:
        ext = "." + fmt
    elif ext not in ENCODERS:
        ext = CODEC_EXTENSIONS.get(source.codec, ".flac" if source.lossless else ".mp3")
    return directory / f"{source.path.stem} ({effect.label}){ext}"


def _check_target(source: Path, target: Path, overwrite: bool) -> None:
    if target == source:
        raise NightcoreError("the output would replace the source file")
    if target.is_dir():
        raise NightcoreError(f"the output {target} is a folder")
    if target.exists() and not overwrite:
        raise NightcoreError(f"{target.name} already exists (use -y to overwrite)")
    if not target.parent.is_dir():
        raise NightcoreError(f"folder {target.parent} does not exist")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _is_generated(path: Path) -> bool:
    return path.stem.endswith((" (Nightcore)", " (Slowed)"))


def expand_inputs(paths: Iterable[str]) -> tuple[list[Path], list[str]]:
    """Files to process (folders are scanned for audio), plus any errors."""
    files, errors = [], []
    for name in paths:
        path = Path(name)
        if path.is_dir():
            found = [p for p in sorted(path.iterdir())
                     if p.is_file() and p.suffix.lower() in INPUT_EXTENSIONS
                     and not p.name.startswith(".") and not _is_generated(p)]
            if not found:
                errors.append(f"{name}: no audio files found in this folder")
            files += found
        elif path.is_file():
            files.append(path)
        else:
            errors.append(f"{name}: no such file or folder")
    return files, errors


def _factor(text: str) -> float:
    value = _parse_number(text)
    if not 0.1 <= value <= 10:
        raise argparse.ArgumentTypeError(f"{text} is out of range (0.1 to 10)")
    return value


def _semitones(text: str) -> float:
    value = _parse_number(text)
    if not -36 <= value <= 36:
        raise argparse.ArgumentTypeError(f"{text} is out of range (-36 to 36)")
    return value


def _decibels(text: str) -> float:
    value = _parse_number(text)
    if not -24 <= value <= 24:
        raise argparse.ArgumentTypeError(f"{text} is out of range (-24 to 24)")
    return value


def _parse_number(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    return value


def _format(text: str) -> str:
    fmt = text.lower().lstrip(".")
    if fmt not in FORMATS:
        raise argparse.ArgumentTypeError(f"{text!r} is not supported (choose from {', '.join(FORMATS)})")
    return fmt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="MakeNightcore",
        description="Make nightcore versions of songs: faster and higher, like a record "
                    "played at the wrong speed.",
        epilog="examples:\n"
               "  %(prog)s song.mp3                   -> 'song (Nightcore).mp3'\n"
               "  %(prog)s song.mp3 --speed 1.35      faster and higher\n"
               "  %(prog)s song.mp3 --bass 6 -f flac  with a bass boost, as FLAC\n"
               "  %(prog)s *.mp3 -o nightcore/        several songs into a folder\n"
               "  %(prog)s song.mp3 --tempo 1.2 --pitch 2  set tempo and pitch separately\n"
               "  %(prog)s song.mp3 --speed 0.8       slowed down instead",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", metavar="INPUT",
                        help="audio files, or folders of audio files")
    parser.add_argument("-s", "--source", action="append", default=[], metavar="INPUT",
                        help="input file (same as giving it without -s)")
    parser.add_argument("-o", "--output", metavar="PATH",
                        help="output file, or folder when there are several inputs "
                             "(default: '<name> (Nightcore).<ext>' next to each input)")
    parser.add_argument("-f", "--format", type=_format, metavar="FORMAT",
                        help=f"output format: {', '.join(FORMATS)} (default: same as the input)")

    effect = parser.add_argument_group(
        "effect",
        "By default the song is sped up by 1.25x with the pitch rising along with it, "
        "which is what nightcore traditionally is. Use --tempo and/or --pitch "
        "instead to change them independently (time-stretching).")
    effect.add_argument("--speed", type=_factor, metavar="X",
                        help=f"speed-up factor; pitch rises with it (default: {DEFAULT_SPEED})")
    effect.add_argument("--tempo", type=_factor, metavar="X",
                        help="tempo factor, without changing the pitch (default: 1)")
    effect.add_argument("--pitch", type=_semitones, metavar="SEMITONES",
                        help="pitch shift in semitones, without changing the tempo (default: 0)")
    effect.add_argument("--bass", type=_decibels, default=0.0, metavar="DB",
                        help="bass boost in dB, e.g. 6 (default: off)")
    effect.add_argument("--engine", choices=ENGINES, default="auto",
                        help="time-stretcher for --tempo/--pitch: the Rubber Band program, "
                             "ffmpeg's rubberband filter, or ffmpeg's lower-quality atempo "
                             "(default: best available)")

    parser.add_argument("-y", "--overwrite", action="store_true",
                        help="overwrite existing output files")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show the commands being run")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


class _Status:
    """One line per file, with a live percentage when writing to a terminal."""

    def __init__(self, quiet: bool, verbose: bool):
        self.quiet = quiet
        self.live = not quiet and not verbose and sys.stdout.isatty()
        self.line = ""

    def info(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    def start(self, text: str) -> None:
        self.line = text
        if self.live:
            print(f"{text}  ...", end="", flush=True)
        else:
            self.info(text)

    def progress(self, fraction: float) -> None:
        print(f"\r{self.line}  {fraction:4.0%}", end="", flush=True)

    def finish(self, ok: bool) -> None:
        if self.live:
            print(f"\r{self.line}  {'done' if ok else 'FAILED'}", flush=True)


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
    if stretch:
        effect = Effect.stretched(args.tempo or 1.0, args.pitch or 0.0, args.bass)
    else:
        effect = Effect.classic(args.speed or DEFAULT_SPEED, args.bass)

    def fail(message: str) -> None:
        print(f"error: {message}", file=sys.stderr, flush=True)

    files, errors = expand_inputs(names)
    for message in errors:
        fail(message)
    if not files:
        return 1

    # Work out where the output goes: a file, or a folder for many inputs.
    target_file, target_dir, add_ext = None, None, False
    if args.output:
        output = Path(args.output)
        is_dir = (output.is_dir() or args.output.endswith(("/", os.sep))
                  or len(files) > 1 or any(Path(name).is_dir() for name in names))
        if is_dir:
            target_dir = output.resolve()
            if target_dir.is_file():
                parser.error(f"--output {args.output} must be a folder when there are several inputs")
        else:
            target_file = output.resolve()
            ext = target_file.suffix.lower()
            if not ext:
                add_ext = True  # the extension is picked per file, as without --output
            elif ext not in ENCODERS:
                parser.error(f"unsupported output format {target_file.suffix!r} "
                             f"(choose from {', '.join(FORMATS)})")
            elif args.format and ext != "." + args.format:
                parser.error(f"--format {args.format} does not match --output {args.output}")

    try:
        tools = Tools.find()
        engine, rubberband = choose_engine(effect, args.engine, tools)
    except NightcoreError as error:
        fail(str(error))
        return 1
    if target_dir:
        target_dir.mkdir(parents=True, exist_ok=True)

    status = _Status(args.quiet, args.verbose)
    how = {"resample": "resampled", "rubberband": "time-stretched with Rubber Band",
           "ffmpeg-rubberband": "time-stretched with ffmpeg's rubberband filter",
           "atempo": "time-stretched with ffmpeg's atempo"}[engine]
    status.info(f"{effect.label}: {effect.describe()}, {how}")
    if engine == "atempo" and args.engine == "auto":
        print("warning: Rubber Band was not found, so the lower-quality atempo filter is used.\n"
              + RUBBERBAND_HELP, file=sys.stderr)

    done, failed, planned = 0, len(errors), set()
    try:
        for number, path in enumerate(files, 1):
            prefix = f"[{number}/{len(files)}] " if len(files) > 1 else ""
            source_path = path.resolve()
            try:
                source = probe(source_path, tools)
                target = default_output(source, target_dir or source_path.parent, args.format, effect)
                if target_file:
                    target = target_file.with_name(target_file.name + target.suffix) if add_ext \
                        else target_file
                if target in planned:
                    raise NightcoreError(f"{target.name} would be written twice "
                                         "(two inputs have the same name)")
                planned.add(target)
                _check_target(source_path, target, args.overwrite)
                status.start(f"{prefix}{path.name} -> {target.name}")
                try:
                    warnings = render(source, target, effect, engine, tools, rubberband=rubberband,
                                      verbose=args.verbose,
                                      progress=status.progress if status.live else None)
                except BaseException:
                    status.finish(False)
                    raise
                status.finish(True)
                for warning in warnings:
                    print(f"warning: {path.name}: {warning}", file=sys.stderr)
                done += 1
            except NightcoreError as error:
                fail(f"{path.name}: {error}")
                failed += 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    if len(files) > 1 or errors:
        status.info(f"{done} done, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
