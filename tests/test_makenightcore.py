"""Tests for MakeNightcore.

Most tests render real audio with ffmpeg and check the result by measuring it:
a sine tone should come out at the right pitch and length, at the right level,
with its tags and cover art. Those tests are skipped when ffmpeg is missing.
"""

import array
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

import MakeNightcore as mn

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and shutil.which("ffprobe")),
                                  reason="ffmpeg is not installed")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX only")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    *map(str, args)], check=True)


def ffprobe(path, *what):
    result = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                             *(what or ("-show_format", "-show_streams")), str(path)],
                            capture_output=True, check=True)
    return json.loads(result.stdout)


def audio_stream(path):
    return next(s for s in ffprobe(path)["streams"] if s["codec_type"] == "audio")


def tags_of(path):
    """All tags, lowercased keys, from the file and its audio stream."""
    info = ffprobe(path)
    tags = {}
    for source in (info["format"], audio_stream(path)):
        tags.update({k.lower(): v for k, v in source.get("tags", {}).items()})
    return tags


def cover_streams(path):
    return [s for s in ffprobe(path)["streams"]
            if s["codec_type"] == "video" and s.get("disposition", {}).get("attached_pic")]


def has_encoder(name):
    return bool(FFMPEG) and name in mn._ffmpeg_list(FFMPEG, "-encoders")


def ogg_codec():
    """What Ogg files are written with by this ffmpeg."""
    return "vorbis" if has_encoder("libvorbis") else "opus"


@pytest.fixture(scope="session")
def cover_png(tmp_path_factory):
    path = tmp_path_factory.mktemp("art") / "cover.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=32x32", "-frames:v", "1", path)
    return path


def make_tone(path, freq=440, seconds=2.0, rate=44100, tags=None, cover=None,
              cover_type="Cover (front)", layout="stereo", args=()):
    """Write a sine tone to `path` (format from its extension), in every channel."""
    channels = {"mono": 1, "stereo": 2, "3.0": 3, "5.1": 6, "5.1(side)": 6}[layout]
    pan = f"pan={layout}|" + "|".join(f"c{i}=c0" for i in range(channels))
    cmd = ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate={rate}:duration={seconds}"]
    if cover:
        cmd += ["-i", cover, "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-disposition:v", "attached_pic"]
        if cover_type:
            cmd += ["-metadata:s:v", f"comment={cover_type}"]
    cmd += ["-af", pan]
    for key, value in (tags or {}).items():
        cmd += ["-metadata", f"{key}={value}"]
    if Path(path).suffix == ".ogg" and not args:
        cmd += ["-c:a", "libvorbis" if has_encoder("libvorbis") else "libopus"]
    ffmpeg(*cmd, *args, path)
    return Path(path)


def samples(path):
    """(rate, channels, float samples) of the first audio stream."""
    stream = audio_stream(path)
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
                          "-map", "0:a:0", "-f", "f32le", "-c:a", "pcm_f32le", "-"],
                         capture_output=True, check=True).stdout
    values = array.array("f")
    values.frombytes(raw)
    if sys.byteorder == "big":
        values.byteswap()
    return int(stream["sample_rate"]), int(stream["channels"]), values


