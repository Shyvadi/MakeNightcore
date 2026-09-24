"""Tests for MakeNightcore.

Most tests render real audio with ffmpeg and check the result by measuring it:
a sine tone should come out at the right pitch, the right length, with its
tags and cover art. Those tests are skipped when ffmpeg is not installed.
"""

import array
import json
import math
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import MakeNightcore as mn

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg is not installed")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    *map(str, args)], check=True)


def ffprobe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                             "-show_format", "-show_streams", str(path)],
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


@pytest.fixture(scope="session")
def cover_png(tmp_path_factory):
    path = tmp_path_factory.mktemp("art") / "cover.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=32x32", "-frames:v", "1", path)
    return path


def make_tone(path, freq=440, seconds=2.0, rate=44100, tags=None, cover=None, args=()):
    """Write a stereo sine tone to `path` (format from its extension)."""
    cmd = ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate={rate}:duration={seconds}"]
    if cover:
        cmd += ["-i", cover, "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-disposition:v", "attached_pic", "-metadata:s:v", "comment=Cover (front)"]
    for key, value in (tags or {}).items():
        cmd += ["-metadata", f"{key}={value}"]
    ffmpeg(*cmd, "-ac", "2", *args, path)
    return Path(path)


def analyse(path):
    """Measure (duration in seconds, frequency in Hz, peak level 0-1)."""
    stream = audio_stream(path)
    rate, channels = int(stream["sample_rate"]), int(stream["channels"])
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
                          "-map", "0:a:0", "-f", "s16le", "-c:a", "pcm_s16le", "-"],
                         capture_output=True, check=True).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    left = samples[::channels]
    start, end = len(left) // 10, len(left) - len(left) // 10  # skip the edges
    rising = sum(1 for i in range(start + 1, end) if left[i - 1] < 0 <= left[i])
    frequency = rising / ((end - start) / rate)
    peak = max(abs(sample) for sample in samples) / 32768
    return len(left) / rate, frequency, peak


def run(*args):
    return mn.main([str(arg) for arg in args])


def available_engines():
    engines = ["atempo"]
    if shutil.which("ffmpeg") and "rubberband" in mn._ffmpeg_filters(shutil.which("ffmpeg")):
        engines.append("ffmpeg-rubberband")
    if mn.find_rubberband():
        engines.append("rubberband")
    return engines


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


def test_effect_rejects_nonsense():
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            mn.Effect.classic(bad)
        with pytest.raises(ValueError):
            mn.Effect.stretched(tempo=bad)
    with pytest.raises(ValueError):
        mn.Effect.stretched(pitch=float("nan"))


@pytest.mark.parametrize("factor", [0.1, 0.3, 0.5, 0.8, 1.0, 1.25, 2.0, 3.7, 10.0])
def test_atempo_chain_multiplies_to_factor(factor):
    steps = [float(f.split("=")[1]) for f in mn._atempo(factor)]
    assert math.prod(steps) == pytest.approx(factor)
    assert all(0.5 <= step <= 2.0 for step in steps)


def test_output_rate():
    assert mn._output_rate(44100, ".mp3") == 44100
    assert mn._output_rate(96000, ".mp3") == 48000
    assert mn._output_rate(88200, ".ogg") == 44100
    assert mn._output_rate(96000, ".flac") == 96000
    assert mn._output_rate(44100, ".opus") == 48000


def make_source(name="song.mp3", codec="mp3", lossless=False, tags=None):
    return mn.Source(path=Path(name), codec=codec, sample_rate=44100, duration=10.0, bits=16,
                     lossless=lossless, cover=None, tags=tags or {})


def test_output_tags_are_updated_for_the_new_version():
    source = make_source(tags={
        "TITLE": "Song", "artist": "Band", "TBPM": "120", "bpm": "oops",
        "REPLAYGAIN_TRACK_GAIN": "-7 dB", "r128_track_gain": "1", "iTunSMPB": "x",
        "encoder": "Lavf", "TKEY": "Am", "album": "Album",
    })
    tags = mn.output_tags(source, mn.Effect.classic(1.25))
    assert tags == {"TITLE": "Song (Nightcore)", "artist": "Band", "TBPM": "150", "album": "Album"}


def test_output_title_falls_back_to_file_name_and_is_not_doubled():
    assert mn.output_tags(make_source("My Track.flac"), mn.Effect.classic())["title"] == \
        "My Track (Nightcore)"
    source = make_source(tags={"title": "Song (Nightcore)"})
    assert mn.output_tags(source, mn.Effect.classic())["title"] == "Song (Nightcore)"
    assert mn.output_tags(source, mn.Effect.classic(0.8))["title"] == "Song (Nightcore) (Slowed)"


