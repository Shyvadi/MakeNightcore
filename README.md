# MakeNightcore

Turn any song into a nightcore version: faster and higher, like a record played at the wrong speed.

```
python3 MakeNightcore.py song.mp3
```

This writes `song (Nightcore).mp3` next to the original. The new file keeps the original's tags and cover art. (On Windows, type `python` or `py` instead of `python3`.)

## How it works

Nightcore is traditionally made by playing a song faster, so the tempo and pitch go up together. MakeNightcore does exactly that. By default the song plays 1.25× faster, which raises it by about 4 semitones. The audio is resampled, not time-stretched, so it sounds as clean as the original. There are none of the smeared, phasey artifacts that time-stretching or pitch-shifting adds. It uses the SoX resampler when your ffmpeg has it, and otherwise ffmpeg's own resampler tuned for high quality.

To set tempo and pitch separately, use `--tempo` and/or `--pitch`. The song is then time-stretched with [Rubber Band](https://breakfastquay.com/rubberband/), using its high-quality R3 engine when available.

## Requirements

- **Python 3.8 or newer.** No Python packages are needed.
- **[ffmpeg](https://ffmpeg.org/) 4.2 or newer** (4.4 and newer are tested), available on your `PATH`:
  - Windows: `winget install Gyan.FFmpeg`. You can also unzip an ffmpeg build into a folder next to `MakeNightcore.py`.
  - macOS: `brew install ffmpeg`. Homebrew's full build, `brew install ffmpeg-full`, adds the SoX resampler and Vorbis. MakeNightcore finds it even though Homebrew keeps it off your `PATH`.
  - Linux: `sudo apt install ffmpeg`, or your distribution's equivalent.
- **Rubber Band** (optional). You only need it for `--tempo` and `--pitch`:
  - Windows: nothing to do. The bundled `rubberband-3.2.1-gpl-executable-windows.zip` is unpacked automatically the first time it's needed, into `%LOCALAPPDATA%\MakeNightcore`.
  - macOS: `brew install rubberband`
  - Linux: `sudo apt install rubberband-cli`

  Without Rubber Band, the tool falls back to ffmpeg's rubberband filter, or to ffmpeg's lower-quality `atempo` filter.

To get a `makenightcore` command you can run from anywhere, install it with `pipx install .` or `uv tool install .`. Inside a virtual environment, `pip install .` works too. The installed command can't use the bundled Rubber Band, or an ffmpeg unzipped next to `MakeNightcore.py`, so put them on your `PATH` (for example with `winget install Gyan.FFmpeg`). For Rubber Band on Windows, unzip it and add the folder with `rubberband.exe` and `sndfile.dll` to your `PATH`.

## Usage

```
python3 MakeNightcore.py song.mp3                     # -> "song (Nightcore).mp3"
python3 MakeNightcore.py song.mp3 --speed 1.35        # faster and higher
python3 MakeNightcore.py song.mp3 --bass 6            # with a 6 dB bass boost
python3 MakeNightcore.py song.flac -f mp3             # choose the output format
python3 MakeNightcore.py song.mp3 -o out.wav          # choose the output file
python3 MakeNightcore.py *.mp3 -o nightcore/          # several songs into a folder
python3 MakeNightcore.py "My Music/"                  # every song in a folder
python3 MakeNightcore.py song.mp3 --tempo 1.2 --pitch 2   # tempo and pitch separately
python3 MakeNightcore.py song.mp3 --speed 0.8         # slowed down -> "song (Slowed).mp3"
```

| Option | What it does |
| --- | --- |
| `INPUT ...` | Audio files, or folders of audio files. Subfolders are not searched. Wildcards such as `*.mp3` work in every shell, including on Windows. `-s INPUT` works too. |
| `-o, --output PATH` | Output file or folder. It is a folder if it already is one, ends in `/`, or there are several inputs or a folder input. A file without an extension gets the one the input's format calls for. The default is `<name> (Nightcore).<ext>` next to each input. Missing folders are created. |
| `-f, --format FORMAT` | `mp3`, `m4a`, `ogg`, `opus`, `flac` or `wav`. The default keeps the input's format. Inputs in other formats become FLAC if they are lossless (Apple Lossless becomes M4A), and otherwise the format that matches their codec (MP3 if none does). |
| `--speed X` | Speed-up factor. The pitch rises with it. The default is `1.25`. |
| `--tempo X` | Tempo factor, without changing the pitch. |
| `--pitch SEMITONES` | Pitch shift, without changing the tempo. |
| `--bass DB` | Bass boost in dB. |
| `--engine NAME` | Time-stretcher for `--tempo`/`--pitch`: `rubberband`, `ffmpeg-rubberband` or `atempo`. The default picks the best one available. |
| `-y, --overwrite` | Overwrite existing output files. Otherwise they are left alone. |
| `-q, --quiet` | Print only errors and warnings. |
| `-v, --verbose` | Also show the commands being run. |

How `--speed` relates to pitch:

| `--speed` | 1.1 | 1.15 | 1.2 | **1.25** | 1.3 | 1.35 | 1.4 | 1.5 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| semitones | +1.65 | +2.42 | +3.16 | **+3.86** | +4.54 | +5.20 | +5.83 | +7.02 |

Version 1 of this tool time-stretched with `rubberband -t 0.85 -p 3`. To get the same settings, use `--tempo 1.176 --pitch 3`. It now uses Rubber Band's newer R3 engine, which sounds cleaner.

## What you get

- **Quality.**
  - Lossless files stay lossless, at their bit depth and sample rate. 16-bit stays 16-bit (dithered), 24-bit stays 24-bit, and 32-bit float WAV stays float. Apple Lossless (ALAC) stays ALAC.
  - Lossy formats are encoded at transparent settings: MP3 V0, Vorbis q6, and AAC 256k or Opus 192k for stereo (scaled with the number of channels). If your ffmpeg has no Vorbis encoder, Ogg files are written with Opus.
  - Surround sound keeps its channels. It is downmixed to stereo for MP3, and in M4A, Ogg and Opus files for layouts those can't store.
  - All processing happens in floating point.
- **No clipping.** A limiter stops peaks from going over full scale. Resampling can create small overs between samples, and bass boosts and Rubber Band's time-stretching can create big ones. The limiter's ceiling is -0.1 dBFS for lossless files and -2 dBFS for lossy ones, whose encoders overshoot. It leaves everything below the ceiling alone. (ffmpeg's AAC encoder can overshoot by more on loud, dense songs, especially with `--bass`, so an M4A file may still go over full scale for a moment. MP3, Vorbis and Opus stay below it.) Without a bass boost or Rubber Band, lossy files are not limited, just like any other conversion. Downmixes, to stereo for MP3 and for surround layouts that AAC, Opus and Vorbis can't store, are normalized so they can't clip either.
- **Tags.**
  - Tags are copied, and the title gets ` (Nightcore)` added.
  - The BPM tag is scaled to the new tempo. Chapters (in MP3 and M4A output) and the timestamps of synced lyrics move to match it.
  - Tags that would now be wrong are removed: ReplayGain, musical key, length, cue sheets, and the identifiers and fingerprint of the original recording (MusicBrainz track, AcoustID, ISRC).
- **Cover art.** The cover is kept in MP3, M4A and FLAC output; the front cover is used if there are several. Ogg, Opus and WAV can't carry it this way.
- **Safety.**
  - Existing files are never overwritten unless you pass `-y`, and the inputs are never overwritten at all.
  - A failed or interrupted run doesn't leave a half-written file behind. That includes Ctrl+C, and on macOS and Linux also `kill` and closing the terminal.
  - One bad file doesn't stop a batch.
- **Many inputs.** Pass several files, wildcards or whole folders. Running the same command again only makes what's new: songs whose result already exists are skipped (use `-y` to redo them), and so are earlier results given along with their originals, as `*.mp3` does. In a folder, files named like results (`... (Nightcore).mp3`) are always left out; give one by name to make it anyway. Any input ffmpeg can read works, including video files; only the audio is used.

## Use from Python

```python
from MakeNightcore import nightcore

nightcore("song.mp3")                                   # -> path of "song (Nightcore).mp3"
nightcore("song.mp3", "out.flac", speed=1.3, bass=6)
nightcore("song.mp3", "nightcore/", format="opus")      # into a folder
nightcore("song.mp3", tempo=1.2, pitch=3, overwrite=True)
```

`output` works like `-o`: a folder if it is one or a string ending in `/` (a `Path` drops the slash, so create the folder first), and otherwise a file. Each option matches the command-line option of the same name. Invalid options raise `ValueError`. Other problems raise `NightcoreError`, or `OSError` when the file system fails. Warnings are issued with Python's `warnings` module.

## Development

```
pip install pytest
pytest
```

The tests render real audio with ffmpeg and measure the results: pitch, length, level, peaks, dither, tags and cover art. They are skipped when ffmpeg isn't installed.

The bundled Rubber Band for Windows is licensed under the GPL. See `COPYING.txt` inside the zip.

Made by Shyvadi
