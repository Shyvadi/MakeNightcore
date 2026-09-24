# MakeNightcore

Turn any song into a nightcore version: faster and higher, like a record played at the wrong speed.

```
python MakeNightcore.py song.mp3
```

This writes `song (Nightcore).mp3` next to the original. The new file keeps the original's tags and cover art.

## How it works

Nightcore is traditionally made by playing a song faster, so the tempo and pitch go up together. MakeNightcore does exactly that. By default the song plays 1.25× faster, which raises it by about 4 semitones. The audio is resampled with the SoX resampler, so it sounds as clean as the original. There are none of the smeared, phasey artifacts that time-stretching or pitch-shifting adds.

To set tempo and pitch separately, use `--tempo` and/or `--pitch`. The song is then time-stretched with [Rubber Band](https://breakfastquay.com/rubberband/)'s high-quality R3 engine.

## Requirements

- **Python 3.8 or newer.** No Python packages are needed.
- **[ffmpeg](https://ffmpeg.org/)**, available on your `PATH`:
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`
- **Rubber Band** (optional). You only need it for `--tempo` and `--pitch`:
  - Windows: nothing to do. The bundled `rubberband-3.2.1-gpl-executable-windows.zip` is unpacked automatically the first time it's needed.
  - macOS: `brew install rubberband`
  - Linux: `sudo apt install rubberband-cli`

  Without Rubber Band, the tool falls back to ffmpeg's rubberband filter, or to ffmpeg's lower-quality `atempo` filter.

To get a `makenightcore` command you can run from anywhere, install it with `pip install .`.

## Usage

```
python MakeNightcore.py song.mp3                     # -> "song (Nightcore).mp3"
python MakeNightcore.py song.mp3 --speed 1.35        # faster and higher
python MakeNightcore.py song.mp3 --bass 6            # with a 6 dB bass boost
python MakeNightcore.py song.flac -f mp3             # choose the output format
python MakeNightcore.py song.mp3 -o out.wav          # choose the output file
python MakeNightcore.py *.mp3 -o nightcore/          # several songs into a folder
python MakeNightcore.py "My Music/"                  # every song in a folder
python MakeNightcore.py song.mp3 --tempo 1.2 --pitch 2   # tempo and pitch separately
python MakeNightcore.py song.mp3 --speed 0.8         # slowed down -> "song (Slowed).mp3"
```

| Option | What it does |
| --- | --- |
| `INPUT ...` | Audio files, or folders of audio files. `-s INPUT` works too. |
| `-o, --output PATH` | Output file. With several inputs, this is an output folder. The default is `<name> (Nightcore).<ext>` next to each input. |
| `-f, --format FORMAT` | `mp3`, `m4a`, `ogg`, `opus`, `flac` or `wav`. The default keeps the input's format. |
| `--speed X` | Speed-up factor. The pitch rises with it. The default is `1.25`. |
| `--tempo X` | Tempo factor, without changing the pitch. |
| `--pitch SEMITONES` | Pitch shift, without changing the tempo. |
| `--bass DB` | Bass boost in dB. A limiter stops the boost from clipping. |
| `--engine NAME` | Time-stretcher for `--tempo`/`--pitch`: `rubberband`, `ffmpeg-rubberband` or `atempo`. The default picks the best one available. |
| `-y, --overwrite` | Overwrite existing output files. Otherwise they are left alone. |
| `-q, --quiet` / `-v, --verbose` | Print only errors, or also show the ffmpeg commands being run. |

How `--speed` relates to pitch:

| `--speed` | 1.1 | 1.15 | 1.2 | **1.25** | 1.3 | 1.35 | 1.4 | 1.5 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| semitones | +1.65 | +2.42 | +3.16 | **+3.86** | +4.54 | +5.20 | +5.83 | +7.02 |

Version 1 of this tool time-stretched with `rubberband -t 0.85 -p 3`. To get that exact sound, use `--tempo 1.176 --pitch 3`.

## What you get

- **Quality.** Lossless files stay lossless and keep their bit depth: 16-bit stays 16-bit (dithered), and 24-bit stays 24-bit. Lossy formats are encoded at transparent settings: MP3 V0, AAC 256k, Vorbis q6 or Opus 192k. All processing happens in floating point.
- **Tags.** Tags are copied, and the title gets ` (Nightcore)` added. The BPM tag is scaled to the new tempo. Tags that would now be wrong are removed: ReplayGain, musical key, length and chapters.
- **Cover art.** The cover is kept in MP3, M4A and FLAC output. Ogg, Opus and WAV can't carry it this way.
- **Safety.** Existing files are never overwritten unless you pass `-y`, and the source file is never overwritten at all. A failed or interrupted run doesn't leave a half-written file behind.
- **Many inputs.** Pass several files or whole folders. Earlier `(Nightcore)` results inside a folder are skipped. Any input ffmpeg can read works, including video files; only the audio is used.

## Use from Python

```python
from MakeNightcore import nightcore

nightcore("song.mp3")                                   # -> Path("song (Nightcore).mp3")
nightcore("song.mp3", "out.flac", speed=1.3, bass=6)
nightcore("song.mp3", tempo=1.2, pitch=3, overwrite=True)
```

## Development

```
pip install pytest
pytest
```

The tests render real audio with ffmpeg and measure the results: pitch, length, level, tags and cover art. They are skipped when ffmpeg isn't installed.

The bundled Rubber Band for Windows is licensed under the GPL. See `COPYING.txt` inside the zip.

Made by Shyvadi