def analyse(path):
    """Measure (duration in s, frequency in Hz, peak level, RMS level) of channel 1."""
    rate, channels, values = samples(path)
    left = values[::channels]
    middle = left[len(left) // 10: len(left) - len(left) // 10]  # skip the edges
    rising = sum(1 for a, b in zip(middle, middle[1:]) if a < 0 <= b)
    rms = math.sqrt(sum(x * x for x in middle) / len(middle))
    return len(left) / rate, rising / (len(middle) / rate), max(map(abs, values)), rms


def db(ratio):
    return 20 * math.log10(ratio)


def run(*args):
    return mn.main([str(arg) for arg in args])


def available_engines():
    engines = ["atempo"]
    if FFMPEG and "rubberband" in mn._ffmpeg_list(FFMPEG, "-filters"):
        engines.append("ffmpeg-rubberband")
    if mn.find_rubberband():
        engines.append("rubberband")
    return engines


def needs_engine(engine):
    if engine not in ("resample", *available_engines()):
        pytest.skip(f"{engine} is not available")


def listing(folder):
    return sorted(p.name for p in Path(folder).iterdir())


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------

def test_classic_effect_ties_pitch_to_speed():
    effect = mn.Effect.classic(1.25)
    assert effect.tempo == 1.25
    assert effect.pitch == pytest.approx(3.8631, abs=1e-4)
    assert not effect.stretch
    assert mn.Effect.classic(2).pitch == pytest.approx(12)
    assert effect.label == "Nightcore"
    assert mn.Effect.classic(0.8).label == "Slowed"


@pytest.mark.parametrize("make", [
    lambda: mn.Effect.classic(0), lambda: mn.Effect.classic(-1),
    lambda: mn.Effect.classic(float("nan")), lambda: mn.Effect.classic(float("inf")),
    lambda: mn.Effect.classic(11), lambda: mn.Effect.stretched(tempo=0.05),
    lambda: mn.Effect.stretched(pitch=37), lambda: mn.Effect.stretched(pitch=float("nan")),
    lambda: mn.Effect.classic(bass=25), lambda: mn.Effect.stretched(bass=float("nan")),
])
def test_effect_rejects_nonsense(make):
    with pytest.raises(ValueError):
        make()


def test_make_effect():
    assert mn.make_effect() == mn.Effect.classic(1.25)
    assert mn.make_effect(speed=1.5, bass=3) == mn.Effect.classic(1.5, 3)
    assert mn.make_effect(pitch=2) == mn.Effect(1.0, 2.0, 0.0, stretch=True)
    assert mn.make_effect(tempo=1.2) == mn.Effect(1.2, 0.0, 0.0, stretch=True)
    with pytest.raises(ValueError):
        mn.make_effect(speed=1.2, tempo=1.1)


@pytest.mark.parametrize("factor", [0.1, 0.3, 0.5, 0.8, 1.0, 1.25, 2.0, 3.7, 10.0])
def test_atempo_chain_multiplies_to_factor(factor):
    steps = [float(f.split("=")[1]) for f in mn._atempo(factor)]
    assert math.prod(steps) == pytest.approx(factor)
    assert all(0.5 <= step <= 2.0 for step in steps)


def test_output_rate():
    lossy, lossless, opus = mn.Codec(()), mn.Codec((), lossless=True), mn.Codec((), opus=True)
    assert mn._output_rate(44100, lossy) == 44100
    assert mn._output_rate(96000, lossy) == 48000
    assert mn._output_rate(88200, lossy) == 44100
    assert mn._output_rate(96000, lossless) == 96000
    assert mn._output_rate(44100, opus) == 48000


def make_source(name="song.mp3", codec="mp3", lossless=False, tags=None):
    return mn.Source(path=Path(name), codec=codec, sample_rate=44100, duration=10.0, bits=16,
                     lossless=lossless, tags=tags or {})


def test_output_tags_are_updated_for_the_new_version():
    source = make_source(tags={
        "TITLE": "Song", "artist": "Band", "TBPM": "120", "bpm": "oops",
        "REPLAYGAIN_TRACK_GAIN": "-7 dB", "r128_track_gain": "1", "iTunSMPB": "x",
        "encoder": "Lavf", "TKEY": "Am", "album": "Album", "_STATISTICS_TAGS": "BPS",
        "BPS": "1411200", "language": "und", "CUESHEET": "FILE x WAVE",
    })
    tags = mn.output_tags(source, mn.Effect.classic(1.25), ".mp3")
    assert tags == {"TITLE": "Song (Nightcore)", "artist": "Band", "album": "Album", "TBPM": "150"}


@pytest.mark.parametrize("ext, key", [(".mp3", "TBPM"), (".m4a", "tmpo"), (".flac", "BPM"),
                                      (".ogg", "BPM"), (".opus", "BPM"), (".wav", None)])
def test_bpm_is_written_under_the_formats_own_key(ext, key):
    tags = mn.output_tags(make_source(tags={"TBPM": "128"}), mn.Effect.classic(1.25), ext)
    assert {k: v for k, v in tags.items() if k != "title"} == ({key: "160"} if key else {})


def test_a_real_language_is_kept():
    tags = mn.output_tags(make_source(tags={"language": "jpn"}), mn.Effect.classic(), ".mp3")
    assert tags["language"] == "jpn"


def test_output_title_falls_back_to_file_name_and_is_not_doubled():
    assert mn.output_tags(make_source("My Track.flac"), mn.Effect.classic(), ".flac")["title"] == \
        "My Track (Nightcore)"
    source = make_source(tags={"title": "Song (Nightcore)"})
    assert mn.output_tags(source, mn.Effect.classic(), ".mp3")["title"] == "Song (Nightcore)"
    assert mn.output_tags(source, mn.Effect.classic(0.8), ".mp3")["title"] == \
        "Song (Nightcore) (Slowed)"


def test_synced_lyrics_are_retimed():
    lyrics = "[ar:Band]\n[00:10.00]first\n[01:15.50]second\n[00:59.99] plain [not a time]"
    source = make_source(tags={"LYRICS": lyrics, "comment": "[00:10.00] untouched"})
    tags = mn.output_tags(source, mn.Effect.classic(1.25), ".mp3")
    assert tags["LYRICS"] == \
        "[ar:Band]\n[00:08.00]first\n[01:00.40]second\n[00:47.99] plain [not a time]"
    assert tags["comment"] == "[00:10.00] untouched"
    assert mn._retime_lyrics("[00:59.999]", 1.0) == "[01:00.00]"


def test_ffmetadata_escaping():
    text = mn._ffmetadata({"a=b": "x;y#z\\w\r\nnext"}).decode("utf-8")
    assert text == ";FFMETADATA1\na\\=b=x\\;y\\#z\\\\w\\\r\\\nnext\n"


@pytest.mark.parametrize("name, codec, lossless, fmt, expected", [
    ("a.mp3", "mp3", False, None, "a (Nightcore).mp3"),
    ("a.FLAC", "flac", True, None, "a (Nightcore).flac"),
    ("a.m4a", "alac", True, None, "a (Nightcore).m4a"),
    ("a.ogg", "opus", False, None, "a (Nightcore).ogg"),
    ("a.webm", "opus", False, None, "a (Nightcore).opus"),
    ("a.mp4", "aac", False, None, "a (Nightcore).m4a"),
    ("a.aiff", "pcm_s16be", True, None, "a (Nightcore).flac"),
    ("a.wma", "wmav2", False, None, "a (Nightcore).mp3"),
    ("a.mp3", "mp3", False, "flac", "a (Nightcore).flac"),
])
def test_default_output_name(name, codec, lossless, fmt, expected):
    source = make_source(name, codec, lossless)
    assert mn.default_output(source, Path("out"), fmt, mn.Effect.classic()) == Path("out", expected)


def test_long_names_are_shortened_to_fit():
    name = "【初音ミク】" + "夜" * 80 + ".mp3"  # 262 bytes
    output = mn.default_output(make_source(name), Path("out"), None, mn.Effect.classic())
    assert len(os.fsencode(output.name)) <= 255
    assert output.name.startswith("【初音ミク】夜夜") and output.name.endswith("夜 (Nightcore).mp3")


@pytest.mark.parametrize("name, ext", [("a.mp3", ".mp3"), ("a.FLAC", ".flac"), ("a", ""),
                                       ("Song ft. X", ""), ("v1.2 final", ""), ("a.xyz", ".xyz")])
def test_extension(name, ext):
    assert mn._extension(Path(name)) == ext


def test_plan_output():
    source, effect = make_source("/music/song.mp3"), mn.Effect.classic()
    assert mn.plan_output(source, None, None, effect) == Path("/music/song (Nightcore).mp3")
    assert mn.plan_output(source, Path("/x/out.flac"), None, effect) == Path("/x/out.flac")
    assert mn.plan_output(source, Path("/x/out"), None, effect) == Path("/x/out.mp3")
    assert mn.plan_output(source, Path("/x/out"), "ogg", effect) == Path("/x/out.ogg")
    assert mn.plan_output(source, Path("/x/d"), None, effect, folder=True) == \
        Path("/x/d/song (Nightcore).mp3")
    for output, fmt in [(Path("/x/out.xyz"), None), (Path("/x/out.mp3"), "flac")]:
        with pytest.raises(mn.NightcoreError):
            mn.plan_output(source, output, fmt, effect)


def test_follow_progress():
    reports = []
    lines = [b"out_time_us=500000", b"out_time_ms=500000", b"progress=continue",
             b"out_time_us=N/A", b"out_time_us=2000000", b"out_time_us=4000000"]
    mn._follow_progress(lines, 2.0, reports.append)
    assert reports == [0.25, 1.0, 1.0]
    reports.clear()
    mn._follow_progress([b"out_time_ms=1000000"], 2.0, reports.append)  # older ffmpeg
    assert reports == [0.5]


def test_fit():
    assert mn._fit("short", 10) == "short"
    fitted = mn._fit("a" * 30 + "b" * 30, 21)
    assert len(fitted) <= 21 and fitted.startswith("aaa") and fitted.endswith("bbb")
    assert "..." in fitted
    assert mn._display_width("日本") == 4
    assert mn._display_width(mn._fit("日本語" * 20, 20)) <= 20


def test_rubberband_flags(monkeypatch):
    def fake_help(text):
        return lambda cmd: subprocess.CompletedProcess(cmd, 0, text.encode(), b"")
    mn._rubberband_flags.cache_clear()
    monkeypatch.setattr(mn, "_capture", fake_help("-3, --fine ... --ignore-clipping ..."))
    assert mn._rubberband_flags("rb3") == ("--fine", "--ignore-clipping")
    monkeypatch.setattr(mn, "_capture", fake_help("-c<N>, --crisp <N>"))
    assert mn._rubberband_flags("rb1") == ()
    mn._rubberband_flags.cache_clear()


@pytest.mark.parametrize("args", [
    [],                                        # no input
    ["a.mp3", "--speed", "1.2", "--tempo", "1.1"],
    ["a.mp3", "--speed", "1.2", "--pitch", "2"],
    ["a.mp3", "--engine", "atempo"],           # engine without time-stretching
    ["a.mp3", "--speed", "0"],
    ["a.mp3", "--speed", "nan"],
    ["a.mp3", "--speed", "fast"],
    ["a.mp3", "--pitch", "99"],
    ["a.mp3", "--bass", "100"],
    ["a.mp3", "-f", "xyz"],
])
def test_bad_arguments_are_usage_errors(args):
    with pytest.raises(SystemExit) as exit_info:
        run(*args)
    assert exit_info.value.code == 2


@pytest.mark.parametrize("output, fmt", [("out.xyz", None), ("out.mp3", "flac")])
def test_bad_output_is_a_usage_error(tmp_path, output, fmt):
    (tmp_path / "a.mp3").write_bytes(b"")
    with pytest.raises(SystemExit) as exit_info:
        run(tmp_path / "a.mp3", "-o", tmp_path / output, *(["-f", fmt] if fmt else []))
    assert exit_info.value.code == 2


def test_file_like_output_for_several_inputs_is_a_usage_error(tmp_path, capsys):
    for name in ("a.mp3", "b.mp3"):
        (tmp_path / name).write_bytes(b"")
    with pytest.raises(SystemExit):
        run(tmp_path / "a.mp3", tmp_path / "b.mp3", "-o", tmp_path / "out.mp3")
    assert "looks like a file" in capsys.readouterr().err
    assert not (tmp_path / "out.mp3").exists()


def test_help_is_wrapped(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "80")
    with pytest.raises(SystemExit):
        run("--help")
    text = capsys.readouterr().out
    body = text.split("examples:")[0]
    assert max(len(line) for line in body.splitlines()) <= 80
    assert "Use --tempo and/or --pitch" in " ".join(body.split())


def test_missing_ffmpeg_is_explained(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.mp3").write_bytes(b"")
    monkeypatch.setattr(mn, "find_program", lambda *names: None)
    assert run(tmp_path / "a.mp3") == 1
    assert "ffmpeg was not found" in capsys.readouterr().err


def test_missing_input_is_reported(tmp_path, capsys):
    assert run(tmp_path / "nope.mp3") == 1
    assert "no such file" in capsys.readouterr().err


def test_wildcards_are_expanded(tmp_path):
    for name in ("b.mp3", "a.mp3", "c.wav", "[x].mp3"):
        (tmp_path / name).write_bytes(b"")
    files, errors = mn.expand_inputs([str(tmp_path / "*.mp3"), str(tmp_path / "*.ogg"),
                                      str(tmp_path / "[x].mp3")])
    assert [f.name for f in files] == ["[x].mp3", "a.mp3", "b.mp3", "[x].mp3"]
    assert errors == [f"{tmp_path / '*.ogg'}: no files match"]


def test_empty_folder_explains_subfolders(tmp_path):
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    files, errors = mn.expand_inputs([str(tmp_path)])
    assert files == [] and "subfolders are not searched" in errors[0]


@pytest.mark.parametrize("cli, has_filter, requested, expected", [
    ("/bin/rubberband", True, "auto", "rubberband"),
    (None, True, "auto", "ffmpeg-rubberband"),
    (None, False, "auto", "atempo"),
    ("/bin/rubberband", True, "ffmpeg-rubberband", "ffmpeg-rubberband"),
    ("/bin/rubberband", True, "atempo", "atempo"),
])
def test_choose_engine(monkeypatch, cli, has_filter, requested, expected):
    monkeypatch.setattr(mn, "find_rubberband", lambda: cli)
    monkeypatch.setattr(mn, "_ffmpeg_list", lambda ffmpeg, what: frozenset(
        {"rubberband"} if has_filter else ()))
    tools = mn.Tools("ffmpeg", "ffprobe")
    chosen = mn.choose_engine(mn.Effect.stretched(1.2), requested, tools)
    assert chosen == (expected, cli if expected == "rubberband" else None)
    assert mn.choose_engine(mn.Effect.classic(), requested, tools) == ("resample", None)


@pytest.mark.parametrize("requested", ["rubberband", "ffmpeg-rubberband"])
def test_unavailable_engine_is_an_error(monkeypatch, requested):
    monkeypatch.setattr(mn, "find_rubberband", lambda: None)
    monkeypatch.setattr(mn, "_ffmpeg_list", lambda ffmpeg, what: frozenset())
    with pytest.raises(mn.NightcoreError):
        mn.choose_engine(mn.Effect.stretched(1.2), requested, mn.Tools("ffmpeg", "ffprobe"))


def test_bundled_rubberband_is_unpacked_in_one_piece(tmp_path):
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("rubberband-x/rubberband.exe", b"program")
        bundle.writestr("rubberband-x/sndfile.dll", b"library")
    cache = tmp_path / "cache"
    mn._unpack(archive, cache)
    mn._unpack(archive, cache)  # a second (or concurrent) run changes nothing
    assert listing(cache) == ["rubberband-x"]
    assert listing(cache / "rubberband-x") == ["rubberband.exe", "sndfile.dll"]


@pytest.mark.skipif(os.name != "nt", reason="the bundled Rubber Band is for Windows")
def test_bundled_rubberband_is_found_on_windows(monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable))
    found = mn.find_rubberband()
    assert found and mn._cache_dir() in Path(found).parents


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

@needs_ffmpeg
def test_default_is_classic_nightcore(tmp_path, capsys):
    source = make_tone(tmp_path / "tone.wav", seconds=2)
    assert run(source) == 0
    duration, frequency, _, _ = analyse(tmp_path / "tone (Nightcore).wav")
    assert duration == pytest.approx(1.6, abs=0.001)
    assert frequency == pytest.approx(550, rel=0.005)
    assert "tone.wav -> tone (Nightcore).wav" in capsys.readouterr().out


@needs_ffmpeg
def test_slowed(tmp_path):
    source = make_tone(tmp_path / "tone.flac", seconds=2)
    assert run(source, "--speed", "0.8") == 0
    duration, frequency, _, _ = analyse(tmp_path / "tone (Slowed).flac")
    assert duration == pytest.approx(2.5, abs=0.001)
    assert frequency == pytest.approx(352, rel=0.005)


@needs_ffmpeg
@pytest.mark.parametrize("engine", ["rubberband", "ffmpeg-rubberband", "atempo"])
@pytest.mark.parametrize("tempo, pitch", [(1.25, 0), (1.0, 12), (1.2, 3), (0.8, -2)])
def test_time_stretching(tmp_path, engine, tempo, pitch):
    needs_engine(engine)
    source = make_tone(tmp_path / "tone.wav", seconds=3)
    assert run(source, "--tempo", tempo, "--pitch", pitch, "--engine", engine) == 0
    name = "tone (Slowed).wav" if tempo < 1 else "tone (Nightcore).wav"
    duration, frequency, _, _ = analyse(tmp_path / name)
    # (librubberband 1.8, in old ffmpeg builds, drops up to 80 ms at the end)
    assert duration == pytest.approx(3 / tempo, abs=0.1 if engine == "ffmpeg-rubberband" else 0.03)
    assert frequency == pytest.approx(440 * 2 ** (pitch / 12), rel=0.005)


@needs_ffmpeg
def test_time_stretching_defaults(tmp_path):
    source = make_tone(tmp_path / "tone.wav", seconds=2)
    assert run(source, "--pitch", "12") == 0  # the tempo stays at 1
    duration, frequency, _, _ = analyse(tmp_path / "tone (Nightcore).wav")
    assert duration == pytest.approx(2, abs=0.03)
    assert frequency == pytest.approx(880, rel=0.005)


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["wav", "mp3"])
@pytest.mark.parametrize("engine", ["rubberband", "ffmpeg-rubberband", "atempo"])
def test_time_stretching_keeps_the_level_without_clipping(tmp_path, engine, fmt):
    needs_engine(engine)
    source = tmp_path / "loud.wav"  # a loud chord of sawtooth waves, peaking near full scale
    saw = "+".join(f"0.33*(2*(t*{f}-floor(0.5+t*{f})))" for f in (110, 138.6, 164.8))
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc={saw}:s=44100:d=4",
           "-af", "alimiter=limit=0.99:level=0,pan=stereo|c0=c0|c1=c0", source)
    assert run(source, "--tempo", "1.2", "--pitch", "3", "--engine", engine, "-f", fmt) == 0
    _, _, peak, rms = analyse(tmp_path / f"loud (Nightcore).{fmt}")
    _, _, peak_before, rms_before = analyse(source)
    if fmt == "wav":
        assert peak <= 0.99  # limited to -0.1 dBFS
    elif engine != "atempo":  # (atempo makes no big overs, so lossy output isn't limited)
        assert peak < 1  # the phase vocoder's big overs are limited, even through MP3
    # Rubber Band's older R2 engine is itself about 1.2 dB quieter on this chord,
    # and lossy output is limited 2 dB lower.
    r2 = engine != "atempo" and not (engine == "rubberband" and
                                     "--fine" in mn._rubberband_flags(mn.find_rubberband()))
    tolerance = (1.5 if r2 else 0.75) + (1.0 if fmt == "mp3" else 0)
    assert db(rms / rms_before) == pytest.approx(0, abs=tolerance)


