"""Vocalworks engine: runs the tools/ pipeline with per-song rules. Settings come from measuring the
original, or from the plan of the user's connected AI assistant (app/assistant.py), checked against
the same rules; nobody adjusts anything by hand.

The same song folders and vocalworks.json notes are used by the desktop app and by the
chat workflow (the vocalworks skill), so a song can move between them.

Song folder:  songs/<name>/song.mp3, song_vocals.wav, song_instrumental.wav, takeN.wav,
              takeN_* intermediates, my_vocal_processed.wav, <name>_my_cover.wav/.mp3,
              vocalworks.json
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SONGS = ROOT / "songs"
sys.path.insert(0, str(TOOLS))
import songfile  # noqa: E402

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".webm", ".wma", ".aiff", ".aif"}
KEEP = re.compile(r"^(song\.mp3|song_vocals\.wav|song_instrumental\.wav|take\d+\.wav|my_vocal_processed\.wav"
                  r"|.+_my_cover\.(wav|mp3)|vocalworks\.json|audioworks\.json|lyrics\.lrc)$")


class PipelineError(RuntimeError):
    pass


# ---------------------------------------------------------------- running tools

def run_tool(name, *args, log=print):
    """Run tools/<name>.py, streaming its output lines to `log`; returns the full output."""
    cmd = [sys.executable, "-u", str(TOOLS / f"{name}.py"), *map(str, args)]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=flags,
                            env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONWARNINGS": "ignore"})
    lines = []
    for raw in proc.stdout:
        for line in raw.replace("\r", "\n").split("\n"):
            line = line.rstrip()
            if line and "%|" not in line and "HF_TOKEN" not in line:  # skip progress bars and hub chatter
                lines.append(line)
                log(line)
    if proc.wait() != 0:
        raise PipelineError(f"{name} failed:\n" + "\n".join(lines[-12:]))
    return "\n".join(lines)


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def convert(src, dst, args=()):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    r = subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(src), "-vn", *args, str(dst)],
                       capture_output=True, text=True, creationflags=flags)
    if r.returncode != 0:
        raise PipelineError(f"Couldn't convert {Path(src).name}: {r.stderr.strip()[-300:]}")


# ---------------------------------------------------------------- songs

def slug(text):
    import unicodedata

    # keep letters readable instead of dropping them: "Yıldızlar" -> "yildizlar", "Ölü" -> "olu"
    text = text.replace("ı", "i").replace("İ", "I")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"\(.*?\)|\[.*?\]", "", text.lower())  # drop "(official video)" etc.
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "song"


def new_song_folder(title):
    base = slug(title)
    folder, n = SONGS / base, 2
    while folder.exists():
        folder, n = SONGS / f"{base}-{n}", n + 1
    folder.mkdir(parents=True)
    return folder


def list_songs():
    """[(folder, title, status)] for every song folder, newest first."""
    out = []
    for d in sorted(SONGS.glob("*/"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not (d / "song.mp3").exists():
            continue
        notes = songfile.load(d)
        title = notes.get("song", {}).get("title") or notes.get("source", {}).get("title") or d.name
        out.append((d, title, notes.get("status", "imported")))
    return out


def import_file(path, log=print):
    path = Path(path)
    if path.suffix.lower() not in AUDIO_EXTS:
        raise PipelineError(f"{path.suffix} isn't a supported audio format")
    folder = new_song_folder(path.stem)
    log(f"Importing {path.name} -> {folder.name}/song.mp3")
    if path.suffix.lower() == ".mp3":
        shutil.copy2(path, folder / "song.mp3")
    else:
        convert(path, folder / "song.mp3", ("-b:a", "320k"))
    songfile.merge(folder, source={"type": "file", "file": path.name, "title": path.stem}, status="imported")
    return folder


def clean_title(title, artist=""):
    """'The Strokes - Reptilia (Official HD Video)' -> ('The Strokes', 'Reptilia')."""
    t = re.sub(r"\s*[(\[][^)\]]*(official|video|audio|lyric|visuali[sz]er|hd|4k|remaster|explicit|clip)[^)\]]*[)\]]",
               "", title, flags=re.I).strip()
    t = re.sub(r"\s*[(\[][^)\]]*\b(19|20)\d\d\b[^)\]]*[)\]]", "", t).strip()  # "(Album, 2006)" notes
    if " - " in t:
        a, t = t.split(" - ", 1)
        artist = artist or a
    artist = re.sub(r"\s*(- Topic|VEVO)$", "", artist or "").strip()
    return artist, t.strip()


def download(url, log=print):
    """YouTube / SoundCloud audio -> new song folder (yt-dlp, converted with the bundled ffmpeg)."""
    import yt_dlp

    platform = "soundcloud" if "soundcloud.com" in url else "youtube" if re.search(r"youtu\.?be", url) else None
    if not platform:
        raise PipelineError("Only YouTube and SoundCloud links are supported")
    opts = {"quiet": True, "noplaylist": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("track"):
        artist, title = info.get("artist") or "", info["track"]
    else:
        uploader = re.sub(r"\s*(- Topic|VEVO)$", "", info.get("uploader") or "").strip()
        artist, title = clean_title(info.get("title") or "song", info.get("artist") or "")
        # "Song - Artist" titles: when the channel's name is on the title side, swap the two
        if uploader and artist and uploader.lower() in title.lower() and uploader.lower() not in artist.lower():
            artist, title = title, artist
        artist = artist or uploader
    folder = new_song_folder(f"{artist} {title}".strip())
    log(f"Downloading '{title}' from {platform} -> {folder.name}")

    def hook(d):
        if d.get("status") == "downloading" and d.get("total_bytes"):
            log(f"download {100 * d['downloaded_bytes'] / d['total_bytes']:.0f}%")

    opts.update({"format": "bestaudio/best", "outtmpl": str(folder / "download.%(ext)s"), "progress_hooks": [hook]})
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    raw = next(folder.glob("download.*"))
    convert(raw, folder / "song.mp3", ("-b:a", "320k"))
    raw.unlink()
    songfile.merge(folder, source={"type": platform, "url": url, "title": title, "artist": artist,
                                   "duration_s": info.get("duration")}, status="imported")
    return folder


def prepare(folder, log=print, with_assistant=False):
    """Separate and analyze: everything needed before the user records. with_assistant: the connected AI
    assistant then plans the cover from the measurements (app/assistant.py)."""
    folder = Path(folder)
    if not ((folder / "song_vocals.wav").exists() and (folder / "song_instrumental.wav").exists()):
        log("Separating vocals and instrumental (this takes a little while)...")
        run_tool("separate", folder / "song.mp3", log=log)
    log("Analyzing the original...")
    run_tool("analyze_song", folder, log=log)
    log("Checking the song for special vocal effects (vocoder, talk box)...")
    try:
        run_tool("song_features", folder, log=log)
    except PipelineError as e:  # the cover can still be made from the measurements alone
        log(f"Feature check failed ({e})")
    notes = songfile.load(folder)
    if notes.get("status") in (None, "imported", "analyzed"):
        notes["status"] = "analyzed"
    if not (folder / "lyrics.lrc").exists():
        info = find_lyrics(folder, notes, log)
        if info:
            notes["lyrics"] = info
    songfile.save(folder, notes)
    from app import assistant

    if with_assistant and assistant.connected():
        assistant.study(folder, log)
        notes = songfile.load(folder)
    return notes


def song_identity(notes):
    """(artist, title, duration_s, album) from the file's tags or where it came from."""
    s, src = notes.get("song", {}), notes.get("source", {})
    return (s.get("artist") or src.get("artist") or "", s.get("title") or src.get("title") or "",
            s.get("duration_s") or src.get("duration_s"), s.get("album"))