@pytest.mark.parametrize("name, codec, lossless, fmt, expected", [
    ("a.mp3", "mp3", False, None, "a (Nightcore).mp3"),
    ("a.FLAC", "flac", True, None, "a (Nightcore).flac"),
    ("a.webm", "opus", False, None, "a (Nightcore).opus"),
    ("a.mp4", "aac", False, None, "a (Nightcore).m4a"),
    ("a.aiff", "pcm_s16be", True, None, "a (Nightcore).flac"),
    ("a.wma", "wmav2", False, None, "a (Nightcore).mp3"),
    ("a.mp3", "mp3", False, "flac", "a (Nightcore).flac"),
])
def test_default_output_name(name, codec, lossless, fmt, expected):
    source = make_source(name, codec, lossless)
    assert mn.default_output(source, Path("out"), fmt, mn.Effect.classic()) == Path("out", expected)


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


def test_missing_ffmpeg_is_explained(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.mp3").write_bytes(b"")
    monkeypatch.setattr(mn, "find_program", lambda *names: None)
    assert run(tmp_path / "a.mp3") == 1
    assert "ffmpeg was not found" in capsys.readouterr().err


def test_missing_input_is_reported(tmp_path, capsys):
    assert run(tmp_path / "nope.mp3") == 1
    assert "no such file" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

@needs_ffmpeg
def test_default_is_classic_nightcore(tmp_path, capsys):
    source = make_tone(tmp_path / "tone.wav", seconds=2)
    assert run(source) == 0
    output = tmp_path / "tone (Nightcore).wav"
    duration, frequency, _ = analyse(output)
    assert duration == pytest.approx(1.6, abs=0.01)
    assert frequency == pytest.approx(550, rel=0.01)
    assert "tone.wav -> tone (Nightcore).wav" in capsys.readouterr().out


@needs_ffmpeg
def test_slowed(tmp_path):
    source = make_tone(tmp_path / "tone.flac", seconds=2)
    assert run(source, "--speed", "0.8") == 0
    duration, frequency, _ = analyse(tmp_path / "tone (Slowed).flac")
    assert duration == pytest.approx(2.5, abs=0.01)
    assert frequency == pytest.approx(352, rel=0.01)


@needs_ffmpeg
@pytest.mark.parametrize("engine", ["rubberband", "ffmpeg-rubberband", "atempo"])
@pytest.mark.parametrize("tempo, pitch", [(1.25, 0), (1.0, 12), (1.2, 3), (0.8, -2)])
def test_time_stretching(tmp_path, engine, tempo, pitch):
    if engine not in available_engines():
        pytest.skip(f"{engine} is not available")
    source = make_tone(tmp_path / "tone.wav", seconds=3)
    assert run(source, "--tempo", tempo, "--pitch", pitch, "--engine", engine) == 0
    name = "tone (Slowed).wav" if tempo < 1 else "tone (Nightcore).wav"
    duration, frequency, _ = analyse(tmp_path / name)
    assert duration == pytest.approx(3 / tempo, abs=0.03)
    assert frequency == pytest.approx(440 * 2 ** (pitch / 12), rel=0.01)


@needs_ffmpeg
def test_time_stretching_defaults(tmp_path):
    source = make_tone(tmp_path / "tone.wav", seconds=2)
    assert run(source, "--pitch", "12") == 0  # tempo stays at 1
    duration, frequency, _ = analyse(tmp_path / "tone (Nightcore).wav")
    assert duration == pytest.approx(2, abs=0.03)
    assert frequency == pytest.approx(880, rel=0.01)


@needs_ffmpeg
@pytest.mark.parametrize("fmt, codec", [("mp3", "mp3"), ("m4a", "aac"), ("ogg", "vorbis"),
                                        ("opus", "opus"), ("flac", "flac"), ("wav", "pcm_s16le")])
def test_output_formats(tmp_path, fmt, codec):
    source = make_tone(tmp_path / "tone.wav", seconds=2, tags={"title": "Tone", "artist": "Sine"})
    assert run(source, "-f", fmt) == 0
    output = tmp_path / f"tone (Nightcore).{fmt}"
    assert audio_stream(output)["codec_name"] == codec
    duration, frequency, _ = analyse(output)
    assert duration == pytest.approx(1.6, abs=0.06)
    assert frequency == pytest.approx(550, rel=0.01)
    tags = tags_of(output)
    assert tags["title"] == "Tone (Nightcore)"
    assert tags["artist"] == "Sine"


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["mp3", "m4a", "flac"])
def test_cover_art_and_tags_are_kept(tmp_path, cover_png, fmt):
    source = make_tone(tmp_path / "song.mp3", cover=cover_png, tags={
        "title": "Song", "artist": "Band", "album": "Album", "TBPM": "120",
        "REPLAYGAIN_TRACK_GAIN": "-7.00 dB"})
    assert run(source, "-f", fmt) == 0
    output = tmp_path / f"song (Nightcore).{fmt}"
    tags = tags_of(output)
    assert tags["title"] == "Song (Nightcore)"
    assert tags["artist"] == "Band"
    assert tags["album"] == "Album"
    assert "replaygain_track_gain" not in tags
    if fmt == "mp3":
        assert tags["tbpm"] == "150"
    covers = cover_streams(output)
    assert len(covers) == 1
    if fmt != "m4a":  # MP4 has no picture types
        assert covers[0]["tags"]["comment"] == "Cover (front)"


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
def test_video_input_keeps_audio_only(tmp_path):
    source = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-f", "lavfi",
           "-i", "color=c=blue:s=64x64:d=2", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", source)
    assert run(source) == 0
    output = tmp_path / "clip (Nightcore).m4a"
    assert [s["codec_type"] for s in ffprobe(output)["streams"]] == ["audio"]
    assert analyse(output)[1] == pytest.approx(550, rel=0.01)