@needs_ffmpeg
def test_atempo_fallback_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mn, "find_rubberband", lambda: None)
    real = mn._ffmpeg_list
    monkeypatch.setattr(mn, "_ffmpeg_list", lambda ffmpeg, what: real(ffmpeg, what) - {"rubberband"})
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source, "--tempo", "1.2") == 0
    captured = capsys.readouterr()
    assert "atempo" in captured.out and "Rubber Band was not found" in captured.err


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["mp3", "m4a", "ogg", "opus", "flac", "wav"])
def test_output_formats(tmp_path, fmt):
    codec = {"mp3": "mp3", "m4a": "aac", "ogg": ogg_codec(), "opus": "opus",
             "flac": "flac", "wav": "pcm_s16le"}[fmt]
    source = make_tone(tmp_path / "tone.wav", seconds=2, tags={"title": "Tone", "artist": "Sine"})
    assert run(source, "-f", fmt) == 0
    output = tmp_path / f"tone (Nightcore).{fmt}"
    assert audio_stream(output)["codec_name"] == codec
    duration, frequency, _, _ = analyse(output)
    assert duration == pytest.approx(1.6, abs=0.001 if fmt in ("flac", "wav") else 0.06)
    assert frequency == pytest.approx(550, rel=0.005)
    tags = tags_of(output)
    assert tags["title"] == "Tone (Nightcore)"
    assert tags["artist"] == "Sine"