def find_lyrics(folder, notes, log=print):
    """Look up synced lyrics online; never fails the caller."""
    from app import lyrics

    artist, title, duration, album = song_identity(notes)
    if not title:
        return None
    log(f"Looking for lyrics: {artist} - {title}")
    try:
        return lyrics.get(folder, artist, title, duration, album, log)
    except Exception as e:  # noqa: BLE001 - lyrics are optional
        log(f"Lyrics lookup failed ({e}); you can add them in step 2")
        return None


def summary(notes):
    """Short human-readable description of a prepared song."""
    s, an, rec = notes.get("song", {}), notes.get("analysis", {}), notes.get("recommended", {})
    if not an:
        return "Not prepared yet: click Prepare below."
    src = notes.get("source", {})
    lines = [
        f"{s.get('artist') or src.get('artist', '')} - {s.get('title') or src.get('title', '')}".strip(" -"),
        f"Key {an['key']}{'' if an['key_stable'] else ' (moves around)'}, {an['tempo_bpm']:.0f} BPM"
        f"{', mono recording' if s.get('mono') else ''}",
        f"Vocals: {', '.join(an['vocal_sections'])}",
        f"Range {an['range'][0]}-{an['range'][1]}, centered on {an['range_median']}",
        f"Original vocal: {an['pitch_style'].lower()}, {'wide stereo' if an['wide'] else 'centered'}, "
        + (f"{an['reverb_s']:.1f} s {'stereo ' if an.get('reverb_stereo') else ''}reverb" if an.get("reverb_s")
           else "no reverb" if "reverb_s" in an else f"room tail {an['room_tail_db']:.0f} dB")
        + f", sits {an['vocal_vs_instrumental_lu']:+.1f} dB vs the music",
    ]
    plain = {"--max-cut 40": "deep tone matching (very dark original)", "--hiss": "tape hiss"}
    extras = [plain.get(f, f) for f in rec.get("match_flags", [])]
    if rec.get("widen"):
        extras.append("stereo widening")
    if rec.get("distortion"):
        extras.append("amp distortion")
    feats = notes.get("features", {})
    if feats.get("vocoder"):
        extras.append("vocoder")
    if extras:
        lines.append("Treatment: " + ", ".join(extras))
    if feats.get("why"):
        lines.append(f"Vocoder check: {feats['why']}")
    plan = notes.get("assistant_plan")
    if plan:
        from app import assistant

        lines.append(f"\nPlan by {plan.get('by', 'your assistant')} ({plan.get('made', '')}): {assistant.describe(plan)}")
        if plan.get("notes"):
            lines.append(plan["notes"])
    return "\n".join(lines)


