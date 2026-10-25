# Security policy

## Supported versions

Security fixes go into the latest release. If you're on an older version, update by downloading the
newest release and running `Install Vocalworks.bat` again.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Older releases | No |

## Reporting a vulnerability

Please report security problems privately, not in a public issue:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability** and describe the problem: what's affected, how to reproduce it, and
   what an attacker could do with it.

Only the maintainer sees the report. You can expect a first reply within a week. Once a fix is
released, the report can be published with credit to you, if you'd like.

## What's in scope

Vocalworks runs on your own PC, but a few parts deal with things from outside it, and problems there
are the most useful to report:

- **Downloads:** song links are fetched with yt-dlp, and files are converted with ffmpeg.
- **Lyrics and song notes:** lyrics come from LRCLIB and song notes from Wikipedia.
- **The optional AI assistant:** Vocalworks starts the assistant's command-line app (Gemini CLI, Codex
  CLI or Claude Code) inside the app's folder, and that app may write files there. Ways to make it act
  outside that folder, or to inject instructions through a song's data, are in scope.
- **The installer:** `Install Vocalworks.bat` downloads uv, Python and the app's libraries.

Bugs in those outside projects themselves (yt-dlp, ffmpeg, PyTorch, the assistants' apps and others)
belong with those projects. Do tell us if Vocalworks uses one of them in an unsafe way.