@needs_ffmpeg
@pytest.mark.parametrize("name, args, fmt, expected", [
    ("in16.flac", [], "flac", ("s16", "16")),
    ("in24.flac", ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"], "flac",
     ("s32", "24")),
    ("in24.wav", ["-c:a", "pcm_s24le"], "wav", ("s32", "24")),
    ("in16.wav", [], "wav", ("s16", "16")),
    ("lossy.mp3", [], "flac", ("s16", "16")),
    ("lossy.mp3", [], "wav", ("s16", "16")),
])
def test_bit_depth_follows_the_source(tmp_path, name, args, fmt, expected):
    source = make_tone(tmp_path / name, args=args)
    assert run(source, "-f", fmt) == 0
    stream = audio_stream(tmp_path / f"{Path(name).stem} (Nightcore).{fmt}")
    bits = stream.get("bits_per_raw_sample") if fmt == "flac" else str(stream["bits_per_sample"])
    assert (stream["sample_fmt"], bits) == expected


@needs_ffmpeg
@pytest.mark.parametrize("fmt, rate", [("flac", 96000), ("mp3", 48000), ("opus", 48000)])
def test_sample_rate(tmp_path, fmt, rate):
    source = make_tone(tmp_path / "hires.flac", rate=96000, seconds=1)
    assert run(source, "-f", fmt) == 0
    output = tmp_path / f"hires (Nightcore).{fmt}"
    assert int(audio_stream(output)["sample_rate"]) == rate
    assert analyse(output)[1] == pytest.approx(550, rel=0.01)


@needs_ffmpeg
@pytest.mark.parametrize("args", [[], ["--tempo", "1.2", "--pitch", "3"]])
def test_bass_boost_never_clips(tmp_path, args):
    source = tmp_path / "loud.wav"
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=0.94*sin(2*PI*60*t):s=44100:d=2", "-ac", "2", source)
    assert run(source, "--bass", "12", *args) == 0
    _, _, peak = analyse(tmp_path / "loud (Nightcore).wav")
    assert 0.8 < peak <= 0.9  # limited to -1 dBFS, not normalized


@needs_ffmpeg
def test_level_is_untouched_without_bass_boost(tmp_path):
    source = make_tone(tmp_path / "tone.wav")
    assert run(source) == 0
    _, _, before = analyse(source)
    _, _, after = analyse(tmp_path / "tone (Nightcore).wav")
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
    assert run(source, "-o", source, "-y") == 1
    assert "replace the source" in capsys.readouterr().err
    assert source.read_bytes() == before


@needs_ffmpeg
def test_legacy_command_line(tmp_path):
    source = make_tone(tmp_path / "input.mp3", seconds=1)
    output = tmp_path / "output.wav"
    assert run("-s", source, "-o", output) == 0
    assert audio_stream(output)["codec_name"] == "pcm_s16le"
    assert analyse(output)[1] == pytest.approx(550, rel=0.01)


@needs_ffmpeg
def test_output_without_extension_keeps_the_format(tmp_path):
    source = make_tone(tmp_path / "tone.ogg", seconds=1)
    assert run(source, "-o", tmp_path / "result") == 0
    assert audio_stream(tmp_path / "result.ogg")["codec_name"] == "vorbis"


