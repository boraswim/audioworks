# Audioworks

A Windows desktop app for singing covers. Pick a song, record yourself singing along, and get
back the song with your voice in it, processed to sound like the original vocal: pitch
correction, tone, compression, room, stereo width, distortion, tape hiss and loudness are all
measured from the original and matched automatically.

Everything runs on your own PC, with no account needed. The one machine-learning component, the
vocal/instrumental splitter ([Demucs](https://github.com/facebookresearch/demucs)), runs locally;
everything else is classic signal processing. The only online lookups are lyrics from LRCLIB and the
song's Wikipedia article. You can optionally connect an AI assistant you already use to plan your
covers (see below).

## Using it

1. **Song**: choose a local audio file (any common format), or paste a YouTube or SoundCloud link,
   and click "Get song". Only download songs you have the rights to use; downloading may be against
   the platform's terms. Then click **Prepare** under "About this song": the app splits the song into
   vocal and instrumental, analyzes how the original vocal was produced, checks the song for effects
   that measuring alone can't settle (a vocoder or talk box, from the song's Wikipedia article and the
   vocal itself), and looks up synced lyrics. With an AI assistant connected, **Prepare with ...** also
   has it plan the cover. Both buttons become "Prepare again" afterwards.
2. **Vocals**: record in the app. The song plays in your headphones at half volume (so you can hear
   yourself) while your mic records, with a one-second count-in, and the lyrics scroll karaoke-style. Or upload a recording you made yourself
   (start recording before you start the song).
3. **Result**: click "Make my cover". There's nothing to set: pitch correction, timing, your vocal
   level and the vocal's effects (amp distortion, tape hiss, stereo width, vocoder) are all matched
   to the original automatically. Listen, download the WAV and MP3, and click "Sounds good" to tidy the
   song's folder.

## Install

1. Download `Audioworks-<version>-windows.zip` from the
   [latest release](https://github.com/boraswim/audioworks/releases/latest) and unzip it where you want
   the app to live (it keeps your songs in that folder too).
2. Double-click **`Install Audioworks.bat`**. It installs Python 3.11 and the app's libraries into the
   folder (about 3 GB with an NVIDIA GPU, which makes splitting songs much faster; less without one,
   where splitting takes several minutes), and adds Desktop and Start menu shortcuts.
3. Start Audioworks from the shortcut. The vocal splitter's model (about 300 MB) downloads the first
   time you prepare a song.

Running the installer again repairs or updates the install.

### From source

Requires Windows, Python 3.11 and [uv](https://docs.astral.sh/uv/). `Install Audioworks.bat` works in a
clone too; or by hand:

```
uv venv .venv --python 3.11
uv pip install --python .venv\Scripts\python.exe -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cu126 --index-strategy unsafe-best-match
```

Then start the app with `Audioworks.bat` or `.venv\Scripts\pythonw.exe -m app`.

## AI assistant (optional)

Click **Connect AI assistant** (above "+ New song") to let an assistant you already use plan your
covers: **Gemini** (Google account), **ChatGPT / Codex** (ChatGPT account) or **Claude** (Claude
account). The dialog installs the assistant's official command-line app (needs
[Node.js](https://nodejs.org)) and opens its sign-in; your account stays with that app on your PC, and
Audioworks has no server and stores no keys.

Once connected, **Prepare with ...** in step 1 ends with the assistant reading the measurements and the Audioworks
guide (`app/assistant_skill.md`), adding what it knows about the record, and writing a plan: pitch
correction, de-essing, tone, hiss, distortion, double-tracking, vocoder and your vocal level. The app
checks the plan against the same rules (for example, pitch correction never moves a note more than
1.5 semitones) and builds the cover from it. Plain **Prepare** leaves the assistant out, and "Prepare
again with ..." re-plans a song.
The assistant may only write its plan file; the AI itself runs on its company's servers.
To go easy on your plan's limits, Audioworks asks for each assistant's lighter model (Gemini Flash,
GPT-6 Luna, Claude Sonnet) and falls back to its default model if that one isn't available. Without an
assistant, everything is decided automatically from the measurements.

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
| `app/assistant.py`, `app/assistant_skill.md` | Optional AI assistant: connecting it, asking it for a plan, checking that plan; its guide |
| `app/lyrics.py` | Synced lyrics: LRCLIB lookup, LRC parsing, automatic sync to the song, timing for plain lyrics |
| `app/make_icon.py` | Draws the app icon into `app/assets/` |
| `tools/separate.py` | Splits a song into vocal and instrumental (Demucs) |
| `tools/analyze_song.py` | Key, tempo, vocal sections and range, how the original vocal is produced, recommended settings |
| `tools/song_features.py` | Decides whether the cover needs a vocoder: what's written about the song plus a check of the vocal |
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

## Contributing

Bug reports and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Please report
security problems privately, as described in [SECURITY.md](SECURITY.md).

## License

Audioworks is free software under the [GNU General Public License v3.0](LICENSE) (GPL-3.0): you may
use, study, change and share it, and anything you share that's built on it must be released under the
same license, with its source. Copyright (C) 2026 Bora Sevim.

It's GPL because it builds on [Parselmouth](https://github.com/YannickJadoul/Parselmouth) (Praat), which
is GPL-3.0. The other libraries it installs keep their own licenses (MIT, BSD, ISC, Apache, LGPL,
Unlicense), all compatible with this one.

The license covers the app's code only. The songs you download or import stay subject to their
rights holders' terms, and so does their use in the covers you make.