@needs_ffmpeg
def test_ogg_is_written_with_opus_without_vorbis(tmp_path, monkeypatch):
    real = mn._ffmpeg_list
    monkeypatch.setattr(mn, "_ffmpeg_list", lambda ffmpeg, what: real(ffmpeg, what) - {"libvorbis"})
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source, "-f", "ogg") == 0
    output = tmp_path / "tone (Nightcore).ogg"
    assert audio_stream(output)["codec_name"] == "opus"
    assert analyse(output)[1] == pytest.approx(550, rel=0.005)


@needs_ffmpeg
def test_missing_encoder_is_explained(tmp_path, monkeypatch, capsys):
    real = mn._ffmpeg_list
    monkeypatch.setattr(mn, "_ffmpeg_list", lambda ffmpeg, what: real(ffmpeg, what) - {"libmp3lame"})
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source, "-f", "mp3") == 1
    assert "can't write mp3 files (it has no libmp3lame encoder)" in capsys.readouterr().err
    assert listing(tmp_path) == ["tone.wav"]


@needs_ffmpeg
@pytest.mark.parametrize("name, args, codec", [
    ("alac.m4a", ["-c:a", "alac"], "alac"),
    ("opus.ogg", ["-c:a", "libopus"], "opus"),
    ("flac.ogg", ["-c:a", "flac"], "flac"),
])
def test_the_codec_is_kept_when_the_format_allows(tmp_path, name, args, codec):
    source = make_tone(tmp_path / name, args=args)
    assert run(source) == 0
    output = tmp_path / f"{Path(name).stem} (Nightcore){Path(name).suffix}"
    assert audio_stream(output)["codec_name"] == codec
    assert analyse(output)[1] == pytest.approx(550, rel=0.005)


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["mp3", "m4a", "flac"])
@pytest.mark.parametrize("engine", ["resample", "rubberband"])
def test_cover_art_and_tags_are_kept(tmp_path, cover_png, fmt, engine, capsys):
    needs_engine(engine)
    source = make_tone(tmp_path / "song.mp3", cover=cover_png, tags={
        "title": "Song", "artist": "Band", "album": "Album", "TBPM": "120",
        "REPLAYGAIN_TRACK_GAIN": "-7.00 dB", "REPLAYGAIN_TRACK_PEAK": "0.98"})
    args = ["--tempo", "1.25"] if engine == "rubberband" else []
    assert run(source, "-f", fmt, *args) == 0
    assert "warning" not in capsys.readouterr().err
    output = tmp_path / f"song (Nightcore).{fmt}"
    tags = tags_of(output)
    assert tags["title"] == "Song (Nightcore)"
    assert tags["artist"] == "Band"
    assert tags["album"] == "Album"
    if fmt != "m4a":  # ffprobe can't read MP4's BPM
        assert tags[{"mp3": "tbpm", "flac": "bpm"}[fmt]] == "150"
    assert not any(key.startswith("replaygain") for key in tags)
    # Nor the ReplayGain the MP3 muxer would put in its header from side data.
    assert not audio_stream(output).get("side_data_list")
    covers = cover_streams(output)
    assert len(covers) == 1
    if fmt != "m4a":  # MP4 has no picture types
        assert covers[0]["tags"]["comment"] == "Cover (front)"


@needs_ffmpeg
def test_the_front_cover_is_chosen(tmp_path, cover_png):
    back = tmp_path / "back.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=16x16", "-frames:v", "1", back)
    source = tmp_path / "song.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-i", back, "-i", cover_png,
           "-map", "0:a", "-map", "1:v", "-map", "2:v", "-c:v", "copy",
           "-disposition:v", "attached_pic", "-metadata:s:v:0", "comment=Cover (back)",
           "-metadata:s:v:1", "comment=Cover (front)", source)
    assert run(source, "-f", "flac") == 0
    covers = cover_streams(tmp_path / "song (Nightcore).flac")
    assert [(c["width"], c["tags"]["comment"]) for c in covers] == [(32, "Cover (front)")]


@needs_ffmpeg
def test_an_untyped_cover_becomes_the_front_cover(tmp_path, cover_png):
    source = make_tone(tmp_path / "song.m4a", cover=cover_png, cover_type=None)
    assert run(source, "-f", "mp3") == 0
    assert cover_streams(tmp_path / "song (Nightcore).mp3")[0]["tags"]["comment"] == "Cover (front)"


@needs_ffmpeg
def test_cover_art_that_cannot_be_stored_is_left_out_with_a_warning(tmp_path, capsys):
    gif = tmp_path / "cover.gif"
    ffmpeg("-f", "lavfi", "-i", "color=c=green:s=16x16", "-frames:v", "1", gif)
    source = make_tone(tmp_path / "song.mp3", cover=gif)
    assert run(source, "-f", "m4a") == 0
    output = tmp_path / "song (Nightcore).m4a"
    assert cover_streams(output) == []
    assert analyse(output)[1] == pytest.approx(550, rel=0.005)
    assert "the cover art (gif) can't be stored in m4a files" in capsys.readouterr().err


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["ogg", "opus", "wav"])
def test_cover_art_is_dropped_where_unsupported(tmp_path, cover_png, fmt, capsys):
    source = make_tone(tmp_path / "song.mp3", cover=cover_png, tags={"title": "Song"})
    assert run(source, "-f", fmt) == 0
    output = tmp_path / f"song (Nightcore).{fmt}"
    assert cover_streams(output) == []
    assert tags_of(output)["title"] == "Song (Nightcore)"
    assert "warning" not in capsys.readouterr().err