# ---------------------------------------------------------------- takes

def next_take(folder):
    n = 1
    while (Path(folder) / f"take{n}.wav").exists():
        n += 1
    return f"take{n}"


def add_take_file(folder, path, log=print):
    take = next_take(folder)
    dst = Path(folder) / f"{take}.wav"
    if Path(path).suffix.lower() == ".wav":
        shutil.copy2(path, dst)
    else:
        convert(path, dst)
    log(f"Added {Path(path).name} as {take}.wav")
    return take


def takes(folder):
    return sorted((p.stem for p in Path(folder).glob("take*.wav") if re.fullmatch(r"take\d+", p.stem)),
                  key=lambda t: int(t[4:]))


# ---------------------------------------------------------------- processing

# above: the user found +1 dB over the original singer's balance still a bit low
# None = decided from the song's analysis; the app exposes no settings (the user wants everything matched)
DEFAULTS = {"tuning": "original", "above": 2.0, "hiss": None, "widen": None, "vocoder": None, "distortion": None}
DRIVES = (0, 4, 8, 12, 16)  # dB tried when copying an amp-driven original vocal


def pitch_settings(rec, tuning):
    a = rec["autotune"]
    strength, retune = a["strength"], a["retune_ms"]
    if tuning == "gentler":
        strength, retune = max(0.6, strength - 0.2), retune * 1.6
    elif tuning == "tighter":
        strength, retune = 1.0, max(10, retune / 2)
    return a["key"], round(strength, 2), round(retune), a["max_shift"]


def off_grid_cents(path):
    """Median distance from the nearest semitone while singing: the 'pitch character' of a vocal."""
    import music

    _, f0 = music.track_pitch(music.load_sound(path))
    mi = music.hz_to_midi(f0[~np.isnan(f0)])
    return float(np.median(np.abs(mi - np.round(mi))) * 100)


def tool_versions(*names):
    """Stage signatures include the tools' modification times, so improving a tool redoes its stages."""
    return {n: (TOOLS / f"{n}.py").stat().st_mtime for n in names}


def sibilance(path, log):
    out = run_tool("deess", path, "--amount", "light", "--analyze", log=log)
    return float(re.search(r"reduced in ([\d.]+)%", out).group(1))