@needs_ffmpeg
def test_output_folder_is_created(tmp_path):
    source = make_tone(tmp_path / "tone.mp3", seconds=1)
    assert run(source, "-o", str(tmp_path / "new" / "folder") + os.sep) == 0
    assert (tmp_path / "new" / "folder" / "tone (Nightcore).mp3").is_file()


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
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == \
        ["a (Nightcore).mp3", "b (Nightcore).wav"]
    assert "2 done, 0 failed" in capsys.readouterr().out


@needs_ffmpeg
def test_two_inputs_with_the_same_output(tmp_path, capsys):
    a = make_tone(tmp_path / "a.mp3", seconds=1)
    b = make_tone(tmp_path / "a.wav", seconds=1)
    assert run(a, b, "-f", "mp3", "-o", tmp_path / "out") == 1
    assert "written twice" in capsys.readouterr().err
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["a (Nightcore).mp3"]


@needs_ffmpeg
@pytest.mark.parametrize("name", [
    "with spaces.mp3", "quote's \"here\".mp3", "colon: yes.mp3", "ünïcødé ♪ 日本.mp3",
    "-starts-with-dash.mp3", "$(echo hi) `x` ;&|.mp3", "100% [remix] {1}.mp3",
])
def test_awkward_file_names(tmp_path, monkeypatch, name):
    if os.name == "nt" and any(c in name for c in '"*:<>?|'):
        pytest.skip("not a valid file name on Windows")
    source = make_tone(tmp_path / "plain.mp3", seconds=1)
    source = source.rename(tmp_path / name)
    monkeypatch.chdir(tmp_path)
    assert run("--", name) == 0  # a relative path, like a user would type it
    assert (tmp_path / f"{Path(name).stem} (Nightcore).mp3").is_file()


@needs_ffmpeg
def test_failure_leaves_nothing_behind(tmp_path, capsys):
    bad = tmp_path / "bad.mp3"
    bad.write_text("this is not audio")
    assert run(bad) == 1
    assert "bad.mp3: cannot read the file" in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir()] == ["bad.mp3"]


@needs_ffmpeg
def test_encoder_failure_leaves_nothing_behind(tmp_path, monkeypatch, capsys):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    monkeypatch.setitem(mn.ENCODERS, ".mp3", ["-c:a", "no_such_encoder"])
    assert run(source, "-f", "mp3") == 1
    assert "ffmpeg failed" in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir()] == ["tone.wav"]


@needs_ffmpeg
@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_output_has_normal_permissions(tmp_path):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source) == 0
    mode = stat.S_IMODE((tmp_path / "tone (Nightcore).wav").stat().st_mode)
    umask = os.umask(0)
    os.umask(umask)
    assert mode == 0o666 & ~umask


@needs_ffmpeg
def test_quiet(tmp_path, capsys):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    assert run(source, "-q") == 0
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# Library use
# --------------------------------------------------------------------------

@needs_ffmpeg
def test_library_function(tmp_path):
    source = make_tone(tmp_path / "tone.wav", seconds=1)
    output = mn.nightcore(source)
    assert output == tmp_path / "tone (Nightcore).wav"
    assert analyse(output)[1] == pytest.approx(550, rel=0.01)
    custom = mn.nightcore(source, tmp_path / "custom.flac", speed=1.5)
    assert analyse(custom)[1] == pytest.approx(660, rel=0.01)
    with pytest.raises(mn.NightcoreError, match="already exists"):
        mn.nightcore(source)
    with pytest.raises(ValueError):
        mn.nightcore(source, speed=1.2, pitch=3)


@needs_ffmpeg
@pytest.mark.parametrize("engine", ["resample", "rubberband", "ffmpeg-rubberband", "atempo"])
def test_progress_is_reported(tmp_path, engine):
    if engine != "resample" and engine not in available_engines():
        pytest.skip(f"{engine} is not available")
    source_path = make_tone(tmp_path / "tone.wav", seconds=5)
    tools = mn.Tools.find()
    source = mn.probe(source_path, tools)
    effect = mn.Effect.classic() if engine == "resample" else mn.Effect.stretched(1.2, 2)
    rubberband = mn.find_rubberband() if engine == "rubberband" else None
    reports = []
    mn.render(source, tmp_path / "out.mp3", effect, engine, tools,
              rubberband=rubberband, progress=reports.append)
    assert reports
    assert reports == sorted(reports)
    assert 0 <= reports[0] and reports[-1] == pytest.approx(1, abs=0.02)


@pytest.mark.skipif(os.name != "nt", reason="the bundled Rubber Band is for Windows")
def test_bundled_rubberband_is_found_on_windows(monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable))
    found = mn.find_rubberband()
    assert found and Path(found).is_relative_to(mn.SCRIPT_DIR)