@needs_ffmpeg
def test_ogg_tags_carry_over(tmp_path):
    source = make_tone(tmp_path / "song.ogg", tags={"title": "Song", "artist": "Band"})
    assert run(source, "-f", "mp3") == 0
    tags = tags_of(tmp_path / "song (Nightcore).mp3")
    assert tags["title"] == "Song (Nightcore)"
    assert tags["artist"] == "Band"


@needs_ffmpeg
def test_technical_stream_tags_are_not_copied(tmp_path):
    source = make_tone(tmp_path / "rip.mka", tags={"title": "Song"}, args=[
        "-metadata:s:a", "BPS=1411200", "-metadata:s:a", "NUMBER_OF_FRAMES=100",
        "-metadata:s:a", "_STATISTICS_WRITING_APP=mkvmerge", "-metadata:s:a", "language=jpn"])
    assert run(source, "-f", "mp3") == 0
    tags = tags_of(tmp_path / "rip (Nightcore).mp3")
    assert tags["title"] == "Song (Nightcore)"
    assert not {"bps", "number_of_frames", "_statistics_writing_app", "language"} & set(tags)


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["mp3", "flac"])
def test_huge_and_awkward_tags(tmp_path, fmt):
    lyrics = "line one\r\nline two; # = \\ done\n" + "la " * 70000  # over 128 KiB
    source = tmp_path / "song.flac"
    metadata = tmp_path / "tags.txt"
    metadata.write_bytes(mn._ffmetadata({"title": "A = B", "lyrics": lyrics}))
    ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-f", "ffmetadata", "-i", metadata,
           "-map", "0", "-map_metadata", "1", source)
    assert run(source, "-f", fmt) == 0
    tags = tags_of(tmp_path / f"song (Nightcore).{fmt}")
    assert tags["title"] == "A = B (Nightcore)"
    assert tags["lyrics"] == lyrics


@needs_ffmpeg
@pytest.mark.parametrize("replaygain", [True, False])
def test_mp3_is_encoded_from_a_decoded_copy_when_the_source_has_replaygain(
        tmp_path, monkeypatch, replaygain):
    # Newer and older ffmpeg versions pass the source's ReplayGain on to the MP3
    # header by different routes; only a separately decoded copy has none.
    commands = []
    real_run = mn._run
    monkeypatch.setattr(mn, "_run", lambda cmd, **kw: (commands.append([str(a) for a in cmd]),
                                                     real_run(cmd, **kw)))
    tags = {"REPLAYGAIN_TRACK_GAIN": "-7.00 dB"} if replaygain else {"title": "Song"}
    source = make_tone(tmp_path / "song.flac", seconds=1, tags=tags)
    assert run(source, "-f", "mp3") == 0
    assert len(commands) == (2 if replaygain else 1)
    assert commands[-1][commands[-1].index("-i") + 1].endswith(
        "decoded.wav" if replaygain else "song.flac")
    assert not audio_stream(tmp_path / "song (Nightcore).mp3").get("side_data_list")


@needs_ffmpeg
def test_chapters_are_dropped(tmp_path):
    chapters = tmp_path / "chapters.txt"
    chapters.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n"
                        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Two\n")
    source = tmp_path / "book.m4a"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=2", "-i", chapters, "-map", "0",
           "-map_chapters", "1", "-c:a", "aac", source)
    assert ffprobe(source, "-show_chapters")["chapters"]
    assert run(source) == 0
    assert ffprobe(tmp_path / "book (Nightcore).m4a", "-show_chapters")["chapters"] == []


@needs_ffmpeg
def test_video_input_keeps_audio_only(tmp_path):
    source = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-f", "lavfi",
           "-i", "color=c=blue:s=64x64:d=2", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", source)
    assert run(source) == 0
    output = tmp_path / "clip (Nightcore).m4a"
    assert [s["codec_type"] for s in ffprobe(output)["streams"]] == ["audio"]
    assert analyse(output)[1] == pytest.approx(550, rel=0.005)


@needs_ffmpeg
def test_file_without_audio(tmp_path, cover_png, capsys):
    shutil.copy(cover_png, tmp_path / "picture.mp4")
    assert run(tmp_path / "picture.mp4") == 1
    assert "contains no audio" in capsys.readouterr().err


@needs_ffmpeg
def test_a_result_without_audio_is_an_error(tmp_path, capsys):
    import wave
    with wave.open(str(tmp_path / "empty.wav"), "wb") as empty:  # a header, no samples
        empty.setnchannels(2)
        empty.setsampwidth(2)
        empty.setframerate(44100)
    assert run(tmp_path / "empty.wav") == 1
    assert "contains no audio" in capsys.readouterr().err
    assert listing(tmp_path) == ["empty.wav"]


@needs_ffmpeg
@pytest.mark.parametrize("engine", ["rubberband", "ffmpeg-rubberband", "atempo"])
def test_too_short_to_time_stretch(tmp_path, engine, capsys):
    needs_engine(engine)
    source = tmp_path / "blip.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=0.05", source)
    assert run(source, "--tempo", "1.25", "--engine", engine) == 1
    assert "too short to time-stretch" in capsys.readouterr().err
    assert listing(tmp_path) == ["blip.wav"]


@needs_ffmpeg
@pytest.mark.parametrize("name, args, fmt, expected", [
    ("in16.flac", [], "flac", ("s16", "16")),
    ("in24.flac", ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"], "flac",
     ("s32", "24")),
    ("in24.wav", ["-c:a", "pcm_s24le"], "wav", ("s32", "24")),
    ("in16.wav", [], "wav", ("s16", "16")),
    ("float.wav", ["-c:a", "pcm_f32le"], "wav", ("flt", "32")),
    ("float.wav", ["-c:a", "pcm_f32le"], "flac", ("s32", "24")),
    ("alac16.m4a", ["-c:a", "alac", "-sample_fmt", "s16p"], "m4a", ("s16p", "16")),
    ("alac24.m4a", ["-c:a", "alac", "-sample_fmt", "s32p"], "m4a", ("s32p", "24")),
    ("lossy.mp3", [], "flac", ("s16", "16")),
    ("lossy.mp3", [], "wav", ("s16", "16")),
])
def test_bit_depth_follows_the_source(tmp_path, name, args, fmt, expected):
    source = make_tone(tmp_path / name, args=args)
    assert run(source, "-f", fmt) == 0
    stream = audio_stream(tmp_path / f"{Path(name).stem} (Nightcore).{fmt}")
    bits = stream.get("bits_per_raw_sample") if fmt != "wav" else str(stream["bits_per_sample"])
    assert (stream["sample_fmt"], bits) == expected


