"""Vocal profiles: one singer on one setup (mic, room, distance), learned from a guided recording and
then from every take sung with it.

A profile lives in profiles/<name>/: profile.json and the guided recording's clips (kept, so they can
be measured again when the measurements improve). A different setup means a different profile; a new
profile can start from another profile's voice measurements ("same singer as") and then only needs
the setup recorded again.

Phase 1: profiles are measured, learned and shown, but don't change how covers are made yet.
Everything here stays on the user's PC.
"""
import datetime
import json
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(ROOT / "tools"))
import music  # noqa: E402

SR = 48000  # the guided recording's rate (generated tones and the recording share it)
PREROLL = 0.5

# the guided recording, in order. "voice" steps describe the singer and can be carried over to a new
# profile for the same singer; the others describe the setup and are always recorded again.
STEPS = [
    {"id": "room", "title": "Room tone", "seconds": 6, "voice": False,
     "say": "Stay completely silent and still, with your mic where it is when you sing.",
     "big": "(silence)"},
    {"id": "level", "title": "Loudest phrase", "seconds": 8, "voice": False,
     "say": "Sing the loudest phrase you'd sing in a song, at your usual distance from the mic.",
     "big": "Sing loud!"},
    {"id": "range", "title": "Your range", "seconds": 14, "voice": True,
     "say": "On an \"ah\", glide slowly from your lowest comfortable note up to your highest comfortable "
            "note and back down. Comfortable, not strained.",
     "big": "low  ->  high  ->  low"},
    {"id": "pitch", "title": "Pitch", "seconds": None, "voice": True,
     "say": "You'll hear a note, then a short pause: sing the same note back on an \"ah\" in the pause and "
            "hold it until the next note. Any octave is fine.",
     "big": "listen, then sing it back"},
    {"id": "dynamics", "title": "Soft and loud", "seconds": 14, "voice": False,
     "say": "Sing the line below twice on one note: first softly, then loudly.",
     "big": "she sells sea shells by the sea shore"},
    {"id": "echo", "title": "Room echo", "seconds": 7, "voice": False,
     "say": "Say a short, loud \"ha!\" three times, with a two-second pause after each.",
     "big": "ha! ... ha! ... ha!"},
]
STEP = {s["id"]: s for s in STEPS}
VOICE_STEPS = [s["id"] for s in STEPS if s["voice"]]


# ---------------------------------------------------------------- storage

def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().replace("ı", "i")).strip("-")
    return s or "profile"


def folder(name):
    return PROFILES / name