def process(folder, take, options=None, log=print):
    """Turn takeN.wav into the finished vocal and cover. Stages whose inputs didn't change are reused."""
    folder = Path(folder)
    opts = {**DEFAULTS, **(options or {})}
    notes = songfile.load(folder)
    rec = notes.get("recommended")
    if not rec:
        raise PipelineError("Prepare the song first (step 1)")
    from app import assistant

    rec, planned = assistant.apply(notes, rec)
    for k, v in planned.items():
        if opts[k] is None or (k == "above" and "above" not in (options or {})):
            opts[k] = v
    if planned:
        log(f"Using {notes['assistant_plan'].get('by', 'the assistant')}'s plan for this song")
    if opts["hiss"] is None:
        opts["hiss"] = "--hiss" in rec.get("match_flags", [])
    if opts["widen"] is None:
        opts["widen"] = bool(rec.get("widen"))
    if opts["distortion"] is None:
        opts["distortion"] = bool(rec.get("distortion"))
    if opts["vocoder"] is None:
        # a version the user picked in chat work wins; otherwise song_features.py decided at prepare time
        if "features" not in notes:  # prepared before the feature check existed
            try:
                run_tool("song_features", folder, log=log)
                notes = songfile.load(folder)
            except PipelineError as e:
                log(f"Feature check failed ({e})")
        final = notes.get("final", {})
        if final.get("version") and final.get("made_with") != "app":
            opts["vocoder"] = final["version"] == "vocoder"
        else:
            opts["vocoder"] = bool(notes.get("features", {}).get("vocoder"))
    tk = notes.setdefault("takes", {}).setdefault(take, {})
    cache = tk.setdefault("app_cache", {})
    f = lambda suffix: folder / f"{take}_{suffix}.wav"  # noqa: E731
    vocals, song = folder / "song_vocals.wav", folder / "song.mp3"

    def stage(name, signature, outputs, work):
        sig = json.dumps(signature, sort_keys=True)
        if cache.get(name) == sig and all(Path(o).exists() for o in outputs):
            log(f"[{name}] unchanged, reusing")
            return False
        log(f"[{name}]")
        work()
        cache[name] = sig
        songfile.save(folder, notes)
        return True

    def do_align():
        out = run_tool("align", folder / f"{take}.wav", "--ref", vocals, "--song", song, "-o", f("aligned"), log=log)
        tk["offset_s"] = float(re.search(r"Offset used:\s+([+-][\d.]+)", out).group(1))
        m = re.search(r"Timing:\s+(.*)", out)
        tk["timing"] = m.group(1) if m else ""

    # re-preparing the song (new stems) or replacing the take invalidates everything downstream
    sig = {"take_mtime": (folder / f"{take}.wav").stat().st_mtime, "stems_mtime": vocals.stat().st_mtime,
           **tool_versions("align")}
    stage("align", sig, [f("aligned")], do_align)

    key, strength, retune, shift = pitch_settings(rec, opts["tuning"])
    sig = {**sig, "pitch": [key, strength, retune, shift], **tool_versions("autotune")}
    target_grid = notes.get("analysis", {}).get("off_grid_cents")

    def do_tune():
        def tune(s, r):
            run_tool("autotune", f("aligned"), "--melody", vocals, "--key", key, "--strength", s,
                     "--retune-ms", r, "--max-shift", shift, "-o", f("tuned"), log=log)
        tune(strength, retune)
        tk["pitch"] = {"strength": strength, "retune_ms": retune}
        if target_grid is not None:
            # fit the original as closely as possible: if still clearly looser than the original
            # singer, tighten once (still within the max-shift cap, so no big jumps)
            got = off_grid_cents(f("tuned"))
            log(f"Pitch character: yours {got:.1f} cents off the notes, original {target_grid:.1f}")
            if got > target_grid + 5 and (strength < 1 or retune > 25):
                s2, r2 = 1.0, max(25, round(retune * 0.6))
                log(f"Tightening the correction to fit the original (strength {s2}, {r2} ms)")
                tune(s2, r2)
                got = off_grid_cents(f("tuned"))
                log(f"Pitch character now {got:.1f} cents")
                tk["pitch"] = {"strength": s2, "retune_ms": r2, "auto_tightened": True}
            tk["pitch"]["off_grid_cents"] = round(got, 1)
            tk["pitch"]["original_off_grid_cents"] = target_grid

    stage("tune", sig, [f("tuned")], do_tune)

    # de-essing: the amount that lands closest to the original's sibilant share, or none
    target = rec.get("deess_target_pct", 5.0)

    def do_deess():
        have = sibilance(f("tuned"), log)
        if have <= target + 2:
            tk["deess"] = "none"
            shutil.copy2(f("tuned"), f("deessed"))
            return
        best = None
        for amount in ("light", "medium", "strong"):
            out = folder / f"{take}_deess_{amount}.wav"
            run_tool("deess", f("tuned"), "--amount", amount, "-o", out, log=log)
            score = abs(sibilance(out, log) - target)
            if best is None or score < best[0]:
                best = (score, amount, out)
        for amount in ("light", "medium", "strong"):
            p = folder / f"{take}_deess_{amount}.wav"
            if p != best[2]:
                p.unlink(missing_ok=True)
        best[2].replace(f("deessed"))
        tk["deess"] = best[1]

    sig = {**sig, "deess_target": target, **tool_versions("deess")}
    stage("deess", sig, [f("deessed")], do_deess)

    voc = opts["vocoder"]
    base = "vocoder_" if voc else ""
    if voc:
        stage("vocoder", {**sig, **tool_versions("vocoder")}, [f("vocoded")], lambda: run_tool("vocoder", f("tuned"), "-o", f("vocoded"), log=log))
    flags = [fl for fl in rec.get("match_flags", []) if fl != "--hiss"] + (["--hiss"] if opts["hiss"] else [])
    match_in = f("vocoded") if voc else f("deessed")
    dist = opts["distortion"]
    msig = {**sig, "vocoder": voc, "flags": flags, "distortion": dist, **tool_versions("match", "distort", "vocoder")}
    # vocoded or driven audio hides some sung moments from the pitch tracker; the tuned take has the same timing
    extra = ["--sung-from", f("tuned")] if (voc or dist) else []

    def run_match(src, out):
        run_tool("match", src, "--ref", vocals, *" ".join(flags).split(), *extra, "-o", out, log=log)

    if dist:
        an = notes.get("analysis", {})
        tk_kurt, tk_flat = an.get("kurtosis", -0.6), an.get("grit_flatness", 0.05)

        def do_drive():
            # amp-style drive, amount chosen by measuring the finished (matched) vocal against the
            # original: flattened waveform (kurtosis) weighted over grit (flatness, which stem bleed inflates)
            best = None
            for g in DRIVES:
                d_out, p_out = folder / f"{take}_drive{g}.wav", folder / f"{take}_drive{g}_matched.wav"
                run_tool("distort", match_in, "--drive", g, "-o", d_out, log=log)
                run_match(d_out, p_out)
                m = re.search(r"kurtosis ([+-][\d.]+).*?flatness 1-5 kHz ([\d.]+)",
                              run_tool("distort", p_out, "--analyze", log=log))
                k, fl = float(m.group(1)), float(m.group(2))
                err = abs(k - tk_kurt) / 0.3 + abs(fl - tk_flat) / 0.03
                log(f"drive {g} dB: kurtosis {k:+.2f}, grit {fl:.3f} (original {tk_kurt:+.2f}, {tk_flat:.3f})")
                if best is None or err < best[0]:
                    best = (err, g, d_out, p_out, k, fl)
            _, g, d_out, p_out, k, fl = best
            d_out.replace(f(f"{base}driven"))
            p_out.replace(f(f"{base}processed"))  # already matched: reuse it instead of matching again
            for g2 in DRIVES:
                for p in (folder / f"{take}_drive{g2}.wav", folder / f"{take}_drive{g2}_matched.wav"):
                    p.unlink(missing_ok=True)
            tk["drive"] = {"drive_db": g, "kurtosis": k, "grit": fl, "original_kurtosis": tk_kurt,
                           "original_grit": tk_flat}
            log(f"Chose {g} dB of drive")
            cache[f"{base}match"] = json.dumps(msig, sort_keys=True)

        stage(f"{base}drive", msig, [f(f"{base}driven"), f(f"{base}processed")], do_drive)
    stage(f"{base}match", msig, [f(f"{base}processed")], lambda: run_match(match_in, f(f"{base}processed")))

    final_vocal = f(f"{base}processed")
    if opts["widen"]:
        stage(f"{base}widen", {**msig, "widen": True, **tool_versions("double")}, [f(f"{base}wide")], lambda: run_tool(
            "double", f(f"{base}processed"), "--tightness", "tight", "--with-lead", "--match-width", vocals,
            "-o", f(f"{base}wide"), log=log))
        final_vocal = f(f"{base}wide")

    mix = f("mix")

    def do_mix():
        out = run_tool("mixdown", folder, "--vocal", final_vocal, "--above", opts["above"], "-o", mix, log=log)
        bal = re.search(r"vocal vs instrumental ([+-][\d.]+) LU \(original ([+-][\d.]+)", out)
        tk["mix"] = {"vocal_vs_instrumental_lu": float(bal.group(1)), "original_lu": float(bal.group(2)),
                     "note": next((ln for ln in out.splitlines() if ln.startswith("Note:")), "")}

    stage("mix", {**msig, "widen": opts["widen"], "above": opts["above"], **tool_versions("mixdown")}, [mix], do_mix)

    # exports: standard names in the song folder
    name = folder.name.replace("-", "_")
    log("[export]")
    write_wav16(final_vocal, folder / "my_vocal_processed.wav", normalize_peak_db=-1.0)
    write_wav16(mix, folder / f"{name}_my_cover.wav")
    write_mp3(mix, folder / f"{name}_my_cover.mp3")
    report = {**check(folder, mix, log), **tk.get("mix", {})}

    tk["settings"] = {k: opts[k] for k in DEFAULTS}
    tk["results"] = report
    notes["final"] = {"take": take, "vocal": "my_vocal_processed.wav", "cover": f"{name}_my_cover.wav/.mp3",
                      "version": "vocoder" if voc else "main", "made_with": "app"}
    notes["status"] = "awaiting feedback"
    songfile.save(folder, notes)
    if tk.get("profile"):  # sung with a vocal profile: this take teaches it (never affects the cover)
        from app import profiles

        profiles.learn(folder, take, tk["profile"], uploaded=bool(tk.get("uploaded")), log=log)
    return report