@needs_ffmpeg
def test_float_sources_keep_their_overs(tmp_path):
    source = make_tone(tmp_path / "hot.wav", args=["-af", "volume=10", "-c:a", "pcm_f32le"])
    assert analyse(source)[2] > 1  # peaks above full scale
    assert run(source) == 0
    assert analyse(tmp_path / "hot (Nightcore).wav")[2] == pytest.approx(analyse(source)[2], rel=0.01)


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["wav", "flac"])
def test_16_bit_output_is_dithered(tmp_path, fmt):
    source = tmp_path / "tail.wav"  # a tone, then digital silence
    ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
           "-filter_complex", "[1]atrim=duration=1[s];[0][s]concat=n=2:v=0:a=1",
           "-c:a", "pcm_s16le", source)
    assert run(source, "-f", fmt) == 0
    rate, _, values = samples(tmp_path / f"tail (Nightcore).{fmt}")
    tail = values[int(1.2 * rate):]
    assert any(tail)  # dither noise...
    assert max(map(abs, tail)) <= 2.5 / 32768  # ...of a bit or two


@needs_ffmpeg
@pytest.mark.parametrize("fmt, rate", [("flac", 96000), ("mp3", 48000), ("opus", 48000)])
def test_sample_rate(tmp_path, fmt, rate):
    source = make_tone(tmp_path / "hires.flac", rate=96000, seconds=1)
    assert run(source, "-f", fmt) == 0
    output = tmp_path / f"hires (Nightcore).{fmt}"
    assert int(audio_stream(output)["sample_rate"]) == rate
    assert analyse(output)[1] == pytest.approx(550, rel=0.005)


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["mono", "5.1", "5.1(side)"])
@pytest.mark.parametrize("fmt", ["mp3", "m4a", "ogg", "opus", "flac", "wav"])
def test_channel_layouts(tmp_path, layout, fmt):
    source = make_tone(tmp_path / "in.flac", seconds=1, layout=layout)
    assert run(source, "-f", fmt) == 0
    assert analyse(tmp_path / f"in (Nightcore).{fmt}")[1] == pytest.approx(550, rel=0.005)


@needs_ffmpeg
@pytest.mark.parametrize("engine", ["rubberband", "ffmpeg-rubberband", "atempo"])
def test_time_stretching_keeps_the_channel_layout(tmp_path, engine):
    needs_engine(engine)
    source = tmp_path / "centre.flac"  # sound in the centre channel only
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=0|0|0.5*sin(2*PI*440*t):c=3.0:d=1", source)
    assert run(source, "--tempo", "1.2", "--engine", engine) == 0
    assert audio_stream(tmp_path / "centre (Nightcore).flac")["channel_layout"] == "3.0"
    assert run(source, "--tempo", "1.2", "--engine", engine, "-f", "mp3") == 0  # a stereo downmix
    assert analyse(tmp_path / "centre (Nightcore).mp3")[3] > 0.01


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["wav", "flac", "mp3", "m4a", "ogg", "opus"])
@pytest.mark.parametrize("args", [[], ["--tempo", "1.2", "--pitch", "3"]])
def test_bass_boost_never_clips(tmp_path, fmt, args):
    source = tmp_path / "loud.wav"
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=0.94*sin(2*PI*60*t)|0.94*sin(2*PI*60*t):s=44100:d=2",
           source)
    assert run(source, "--bass", "12", "-f", fmt, *args) == 0
    _, _, peak, rms = analyse(tmp_path / f"loud (Nightcore).{fmt}")
    if fmt in ("wav", "flac"):
        assert 0.95 < peak <= 0.99  # limited to -0.1 dBFS, not normalized
    else:
        assert peak < 1  # lossy codecs overshoot, but not above full scale
    assert rms / peak < 0.8  # still a sine (0.71), not squared off by clipping (~1)


@needs_ffmpeg
def test_bass_boost_boosts_the_bass_only(tmp_path):
    change = {}
    for freq in (60, 3000):
        source = make_tone(tmp_path / f"t{freq}.wav", freq=freq / 1.25)  # `freq` once sped up
        assert run(source, "--bass", "6") == 0
        change[freq] = db(analyse(tmp_path / f"t{freq} (Nightcore).wav")[3] / analyse(source)[3])
    assert change[60] > 4.5
    assert abs(change[3000]) < 0.5


@needs_ffmpeg
@pytest.mark.parametrize("fmt, limited", [("flac", True), ("wav", True), ("mp3", False)])
def test_resampling_a_loud_master_does_not_clip(tmp_path, fmt, limited):
    source = tmp_path / "loud.wav"  # sawtooths: resampling makes peaks between the samples
    saw = "+".join(f"0.33*(2*(t*{f}-floor(0.5+t*{f})))" for f in (110, 138.6, 164.8))
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc={saw}:s=44100:d=4",
           "-af", "alimiter=limit=0.99:level=0,pan=stereo|c0=c0|c1=c0", source)
    assert run(source, "-f", fmt) == 0
    _, _, peak, rms = analyse(tmp_path / f"loud (Nightcore).{fmt}")
    _, _, _, rms_before = analyse(source)
    if limited:
        assert peak <= 0.99  # these would clip, so they are limited
    else:
        assert peak > 1  # MP3 keeps the overs, as the source's own would be
    assert db(rms / rms_before) == pytest.approx(0, abs=0.1)


@needs_ffmpeg
@pytest.mark.parametrize("soxr", [True, False])
def test_sound_pushed_past_the_top_of_the_range_is_removed_not_aliased(tmp_path, monkeypatch, soxr):
    if soxr and not mn._has_soxr(FFMPEG):
        pytest.skip("this ffmpeg has no SoX resampler")
    monkeypatch.setattr(mn, "_has_soxr", lambda ffmpeg: soxr)
    source = make_tone(tmp_path / "high.wav", freq=18500)  # 23.1 kHz once sped up
    assert run(source, "-f", "flac") == 0
    _, _, _, rms = analyse(tmp_path / "high (Nightcore).flac")
    assert db(max(rms, 1e-9) / analyse(source)[3]) < -60


@needs_ffmpeg
def test_level_is_untouched_without_bass_boost(tmp_path):
    source = make_tone(tmp_path / "tone.wav")
    assert run(source) == 0
    _, _, before, _ = analyse(source)
    _, _, after, _ = analyse(tmp_path / "tone (Nightcore).wav")
    assert after == pytest.approx(before, rel=0.01)


# --------------------------------------------------------------------------
# Files and folders
# --------------------------------------------------------------------------

@needs_ffmpeg
def test_existing_output_needs_overwrite(tmp_path, capsys):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source) == 0
    assert run(source) == 1
    assert "already exists (use -y to overwrite)" in capsys.readouterr().err
    assert run(source, "-y") == 0


@needs_ffmpeg
def test_source_is_never_overwritten(tmp_path, capsys):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    before = source.read_bytes()
    os.link(source, tmp_path / "hardlink.wav")
    for output in (source, tmp_path / "hardlink.wav", tmp_path / "." / "tone.wav"):
        assert run(source, "-o", output, "-y") == 1
        assert "replace the source" in capsys.readouterr().err
    assert source.read_bytes() == before


@needs_ffmpeg
def test_source_is_safe_on_case_insensitive_file_systems(tmp_path):
    (tmp_path / "probe").write_bytes(b"")
    if not (tmp_path / "PROBE").exists():
        pytest.skip("the file system is case-sensitive")
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    before = source.read_bytes()
    assert run(source, "-o", tmp_path / "TONE.wav", "-y") == 1
    assert source.read_bytes() == before