def load(name):
    p = folder(name) / "profile.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save(profile):
    d = folder(profile["id"])
    d.mkdir(parents=True, exist_ok=True)
    profile["updated"] = datetime.date.today().isoformat()
    profile["summary"] = summarize(profile)
    (d / "profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def list_profiles():
    """[(id, display name, complete)] sorted by name."""
    out = []
    if PROFILES.exists():
        for d in sorted(PROFILES.iterdir()):
            p = load(d.name) if d.is_dir() else None
            if p:
                out.append((p["id"], display_name(p), complete(p)))
    return sorted(out, key=lambda r: r[1].lower())


def display_name(p):
    return f"{p['singer']} ({p['setup']})"


def new(singer, setup, devices=None, voice_from=None):
    base = slug(f"{singer} {setup}")
    name, n = base, 2
    while folder(name).exists():
        name, n = f"{base}-{n}", n + 1
    p = {"id": name, "singer": singer.strip(), "setup": setup.strip(),
         "created": datetime.date.today().isoformat(), "devices": devices or {},
         "calibration": {}, "takes": [], "voice_from": None}
    src = load(voice_from) if voice_from else None
    if src:  # same singer, new setup: carry the voice over, record the setup again
        p["voice_from"] = src["id"]
        p["calibration"] = {k: {**v, "carried_from": src["id"]} for k, v in src["calibration"].items()
                            if k in VOICE_STEPS}
        p["takes"] = [{**t, "setup_match": None, "carried_from": src["id"]} for t in src.get("takes", [])]
    save(p)
    return p


def complete(p):
    return all(s["id"] in p.get("calibration", {}) for s in STEPS)


def delete(name):
    from send2trash import send2trash

    if folder(name).exists():
        send2trash(str(folder(name)))


def active():
    """The active profile's id, or None."""
    from app import assistant

    name = assistant.load_settings().get("profile")
    return name if name and load(name) else None


def set_active(name):
    from app import assistant

    data = assistant.load_settings()
    if name:
        data["profile"] = name
    else:
        data.pop("profile", None)
    assistant.save_settings(data)


# ---------------------------------------------------------------- the guided recording

def tone(midi, seconds, sr=SR, level_db=-18.0):
    """A soft, organ-like reference note: a few harmonics with gentle fades."""
    t = np.arange(int(seconds * sr)) / sr
    f = music.midi_to_hz(midi)
    x = sum((0.6 ** k) * np.sin(2 * np.pi * f * (k + 1) * t) for k in range(4))
    fade = np.minimum(1, np.minimum(t, t[::-1]) / 0.03)
    x = x * fade / np.abs(x).max() * 10 ** (level_db / 20)
    return x.astype("float32")


def pitch_notes(range_result):
    """Seven notes across the singer's comfortable range (inside its edges), lowest first."""
    lo, hi = (range_result["low_midi"], range_result["high_midi"]) if range_result else (48, 64)
    lo, hi = lo + 2, hi - 2
    if hi - lo < 4:
        mid = (lo + hi) / 2
        lo, hi = mid - 3, mid + 3
    return [int(round(m)) for m in np.linspace(lo, hi, 7)]


NOTE_S, GAP_S = 1.2, 2.4


def backing(step_id, profile):
    """(stereo float32 array to play, schedule) for a step; silence except in the pitch step."""
    if step_id != "pitch":
        return np.zeros((int(STEP[step_id]["seconds"] * SR), 2), "float32"), None
    parts, schedule, t = [], [], 0.0
    for m in pitch_notes(profile["calibration"].get("range")):
        parts += [tone(m, NOTE_S), np.zeros(int(GAP_S * SR), "float32")]
        schedule.append({"midi": m, "start": PREROLL + t + NOTE_S, "end": PREROLL + t + NOTE_S + GAP_S})
        t += NOTE_S + GAP_S
    mono = np.concatenate(parts)
    return np.stack([mono, mono], axis=1), schedule


def clip_path(profile, step_id):
    return folder(profile["id"]) / "calibration" / f"{step_id}.wav"


def measure_step(profile, step_id, schedule=None, latency=0.0):
    """Measure a recorded step, store it in the profile, and return (result, message, ok).
    latency: the recording's delay behind playback (seconds, input + output), from the audio stream."""
    x, sr = sf.read(str(clip_path(profile, step_id)), always_2d=True)
    m = x[:, 0][int(PREROLL * sr):]
    fn = {"room": m_room, "level": m_level, "range": m_range, "pitch": m_pitch, "dynamics": m_dynamics,
          "echo": m_echo}[step_id]
    result = fn(m, sr, schedule, latency) if step_id == "pitch" else fn(m, sr)
    message, ok = result.pop("_message", ""), bool(result.pop("_ok", True))  # shown, not stored
    result["recorded"] = datetime.date.today().isoformat()
    profile["calibration"][step_id] = result
    save(profile)
    return result, message, ok


# ---------------------------------------------------------------- measurements

BANDS = np.geomspace(60, 16000, 21)


def db(x):
    return 20 * np.log10(np.maximum(x, 1e-12))


def band_spectrum(m, sr):
    """Level in 20 log-spaced bands (dB), for comparing tones and noise between recordings."""
    from scipy.signal import welch

    f, p = welch(m, sr, nperseg=4096)
    return [round(float(10 * np.log10(p[(f >= lo) & (f < hi)].mean() + 1e-20)), 1) for lo, hi in zip(BANDS[:-1], BANDS[1:])]


def block_db(m, sr, block=0.05):
    n = int(block * sr)
    k = len(m) // n
    return db(np.sqrt((m[:k * n].reshape(k, n) ** 2).mean(axis=1)))


def pitch_track(m, sr):
    import parselmouth

    t, f0 = music.track_pitch(parselmouth.Sound(m.astype("float64"), sampling_frequency=sr), floor=60, ceiling=1000)
    return t, music.hz_to_midi(f0)


def sibilant_pct(m, sr):
    """Share of loud frames whose top end dominates, measured as analyze_song does for originals."""
    import scipy.signal as ss
    import deess

    fq, _, spec = ss.stft(m, sr, nperseg=deess.NPERSEG, noverlap=deess.NPERSEG - deess.HOP)
    pw = np.abs(spec) ** 2
    full = 10 * np.log10(pw.sum(axis=0) + 1e-12)
    rel = 10 * np.log10(pw[fq >= 4500].sum(axis=0) + 1e-12) - full
    loud = full > full.max() - 50
    return float(100 * np.mean(rel[loud] > deess.AMOUNTS["light"][0] + 1.5))


def m_room(m, sr):
    m = m[int(0.3 * sr):]  # settle
    levels = block_db(m, sr)
    typical = float(np.median(levels))
    n = int(0.05 * sr)
    quiet = levels < typical + 6  # the room itself, without a cough or a bump
    calm = m[:len(levels) * n].reshape(len(levels), n)[quiet].ravel()
    floor = float(db(np.sqrt(np.mean(calm ** 2))))
    spec = band_spectrum(calm, sr)
    from scipy.signal import welch

    f, p = welch(m, sr, nperseg=sr)  # 1 Hz resolution for mains hum
    hum = None
    for base in (50, 60):
        near = (f > base - 8) & (f < base + 8) & (np.abs(f - base) > 2)
        peak = p[(f >= base - 1) & (f <= base + 1)].max()
        if 10 * np.log10(peak / (np.median(p[near]) + 1e-30)) > 15:
            hum = base
    ok = levels.max() < typical + 20  # a cough or a bump: record it again so the reading is the room alone
    msg = (f"Background noise {floor:.0f} dBFS" + (f", {hum} Hz mains hum" if hum else ", no hum")
           + ("" if floor < -50 else " (noisy: a quieter room or spot will give cleaner covers)")
           + ("" if ok else ". Something loud happened: record this again in silence."))
    return {"noise_db": round(floor, 1), "noise_bands": spec, "hum_hz": hum, "_message": msg, "_ok": ok}


def m_level(m, sr):
    peak = float(db(np.abs(m).max()))
    clip = float(100 * np.mean(np.abs(m) > 0.99))
    loud = float(np.percentile(block_db(m, sr), 95))
    if peak > -1:
        advice, ok = "Too loud: it reached full scale. Turn the mic down and record this again.", False
    elif loud < -30:
        advice, ok = "Very quiet: turn the mic up a little and record this again.", False
    else:
        advice, ok = "Level is good.", True
    return {"peak_db": round(peak, 1), "clip_pct": round(clip, 3), "loud_db": round(loud, 1),
            "_message": f"Loudest moment {peak:.1f} dBFS. {advice}", "_ok": ok}


def m_range(m, sr):
    _, mi = pitch_track(m, sr)
    v = mi[~np.isnan(mi)]
    if len(v) < 150:  # under ~1.5 s of singing
        return {"_message": "Couldn't hear enough singing: glide slowly on an \"ah\" and record this again.",
                "_ok": False}
    lo, hi = float(np.percentile(v, 3)), float(np.percentile(v, 97))
    msg = f"Comfortable range {music.midi_name(lo)} to {music.midi_name(hi)} ({hi - lo:.0f} semitones)."
    ok = hi - lo >= 5
    if not ok:
        msg += " That's narrow: glide all the way from low to high and record this again."
    return {"low_midi": round(lo, 1), "high_midi": round(hi, 1), "low": music.midi_name(lo),
            "high": music.midi_name(hi), "center_midi": round((lo + hi) / 2, 1), "_message": msg, "_ok": ok}


def note_habits(seg, target):
    """One sung note against its target: steady offset (cents), scoop at the start, vibrato."""
    v = ~np.isnan(seg)
    if v.sum() < 50:  # under half a second
        return None
    s = seg[v]
    s = s - 12 * np.round((np.median(s) - target) / 12)  # any octave
    steady = s[len(s) // 3:]  # past the attack
    offset = float(np.median(steady - target) * 100)
    start = float(np.median(s[:5] - target) * 100) - offset  # first 50 ms, relative to where it settles
    # settled: the pitch line, smoothed over a vibrato cycle, stays within 25 cents of where the note sits
    smooth = np.convolve(np.pad(s, 7, mode="edge"), np.ones(15) / 15, mode="valid")
    settle = next((i for i in range(len(s)) if np.all(np.abs(smooth[i:i + 10] - np.median(steady)) < 0.25)), len(s))
    vib_rate = vib_depth = None
    if len(steady) >= 80:
        d = (steady - np.polyval(np.polyfit(np.arange(len(steady)), steady, 2), np.arange(len(steady)))) * 100
        w = np.hanning(len(d))
        spec = np.abs(np.fft.rfft(d * w, 4096))  # zero-padded: finer rate steps than the note's length gives
        f = np.fft.rfftfreq(4096, 0.01)
        band = (f >= 3.5) & (f <= 8.5)
        if band.any() and spec[band].max() > 3 * np.median(spec[1:]):
            k = np.argmax(spec[band])
            vib_rate = float(f[band][k])
            vib_depth = float(2 * 2 * spec[band][k] / w.sum())  # the vibrato's own swing, peak to peak, cents
    return {"target": target, "offset_cents": round(offset, 1), "scoop_cents": round(start, 1),
            "settle_s": round(settle * 0.01, 2), "vibrato_hz": vib_rate and round(vib_rate, 1),
            "vibrato_cents": vib_depth and round(vib_depth, 1)}


def m_pitch(m, sr, schedule, latency=0.0):
    t, mi = pitch_track(m, sr)
    notes = []
    for item in schedule or []:
        # in the trimmed clip, from just after the reference note has stopped sounding in the recording
        # (it arrives `latency` late; its tail leaking from the headphones would read as a perfect note)
        a, b = item["start"] - PREROLL + latency + 0.06, item["end"] - PREROLL + latency
        h = note_habits(mi[(t >= a) & (t < b)], item["midi"])
        if h:
            notes.append(h)
    if len(notes) < 4:
        return {"notes": notes, "_message": f"Heard only {len(notes)} of {len(schedule or [])} notes: sing each "
                "note back in the pause after it, and record this again.", "_ok": False}
    off = [n["offset_cents"] for n in notes]
    msg = (f"{len(notes)} notes: on average {abs(np.median(off)):.0f} cents "
           f"{'sharp' if np.median(off) > 0 else 'flat'}, within {np.median(np.abs(off)):.0f} cents typically.")
    return {"notes": notes, "_message": msg, "_ok": True}


def m_dynamics(m, sr):
    _, mi = pitch_track(m, sr)
    voiced = ~np.isnan(mi)
    lv = block_db(m, sr, 0.01)[:len(voiced)]
    lv = lv[voiced[:len(lv)]]
    if len(lv) < 200:
        return {"_message": "Couldn't hear enough singing: sing the line twice, softly then loudly.", "_ok": False}
    half = len(lv) // 2
    soft, loud = float(np.median(lv[:half])), float(np.median(lv[half:]))
    sib = sibilant_pct(m, sr)
    spread = loud - soft
    ok = spread > 3
    msg = (f"Soft to loud: {spread:.0f} dB. \"S\" sounds stand out in {sib:.0f}% of loud moments."
           + ("" if ok else " Make the second time clearly louder and record this again."))
    return {"soft_db": round(soft, 1), "loud_db": round(loud, 1), "spread_db": round(spread, 1),
            "sibilant_pct": round(sib, 1), "voice_bands": band_spectrum(m, sr), "_message": msg, "_ok": ok}


def m_echo(m, sr):
    env = block_db(m, sr, 0.01)
    floor = float(np.percentile(env, 10))
    peaks, i = [], 0
    while i < len(env):
        if env[i] > floor + 25 and env[i] == env[max(0, i - 30):i + 30].max():
            peaks.append(i)
            i += 60
        else:
            i += 1
    rts = []
    for p in peaks:
        seg = env[p + 3:p + 45]  # 30-450 ms after the burst
        ok = seg > floor + 6
        if ok.sum() >= 6:
            slope = np.polyfit(np.flatnonzero(ok) * 0.01, seg[ok], 1)[0]
            if slope < -5:
                rts.append(float(np.clip(-60 / slope, 0.05, 3.0)))
    if not rts:
        return {"_message": "Couldn't hear the \"ha!\"s clearly: say them short and loud, and record this again.",
                "_ok": False}
    rt = float(np.median(rts))
    word = "very dry" if rt < 0.25 else "dry" if rt < 0.45 else "a bit echoey" if rt < 0.8 else "echoey"
    return {"decay_s": round(rt, 2), "bursts": len(rts),
            "_message": f"Room sounds {word} (echo dies away in about {rt:.1f} s).", "_ok": True}


# ---------------------------------------------------------------- learning from takes

def take_noise(m, sr, sung):
    import match

    floor = match.noise_floor(m, sr, sung)
    if floor is None:
        return None, None
    return float(db(np.sqrt(np.mean(floor ** 2)))), band_spectrum(floor, sr)


def setup_matches(profile, noise_db, noise_bands):
    """Does a take's background sound like this profile's room tone? None when it can't tell.
    In-song recordings also carry some headphone bleed, so the limits are generous."""
    room = profile["calibration"].get("room")
    if not room or noise_db is None:
        return None
    a, b = np.array(room["noise_bands"]), np.array(noise_bands)
    shape = float(np.corrcoef(a - a.mean(), b - b.mean())[0, 1])
    return abs(noise_db - room["noise_db"]) <= 10 and shape >= 0.6


def learn(song_folder, take, profile_id, uploaded=False, log=print):
    """Add one processed take's measurements to its profile. Never raises (covers don't depend on it)."""
    try:
        p = load(profile_id)
        if not p:
            return
        song_folder = Path(song_folder)
        import match

        x, sr = sf.read(str(song_folder / f"{take}_aligned.wav"), always_2d=True)
        m = x[:, 0]
        ref, rsr = sf.read(str(song_folder / "song_vocals.wav"), always_2d=True)
        sung = match.sung_mask(m, sr)
        t, mi = pitch_track(m, sr)
        rt, rmi = pitch_track(ref.mean(axis=1), rsr)
        n = min(len(mi), len(rmi))
        mi, rmi = mi[:n], rmi[:n]
        both = ~np.isnan(mi) & ~np.isnan(rmi)
        dev = (mi[both] - rmi[both])
        dev = dev - 12 * np.round(dev / 12)  # sung in another octave counts the same
        near = np.abs(dev) <= 1.0  # the same note was meant
        sung_midi = mi[both][near]
        pitch = {}
        if near.sum() > 300:
            pitch = {"offset_cents": round(float(np.median(dev[near]) * 100), 1), "frames": int(near.sum()),
                     "registers": registers(sung_midi, dev[near] * 100, p)}
        noise_db, noise_bands = (None, None) if uploaded else take_noise(m, sr, sung)
        match_setup = None if uploaded else setup_matches(p, noise_db, noise_bands)
        notes = json.loads((song_folder / "vocalworks.json").read_text(encoding="utf-8"))
        tk = notes.get("takes", {}).get(take, {})
        v = mi[~np.isnan(mi)]
        entry = {"song": song_folder.name, "take": take, "date": datetime.date.today().isoformat(),
                 "uploaded": uploaded, "setup_match": match_setup, "approved": False,
                 "pitch": pitch, "range_low_midi": round(float(np.percentile(v, 5)), 1) if len(v) else None,
                 "range_high_midi": round(float(np.percentile(v, 95)), 1) if len(v) else None,
                 "sibilant_pct": round(sibilant_pct(m[sung] if sung.any() else m, sr), 1),
                 "spread_db": round(float(match.metrics(m, sr, sung)["spread"]), 1),
                 "noise_db": noise_db and round(noise_db, 1),
                 "vocal_balance_lu": tk.get("mix", {}).get("vocal_vs_instrumental_lu")}
        if match_setup is False:  # a different mic or room: its voice data still counts, its setup data doesn't
            entry["sibilant_pct"] = entry["spread_db"] = None
        p["takes"] = [e for e in p["takes"] if not (e.get("song") == entry["song"] and e.get("take") == take
                                                   and not e.get("carried_from"))] + [entry]
        save(p)
        log(f"Profile {display_name(p)}: learned from {take}"
            + (" (sounds like a different setup: only the voice part counted. If you changed mic or room, "
               "create a new profile for it.)" if match_setup is False else ""))
    except Exception as e:  # noqa: BLE001
        log(f"Profile learning skipped ({e!r})")


def registers(midi, cents, p):
    """Median pitch offset in the low, middle and high third of the singer's range."""
    rng = p["calibration"].get("range")
    lo, hi = (rng["low_midi"], rng["high_midi"]) if rng else (np.percentile(midi, 5), np.percentile(midi, 95))
    edges = [lo + (hi - lo) / 3, lo + 2 * (hi - lo) / 3]
    out = {}
    for name, sel in (("low", midi < edges[0]), ("mid", (midi >= edges[0]) & (midi < edges[1])),
                      ("high", midi >= edges[1])):
        if sel.sum() > 100:
            out[name] = round(float(np.median(cents[sel])), 1)
    return out


def mark_approved(song_folder, take, profile_id):
    p = load(profile_id) if profile_id else None
    if not p:
        return
    for e in p["takes"]:
        if e.get("song") == Path(song_folder).name and e.get("take") == take and not e.get("carried_from"):
            e["approved"] = True
    save(p)


# ---------------------------------------------------------------- the summary

def weighted_median(values, weights):
    pairs = sorted((v, w) for v, w in zip(values, weights) if v is not None)
    if not pairs:
        return None
    total, acc = sum(w for _, w in pairs), 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def take_weights(takes):
    """Newer takes count more, approved ones twice; carried-over ones half."""
    n = len(takes)
    return [(0.9 ** (n - 1 - i)) * (2 if t.get("approved") else 1) * (0.5 if t.get("carried_from") else 1)
            for i, t in enumerate(takes)]


def summarize(p):
    cal, takes = p.get("calibration", {}), p.get("takes", [])
    w = take_weights(takes)
    s = {"takes_learned": len([t for t in takes if not t.get("carried_from")]),
         "approved": len([t for t in takes if t.get("approved") and not t.get("carried_from")])}
    if "range" in cal:
        s["range"] = [cal["range"]["low"], cal["range"]["high"]]
        s["center"] = music.midi_name(cal["range"]["center_midi"])
    songs_lo = weighted_median([t.get("range_low_midi") for t in takes], w)
    songs_hi = weighted_median([t.get("range_high_midi") for t in takes], w)
    if songs_lo is not None:
        s["range_in_songs"] = [music.midi_name(songs_lo), music.midi_name(songs_hi)]
    # pitch tendency per register: the guided notes count like two takes each
    reg_vals = {k: ([], []) for k in ("low", "mid", "high")}
    notes = cal.get("pitch", {}).get("notes", [])
    if notes and "range" in cal:
        lo, hi = cal["range"]["low_midi"], cal["range"]["high_midi"]
        for nt in notes:
            k = "low" if nt["target"] < lo + (hi - lo) / 3 else "mid" if nt["target"] < lo + 2 * (hi - lo) / 3 else "high"
            reg_vals[k][0].append(nt["offset_cents"])
            reg_vals[k][1].append(2.0)
    for t, wt in zip(takes, w):
        for k, v in t.get("pitch", {}).get("registers", {}).items():
            reg_vals[k][0].append(v)
            reg_vals[k][1].append(wt)
    tend = {k: weighted_median(*vw) for k, vw in reg_vals.items() if vw[0]}
    if tend:
        s["pitch_by_register_cents"] = {k: round(v, 1) for k, v in tend.items()}
    if notes:
        for key, field in (("scoop_cents", "scoop_cents"), ("settle_s", "settle_s")):
            s[key] = round(float(np.median([n[field] for n in notes])), 2)
        vib = [n for n in notes if n.get("vibrato_hz")]
        if len(vib) >= 2:
            s["vibrato"] = {"hz": round(float(np.median([n["vibrato_hz"] for n in vib])), 1),
                            "cents": round(float(np.median([n["vibrato_cents"] for n in vib])), 1)}
    # setup-dependent: only takes that matched this setup, plus the guided recording
    same = [(t, wt) for t, wt in zip(takes, w) if t.get("setup_match") is not False and not t.get("carried_from")]
    sib_v = [t.get("sibilant_pct") for t, _ in same] + ([cal["dynamics"]["sibilant_pct"]] if "dynamics" in cal else [])
    sib_w = [wt for _, wt in same] + ([2.0] if "dynamics" in cal else [])
    if any(v is not None for v in sib_v):
        s["sibilant_pct"] = round(weighted_median(sib_v, sib_w), 1)
    spread = weighted_median([t.get("spread_db") for t, _ in same], [wt for _, wt in same])
    if spread is not None:
        s["evenness_in_songs_db"] = round(spread, 1)
    if "dynamics" in cal:
        s["soft_to_loud_db"] = cal["dynamics"]["spread_db"]
    if "room" in cal:
        s["noise_db"] = cal["room"]["noise_db"]
        s["hum_hz"] = cal["room"]["hum_hz"]
    if "echo" in cal:
        s["room_decay_s"] = cal["echo"]["decay_s"]
    if "level" in cal:
        s["mic_peak_db"] = cal["level"]["peak_db"]
    bal = [t.get("vocal_balance_lu") for t in takes if t.get("approved") and t.get("vocal_balance_lu") is not None]
    if bal:
        s["approved_vocal_balance_lu"] = round(float(np.median(bal)), 1)
    s["setup_mismatches"] = len([t for t in takes if t.get("setup_match") is False and not t.get("carried_from")])
    return s


def describe(p):
    """The "Your voice" card, in plain lines."""
    s = p.get("summary") or summarize(p)
    if not p.get("calibration"):
        return "Not recorded yet: click \"Record the profile\" to start."
    lines = [f"{display_name(p)}" + ("" if complete(p) else "   (guided recording not finished)")]
    if "range" in s:
        lines.append(f"Range: {s['range'][0]} to {s['range'][1]}, centred on {s['center']}"
                     + (f" (in songs you sing {s['range_in_songs'][0]} to {s['range_in_songs'][1]})"
                        if "range_in_songs" in s else ""))
    if "pitch_by_register_cents" in s:
        word = lambda c: "on pitch" if abs(c) < 8 else f"{abs(c):.0f} cents {'sharp' if c > 0 else 'flat'}"  # noqa: E731
        lines.append("Pitch: " + ", ".join(f"{k} notes {word(v)}" for k, v in s["pitch_by_register_cents"].items()))
    if "scoop_cents" in s:
        lines.append(f"Note starts: you begin {abs(s['scoop_cents']):.0f} cents "
                     f"{'below' if s['scoop_cents'] < 0 else 'above'} and settle in {s['settle_s']:.2f} s")
    if "vibrato" in s:
        lines.append(f"Vibrato: {s['vibrato']['hz']:.1f} per second, {s['vibrato']['cents']:.0f} cents wide")
    if "sibilant_pct" in s:
        lines.append(f"\"S\" sounds: stand out in {s['sibilant_pct']:.0f}% of loud moments"
                     + (f"; soft to loud {s['soft_to_loud_db']:.0f} dB" if "soft_to_loud_db" in s else ""))
    if "noise_db" in s:
        lines.append(f"Setup: background noise {s['noise_db']:.0f} dBFS"
                     + (f", {s['hum_hz']} Hz hum" if s.get("hum_hz") else "")
                     + (f", room echo {s['room_decay_s']:.1f} s" if "room_decay_s" in s else "")
                     + (f", loudest singing peaks at {s['mic_peak_db']:.0f} dBFS" if "mic_peak_db" in s else ""))
    lines.append(f"Learned from {s['takes_learned']} take(s), {s['approved']} approved"
                 + (f"; {s['setup_mismatches']} sounded like a different setup" if s.get("setup_mismatches") else ""))
    if p.get("voice_from"):
        lines.append(f"Voice carried over from {p['voice_from']}")
    return "\n".join(lines)
