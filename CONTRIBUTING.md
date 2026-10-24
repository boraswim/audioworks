# Contributing to Audioworks

Thanks for helping. Bug reports, fixes and ideas are all welcome. Please follow the
[code of conduct](CODE_OF_CONDUCT.md), and report security problems privately as described in
[SECURITY.md](SECURITY.md), not in an issue.

## Reporting a bug

Open an issue with the **Bug report** form. The most useful details are what you did, what happened,
your Windows version and GPU, and the end of `audioworks.log` (in the app's folder). Please don't attach
songs, recordings or lyrics: they're copyrighted or personal, and the log is enough.

## Setting up

Audioworks is a Windows app in Python 3.11 with [uv](https://docs.astral.sh/uv/). In a clone, run
`Install Audioworks.bat`, or by hand:

```
uv venv .venv --python 3.11
uv pip install --python .venv\Scripts\python.exe -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cu126 --index-strategy unsafe-best-match
```

Start the app with `.venv\Scripts\python.exe -m app` to see its output in the console. The README's
**Code** table says what each file does.

## How the app is meant to work

A few principles shape every change:

- **No knobs.** The user picks a song, sings, and gets a cover; there are no settings for pitch, level or
  effects. A new feature has to decide for itself, from measurements of the original (or from the
  connected AI assistant's plan, which the app checks against the same rules).
- **Measure, then claim.** Each tool prints what it measured before and after. When you change
  processing, compare those numbers on a real song against the original.
- **Copy the original, add nothing.** An effect is applied only when the original measurably has it.
- **The user's voice stays theirs.** Pitch correction never moves a note more than 1.5 semitones and
  never snaps instantly.
- **The guides stay in sync.** If you change a production rule in `tools/` or `app/pipeline.py`,
  update `app/assistant_skill.md` (the AI assistant's guide) and the README to match.

## Making a change

1. Fork the repository and create a branch.
2. Match the style of the surrounding code: its naming, comment density and idioms.
3. Test with a real song folder: prepare a song in the app, record or upload a take, and make the cover.
   For a tool change, run the tool on its own (`.venv\Scripts\python.exe tools\<name>.py --help`) and
   check its before/after numbers.
4. In the window code (`app/main.py`), connect worker signals to window methods with queued
   connections, never to lambdas: a lambda runs on the worker thread and crashes Qt.
5. Never commit anything from `songs/`, logs, or personal settings (`.gitignore` covers them).
6. Open a pull request and fill in its template.

By contributing, you agree that your contributions are licensed under the project's
[GPL-3.0 license](LICENSE).