@needs_ffmpeg
@posix_only
def test_symlinked_input_is_named_after_the_link(tmp_path):
    (tmp_path / "store").mkdir()
    (tmp_path / "library").mkdir()
    target = make_tone(tmp_path / "store" / "SHA256-abc.mp3", seconds=1)
    (tmp_path / "library" / "My Song.mp3").symlink_to(target)
    assert run(tmp_path / "library" / "My Song.mp3") == 0
    assert listing(tmp_path / "library") == ["My Song (Nightcore).mp3", "My Song.mp3"]
    assert listing(tmp_path / "store") == ["SHA256-abc.mp3"]
    assert tags_of(tmp_path / "library" / "My Song (Nightcore).mp3")["title"] == \
        "My Song (Nightcore)"


@needs_ffmpeg
def test_legacy_command_line(tmp_path):
    source = make_tone(tmp_path / "input.mp3", seconds=1)
    output = tmp_path / "output.wav"
    assert run("-s", source, "-o", output) == 0
    assert audio_stream(output)["codec_name"] == "pcm_s16le"
    assert analyse(output)[1] == pytest.approx(550, rel=0.005)


@needs_ffmpeg
def test_output_without_extension_keeps_the_format(tmp_path):
    source = make_tone(tmp_path / "tone.ogg", seconds=1)
    assert run(source, "-o", tmp_path / "result") == 0
    assert audio_stream(tmp_path / "result.ogg")["codec_name"] == ogg_codec()


@needs_ffmpeg
def test_output_folder_is_created(tmp_path):
    source = make_tone(tmp_path / "tone.mp3", seconds=1)
    assert run(source, "-o", str(tmp_path / "new" / "folder") + os.sep) == 0
    assert (tmp_path / "new" / "folder" / "tone (Nightcore).mp3").is_file()


@needs_ffmpeg
def test_output_file_in_a_new_folder(tmp_path):
    source = make_tone(tmp_path / "tone.mp3", seconds=1)
    assert run(source, "-o", tmp_path / "new" / "folder" / "out.mp3") == 0
    assert (tmp_path / "new" / "folder" / "out.mp3").is_file()


@needs_ffmpeg
def test_output_into_an_existing_folder(tmp_path):
    source = make_tone(tmp_path / "tone.mp3", seconds=1)
    (tmp_path / "out").mkdir()
    assert run(source, "-o", tmp_path / "out") == 0  # no trailing slash
    assert listing(tmp_path / "out") == ["tone (Nightcore).mp3"]


@needs_ffmpeg
def test_folder_input(tmp_path, capsys):
    music = tmp_path / "music"
    music.mkdir()
    make_tone(music / "a.mp3", seconds=1)
    make_tone(music / "b.wav", seconds=1)
    make_tone(music / "c (Nightcore).mp3", seconds=1)  # an earlier result: skipped
    make_tone(music / ".hidden.mp3", seconds=1)
    (music / "notes.txt").write_text("not audio")
    assert run(music, "-o", tmp_path / "out") == 0
    assert listing(tmp_path / "out") == ["a (Nightcore).mp3", "b (Nightcore).wav"]
    assert "2 done, 0 failed" in capsys.readouterr().out


@needs_ffmpeg
def test_folder_with_one_file_goes_into_the_output_folder(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    make_tone(music / "a.mp3", seconds=1)
    assert run(music, "-o", tmp_path / "out") == 0
    assert listing(tmp_path / "out") == ["a (Nightcore).mp3"]


@needs_ffmpeg
def test_two_inputs_with_the_same_output(tmp_path, capsys):
    a = make_tone(tmp_path / "a.mp3", seconds=1)
    b = make_tone(tmp_path / "a.wav", seconds=1)
    assert run(a, b, "-f", "mp3", "-o", tmp_path / "out") == 1
    assert "written twice" in capsys.readouterr().err
    assert listing(tmp_path / "out") == ["a (Nightcore).mp3"]


@needs_ffmpeg
def test_mixed_success_and_failure(tmp_path, capsys):
    good = make_tone(tmp_path / "good.wav", seconds=1)
    assert run(good, tmp_path / "missing.mp3") == 1
    assert "1 done, 1 failed" in capsys.readouterr().out
    assert (tmp_path / "good (Nightcore).wav").is_file()


@needs_ffmpeg
@pytest.mark.parametrize("name", [
    "with spaces.mp3", "quote's \"here\".mp3", "colon: yes.mp3", "ünïcødé ♪ 日本.mp3",
    "-starts-with-dash.mp3", "$(echo hi) `x` ;&|.mp3", "100% [remix] {1}.mp3",
    "【初音ミク】" + "夜" * 72 + ".mp3",  # 238 bytes: near the file name limit
])
def test_awkward_file_names(tmp_path, monkeypatch, name):
    if os.name == "nt" and any(c in name for c in '"*:<>?|'):
        pytest.skip("not a valid file name on Windows")
    source = make_tone(tmp_path / "plain.mp3", seconds=1).rename(tmp_path / name)
    monkeypatch.chdir(tmp_path)
    assert run("--", name) == 0  # a relative path, like a user would type it
    outputs = [p for p in listing(tmp_path) if p != source.name]
    assert len(outputs) == 1 and outputs[0].endswith(" (Nightcore).mp3")
    assert analyse(tmp_path / outputs[0])[1] == pytest.approx(550, rel=0.005)


@needs_ffmpeg
def test_unreadable_input_leaves_nothing_behind(tmp_path, capsys):
    bad = tmp_path / "bad.mp3"
    bad.write_text("this is not audio")
    assert run(bad) == 1
    error = capsys.readouterr().err
    assert "bad.mp3: not an audio file ffmpeg can read" in error
    assert str(tmp_path) not in error
    assert listing(tmp_path) == ["bad.mp3"]


@needs_ffmpeg
def test_failure_mid_encode_removes_the_partial_file(tmp_path):
    source_path = make_tone(tmp_path / "tone.wav", seconds=30)
    tools = mn.Tools.find()
    source = mn.probe(source_path, tools)

    def fail_early(fraction):
        if fraction > 0.02:
            raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        mn.render(source, tmp_path / "out.flac", mn.Effect.classic(), "resample", tools,
                  progress=fail_early)
    assert listing(tmp_path) == ["tone.wav"]


@needs_ffmpeg
@pytest.mark.parametrize("engine", ["resample", "rubberband"])
def test_interrupt_stops_ffmpeg_and_cleans_up(tmp_path, monkeypatch, capsys, engine):
    needs_engine(engine)
    source = make_tone(tmp_path / "tone.wav", seconds=30)
    work = tmp_path / "tmp"
    work.mkdir()
    started = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        started.append(real_popen(*args, **kwargs))
        return started[-1]

    def interrupt(self, fraction):
        if fraction > 0.02:
            raise KeyboardInterrupt
    monkeypatch.setattr(mn.subprocess, "Popen", popen)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(mn._Status, "progress", interrupt)
    monkeypatch.setattr(mn.tempfile, "tempdir", str(work))
    args = ["--tempo", "1.2"] if engine == "rubberband" else []
    assert run(source, "-f", "flac", *args) == 130
    captured = capsys.readouterr()
    assert "FAILED" in captured.out and "interrupted" in captured.err
    assert started and all(proc.poll() is not None for proc in started)
    assert listing(tmp_path) == ["tmp", "tone.wav"]
    assert listing(work) == []


@needs_ffmpeg
@posix_only
def test_termination_cleans_up(tmp_path):
    source = make_tone(tmp_path / "tone.wav", seconds=240)
    work = tmp_path / "tmp"
    work.mkdir()
    proc = subprocess.Popen([sys.executable, mn.__file__, str(source), "-f", "flac", "-q"],
                            env=dict(os.environ, TMPDIR=str(work)))
    deadline = time.time() + 30
    while not any(p.name.startswith(".nightcore-") for p in tmp_path.iterdir()):
        assert time.time() < deadline and proc.poll() is None
        time.sleep(0.01)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 130
    time.sleep(0.5)  # anything still running would keep writing
    assert listing(tmp_path) == ["tmp", "tone.wav"]
    assert listing(work) == []


@needs_ffmpeg
def test_file_system_errors_are_reported_and_the_batch_goes_on(tmp_path, monkeypatch, capsys):
    a = make_tone(tmp_path / "a.wav", seconds=1)
    b = make_tone(tmp_path / "b.wav", seconds=1)
    real_replace = os.replace

    def locked(source, target):
        if Path(target).name == "a (Nightcore).wav":
            raise PermissionError(13, "The file is being used by another process", str(target))
        real_replace(source, target)
    monkeypatch.setattr(mn.os, "replace", locked)
    assert run(a, b) == 1
    captured = capsys.readouterr()
    assert "a.wav: The file is being used by another process" in captured.err
    assert "1 done, 1 failed" in captured.out
    assert listing(tmp_path) == ["a.wav", "b (Nightcore).wav", "b.wav"]


@needs_ffmpeg
@posix_only
def test_output_has_normal_permissions(tmp_path):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source) == 0
    mode = stat.S_IMODE((tmp_path / "tone (Nightcore).wav").stat().st_mode)
    umask = os.umask(0)
    os.umask(umask)
    assert mode == 0o666 & ~umask


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