def write_wav16(src, dst, normalize_peak_db=None):
    x, sr = sf.read(str(src))
    peak = np.abs(x).max()
    if normalize_peak_db is not None and peak > 10 ** (normalize_peak_db / 20):
        x = x * 10 ** (normalize_peak_db / 20) / peak
    sf.write(str(dst), np.clip(x, -1, 1), sr, subtype="PCM_16")


def write_mp3(src, dst, kbps=320):
    """MP3 via LAME; re-encodes slightly quieter if encoding pushed peaks over 0 dBFS."""
    import lameenc

    x, sr = sf.read(str(src), always_2d=True)
    gain = 1.0
    for _ in range(3):
        pcm = (np.clip(x * gain, -1, 1) * 32767).astype("<i2")
        enc = lameenc.Encoder()
        enc.set_bit_rate(kbps)
        enc.set_in_sample_rate(sr)
        enc.set_channels(x.shape[1])
        enc.set_quality(2)
        Path(dst).write_bytes(enc.encode(pcm.tobytes()) + enc.flush())
        peak = np.abs(sf.read(str(dst))[0]).max()
        if peak < 1.0:
            return
        gain *= 0.97 / peak


def check(folder, mix, log=print):
    """Measure the finished mix against the original (the vocal balance comes from mixdown)."""
    import pyloudnorm

    folder = Path(folder)
    song, sr = sf.read(str(folder / "song.mp3"), always_2d=True)
    m, _ = sf.read(str(mix), always_2d=True)
    st = lambda x: x if x.shape[1] == 2 else np.repeat(x, 2, axis=1)  # noqa: E731
    meter = pyloudnorm.Meter(sr)
    r = lambda x: round(float(x), 1)  # noqa: E731
    msr = sf.info(str(mix)).samplerate
    if msr != sr or abs(len(m) / msr - len(song) / sr) > 0.1:
        raise PipelineError(f"The mix doesn't match the original ({len(m) / msr:.1f} s at {msr} Hz vs "
                            f"{len(song) / sr:.1f} s at {sr} Hz), so it would play at the wrong speed or length. "
                            "Not delivering it.")
    report = {
        "song_lufs": r(meter.integrated_loudness(st(m))), "original_lufs": r(meter.integrated_loudness(st(song))),
        "peak_dbfs": r(20 * np.log10(np.abs(m).max() + 1e-12)),
        "channels": int(m.shape[1]), "duration_s": r(len(m) / sr),
    }
    log(f"Result: {report['song_lufs']} LUFS (original {report['original_lufs']}), peak {report['peak_dbfs']} dBFS, "
        f"{'mono' if report['channels'] == 1 else 'stereo'}, {report['duration_s']} s")
    return report


def approve(folder, log=print):
    """Mark the cover final and move everything outside the keep-list to the Recycle Bin."""
    from send2trash import send2trash

    folder = Path(folder)
    for p in sorted(folder.iterdir()):
        if p.is_file() and not KEEP.match(p.name):
            send2trash(str(p))
            log(f"Recycled {p.name}")
    notes = songfile.load(folder)
    notes["status"] = "approved"
    for tk in notes.get("takes", {}).values():
        tk.pop("app_cache", None)  # intermediates are gone
    songfile.save(folder, notes)
    final = notes.get("final", {}).get("take")
    pid = notes.get("takes", {}).get(final, {}).get("profile")
    if pid:  # an approved cover counts double in its profile
        from app import profiles

        profiles.mark_approved(folder, final, pid)
