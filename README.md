# Audioworks

A Windows desktop app for singing covers. Pick a song, record yourself singing along, and get
back the song with your voice in it, processed to sound like the original vocal: pitch
correction, tone, compression, room, stereo width, distortion, tape hiss and loudness are all
measured from the original and matched automatically.

Everything runs on your own PC. There's no AI service or account involved; the one machine-learning
component, the vocal/instrumental splitter ([Demucs](https://github.com/facebookresearch/demucs)),
runs locally. Everything else is classic signal processing.

## Using it

1. **Song**: choose a local audio file (any common format), or paste a YouTube or SoundCloud link.
   Only download songs you have the rights to use; downloading may be against the platform's terms.
   The app splits the song into vocal and instrumental, analyzes how the original vocal was produced,
   and looks up synced lyrics.
2. **Vocals**: record in the app. The song plays in your headphones while your mic records, with a
   one-second count-in, and the lyrics scroll karaoke-style. Or upload a recording you made yourself
   (start recording before you start the song).
3. **Result**: click "Make my cover". There's nothing to set: pitch correction, timing, your vocal
   level and the vocal's effects (amp distortion, tape hiss, stereo width) are all matched to the
   original automatically. Listen, download the WAV and MP3, and click "Sounds good" to tidy the
   song's folder.

## Setup

Requires Windows, Python 3.11 and [uv](https://docs.astral.sh/uv/). An NVIDIA GPU makes splitting
songs much faster; it works on the CPU too, just slower.

```
uv venv .venv --python 3.11
uv pip install --python .venv\Scripts\python.exe -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cu126 --index-strategy unsafe-best-match
```

Start the app by double-clicking `Audioworks.bat`, or run `.venv\Scripts\pythonw.exe -m app`.
For a shortcut with the app icon, create one that runs `.venv\Scripts\pythonw.exe -m app` in the
project folder, with `app\assets\audioworks.ico` as its icon.

The Demucs model downloads automatically the first time a song is split.

## Where things are kept

Songs live in `songs/<song-name>/` (created automatically, not part of the repository):

```
song.mp3                      the original
song_vocals.wav               the original vocal, split out (melody and production reference)
song_instrumental.wav         the backing track
take1.wav, take2.wav ...      your recordings (always kept)
my_vocal_processed.wav        your finished vocal
<song_name>_my_cover.wav/.mp3 your cover
audioworks.json               the song's notes: analysis, settings, takes, results, status
lyrics.lrc                    synced lyrics for the karaoke view
```

While a cover is being made, working files (`takeN_aligned.wav`, `takeN_tuned.wav`, ...) appear
alongside; "Sounds good" moves them to the Recycle Bin. The app writes a log to `audioworks.log`.

## Code

| Path | Job |
|---|---|
| `app/main.py` | The 3-step window (PySide6) |
| `app/pipeline.py` | The engine: import or download, prepare, process a take with per-song rules, export, tidy up |
| `app/recorder.py` | In-app recording: plays the song and records the mic in one duplex audio stream |
| `app/lyrics.py` | Synced lyrics: LRCLIB lookup, LRC parsing, automatic sync to the song, timing for plain lyrics |
| `app/make_icon.py` | Draws the app icon into `app/assets/` |
| `tools/separate.py` | Splits a song into vocal and instrumental (Demucs) |
| `tools/analyze_song.py` | Key, tempo, vocal sections and range, how the original vocal is produced, recommended settings |
| `tools/align.py` | Measures where a recording starts relative to the song and lines it up |
| `tools/autotune.py` | Pitch correction toward the original melody (PSOLA) |
| `tools/deess.py` | Tames harsh "s" sounds |
| `tools/distort.py` | Amp-style vocal saturation |
| `tools/vocoder.py` | Vocoder effect |
| `tools/match.py` | Copies the original vocal's EQ, evenness, room, loudness, peaks and noise floor |
| `tools/double.py` | Stereo widening by double-tracking |
| `tools/mixdown.py` | Mixes at the original's vocal balance and masters to its loudness |
| `tools/music.py`, `tools/songfile.py` | Shared helpers: pitch tracking, keys, PSOLA; the song notes file |

Each tool also runs on its own from the command line (`.venv\Scripts\python.exe tools\<name>.py --help`).