@needs_ffmpeg
def test_quiet(tmp_path, capsys):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source, "-q") == 0
    assert capsys.readouterr().out == ""


@needs_ffmpeg
def test_verbose_shows_commands(tmp_path, capsys):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source, "-v") == 0
    out = capsys.readouterr().out
    assert "  $ " in out and "asetrate=55125" in out


@needs_ffmpeg
def test_live_progress_stays_on_one_line(tmp_path, monkeypatch, capsys):
    source = make_tone(tmp_path / ("A Very Long Artist Name - A Very Long Title " * 2 + ".wav"),
                       seconds=60)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(mn.shutil, "get_terminal_size", lambda: os.terminal_size((60, 24)))
    assert run(source) == 0
    lines = capsys.readouterr().out.split("\n")
    updates = lines[1].split("\r")
    assert updates[-1] == f"{source.name} -> {source.stem.rstrip()} (Nightcore).wav  done"
    assert any("%" in update for update in updates)
    assert all(mn._display_width(update) < 60 for update in updates[:-1])


@needs_ffmpeg
def test_no_carriage_returns_when_not_a_terminal(tmp_path, capsys):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source) == 0
    assert "\r" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# Library use and internals
# --------------------------------------------------------------------------

@needs_ffmpeg
def test_library_function(tmp_path):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    output = mn.nightcore(source)
    assert output == tmp_path / "tone (Nightcore).wav"
    assert analyse(output)[1] == pytest.approx(550, rel=0.005)
    assert analyse(mn.nightcore(source, tmp_path / "custom.flac", speed=1.5))[1] == \
        pytest.approx(660, rel=0.005)
    assert mn.nightcore(source, tmp_path / "noext", format="mp3") == tmp_path / "noext.mp3"
    (tmp_path / "folder").mkdir()
    assert mn.nightcore(source, tmp_path / "folder", tempo=1.2) == \
        tmp_path / "folder" / "tone (Nightcore).wav"
    with pytest.raises(mn.NightcoreError, match="already exists"):
        mn.nightcore(source)
    with pytest.raises(ValueError):
        mn.nightcore(source, speed=1.2, pitch=3)
    with pytest.raises(ValueError):
        mn.nightcore(source, engine="atempo")
    with pytest.raises(ValueError):
        mn.nightcore(source, format="xyz")


@needs_ffmpeg
def test_library_function_warns(tmp_path):
    gif = tmp_path / "cover.gif"
    ffmpeg("-f", "lavfi", "-i", "color=c=green:s=16x16", "-frames:v", "1", gif)
    source = make_tone(tmp_path / "song.mp3", cover=gif)
    with pytest.warns(UserWarning, match="cover art"):
        mn.nightcore(source, format="m4a")


@needs_ffmpeg
@pytest.mark.parametrize("engine", ["resample", "rubberband", "ffmpeg-rubberband", "atempo"])
def test_progress_is_reported(tmp_path, engine):
    needs_engine(engine)
    source_path = make_tone(tmp_path / "tone.wav", seconds=20)
    tools = mn.Tools.find()
    source = mn.probe(source_path, tools)
    effect = mn.Effect.classic() if engine == "resample" else mn.Effect.stretched(1.2, 2)
    rubberband = mn.find_rubberband() if engine == "rubberband" else None
    reports = []
    mn.render(source, tmp_path / "out.mp3", effect, engine, tools,
              rubberband=rubberband, progress=reports.append)
    assert reports == sorted(reports)
    assert reports[-1] == pytest.approx(1, abs=0.02)
    if engine == "rubberband":
        assert reports[0] < 0.1
        assert max(b - a for a, b in zip(reports, reports[1:])) < 0.2


@needs_ffmpeg
@pytest.mark.parametrize("engine", ["resample", "rubberband", "ffmpeg-rubberband", "atempo"])
def test_commands(tmp_path, monkeypatch, engine):
    """The settings the measurements can't easily see."""
    needs_engine(engine)
    commands = []
    real_run, real_run_rubberband = mn._run, mn._run_rubberband

    def record(cmd, **kwargs):
        commands.append([str(arg) for arg in cmd])
        return real_run(cmd, **kwargs)

    def record_rubberband(cmd, **kwargs):
        commands.append(([str(arg) for arg in cmd], kwargs["cwd"]))
        return real_run_rubberband(cmd, **kwargs)
    monkeypatch.setattr(mn, "_run", record)
    monkeypatch.setattr(mn, "_run_rubberband", record_rubberband)
    source = make_tone(tmp_path / "tone.flac", seconds=1, layout="5.1(side)")
    args = [] if engine == "resample" else ["--tempo", "1.2", "--engine", engine]
    assert run(source, "--bass", "3", *args) == 0

    encode = commands[-1]
    graph = encode[encode.index("-filter_complex") + 1]
    assert "aformat=sample_fmts=fltp," in graph.split("asetrate")[0]  # floating point first
    assert encode[encode.index("-map_chapters") + 1] == "-1"
    assert "-af" not in encode and "-metadata" not in encode
    assert "aformat=sample_fmts=dblp,bass=g=3" in graph  # a double-precision filter
    assert "alimiter=limit=0.989" in graph
    if "latency" in mn._filter_options(FFMPEG, "alimiter"):
        assert "latency=1" in graph
    if engine == "ffmpeg-rubberband":
        assert "pitchq=quality" in graph
    if engine == "rubberband":
        decode, (stretch, cwd) = commands[0], commands[1]
        assert "volume=0.25" in decode
        assert graph.startswith("[0:a:0]channelmap=channel_layout=5.1(side),")
        assert "volume=4" in graph
        assert stretch[-2:] == ["decoded.wav", "stretched.wav"]
        assert Path(decode[-1]) == Path(cwd, "decoded.wav")
        assert all(flag in stretch for flag in mn._rubberband_flags(stretch[0]))
