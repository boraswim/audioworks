"""Study a song before recording: key, tempo, vocal sections and range, and how the
original vocal was produced, ending with recommended processing settings.

Needs song.mp3 plus song_vocals.wav / song_instrumental.wav (run separate.py first).
Saves the results into the song's vocalworks.json (see songfile.py), keeping any notes there.

  python tools/analyze_song.py songs/<name>
"""
import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import welch

sys.path.insert(0, str(Path(__file__).parent))
import match  # noqa: E402
import music  # noqa: E402
import songfile  # noqa: E402

warnings.filterwarnings("ignore")
fmt = lambda t: f"{int(t) // 60}:{int(t) % 60:02d}"  # noqa: E731


def id3_tags(path):
    raw = open(path, "rb").read(8192)
    tags = {}
    if raw[:3] != b"ID3":
        return tags
    for tag, name in ((b"TIT2", "title"), (b"TPE1", "artist"), (b"TALB", "album")):
        k = raw.find(tag)
        if k >= 0:
            size = int.from_bytes(raw[k + 4:k + 8], "big")
            body = raw[k + 10:k + 10 + size]
            enc = {1: "utf-16", 2: "utf-16", 3: "utf-8"}.get(body[0], "latin-1")
            tags[name] = body[1:].decode(enc, errors="replace").strip("\x00")
    return tags


def key_report(path):
    import librosa

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    chroma = librosa.feature.chroma_cqt(y=librosa.effects.harmonic(y), sr=sr)

    def rank(c):
        return sorted(((np.corrcoef(c, np.roll(prof, t))[0, 1], t, mode)
                       for mode, prof in (("major", music.MAJOR_PROFILE), ("minor", music.MINOR_PROFILE))
                       for t in range(12)), reverse=True)

    overall = rank(chroma.mean(axis=1))
    per = int(30 * sr / 512)
    sections = [music.key_name(*rank(chroma[:, i * per:(i + 1) * per].mean(axis=1))[0][1:])
                for i in range(chroma.shape[1] // per)]
    tempos = librosa.beat.tempo(y=y, sr=sr, aggregate=None)
    return overall, sections, np.percentile(tempos, [10, 50, 90])


def vocal_sections(m, sr):
    blk = int(0.5 * sr)
    r = np.array([np.sqrt(np.mean(m[k:k + blk] ** 2)) for k in range(0, len(m) - blk, blk)])
    act = match.db(r) > match.db(r.max()) - 30
    secs, s = [], None
    for k, on in enumerate(np.r_[act, False]):
        if on and s is None:
            s = k
        if not on and s is not None:
            if k - s >= 4:
                secs.append([s * 0.5, k * 0.5])
            s = None
    merged = []
    for a, b in secs:
        if merged and a - merged[-1][1] < 3:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return merged


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", type=Path)
    a = p.parse_args()
    d = a.folder
    song, voc, inst = d / "song.mp3", d / "song_vocals.wav", d / "song_instrumental.wav"
    if not voc.exists() or not inst.exists():
        sys.exit(f"Missing stems; run: python tools/separate.py {song}")

    info = sf.info(str(song))
    tags = id3_tags(song)
    x, sr = sf.read(str(song), always_2d=True)
    print("== FILE")
    print(f"{tags.get('artist', '?')} - {tags.get('title', '?')} ({tags.get('album', '?')})")
    print(f"{info.format} {info.samplerate} Hz {info.channels} ch, {fmt(info.duration)} ({info.duration:.1f} s)")
    mono_song = x.shape[1] == 1 or np.corrcoef(x[:, 0], x[:, 1])[0, 1] > 0.99
    import pyloudnorm
    meter = pyloudnorm.Meter(sr)
    song_lufs = meter.integrated_loudness(x if x.shape[1] > 1 else np.repeat(x, 2, 1))
    print(f"loudness {song_lufs:.1f} LUFS, peak {match.db(np.abs(x).max()):+.2f} dBFS, "
          f"{'MONO recording' if mono_song else 'stereo'}")

    overall, sections, tempo = key_report(song)
    (s1, t1, m1), (s2, t2, m2) = overall[0], overall[1]
    print("\n== KEY & TEMPO")
    print(f"key {music.key_name(t1, m1)} (margin {s1 - s2:.3f} over {music.key_name(t2, m2)}; < 0.02 = unsure)")
    stable = len(set(sections)) == 1
    print(f"per 30 s: {', '.join(sections)}" + ("  -> stable" if stable else "  -> moves around"))
    print(f"tempo {tempo[1]:.0f} BPM (10-90%: {tempo[0]:.0f}-{tempo[2]:.0f}"
          + (", not on a click)" if tempo[2] - tempo[0] > 8 else ", steady)"))

    vs, vsr = sf.read(str(voc), always_2d=True)
    ins, isr = sf.read(str(inst), always_2d=True)
    if vsr != sr or isr != sr:  # stems from an older separate.py may be at 44.1 kHz
        import soxr
        vs, ins = soxr.resample(vs, vsr, sr), soxr.resample(ins, isr, sr)
    vm = vs.mean(axis=1)
    sung = match.sung_mask(vm, sr)
    times, f0 = music.track_pitch(music.load_sound(voc))
    voiced = ~np.isnan(f0)
    mi = music.hz_to_midi(f0[voiced])
    pcs = music.scale_pcs(t1, m1)
    in_key = np.mean([int(round(v)) % 12 in pcs for v in mi])
    usage = np.bincount(np.round(mi).astype(int) % 12, minlength=12) / len(mi)
    jumps = np.abs(np.diff(music.hz_to_midi(f0)))
    jumps = jumps[~np.isnan(jumps)]
    lvl = np.array([match.db(np.sqrt(np.mean(vm[int(t * sr):int((t + STEPW) * sr)] ** 2))) for t in times])
    low = voiced & (music.hz_to_midi(np.where(voiced, f0, 1)) < 48)
    print("\n== VOCAL PART")
    print("sections: " + ", ".join(f"{fmt(s)}-{fmt(e)}" for s, e in vocal_sections(vm, sr)))
    print(f"sung {100 * voiced.mean():.0f}% of the song; range (5-95%) {music.midi_name(np.percentile(mi, 5))}-"
          f"{music.midi_name(np.percentile(mi, 95))}, median {music.midi_name(np.median(mi))}")
    print(f"notes in {music.key_name(t1, m1)}: {100 * in_key:.0f}%; most used: "
          + " ".join(f"{music.NOTE_NAMES[i]}:{100 * usage[i]:.0f}" for i in np.argsort(-usage)[:6]))
    if low.any():
        print(f"below C3: {100 * low.sum() / voiced.sum():.0f}% of sung frames, "
              f"{np.median(lvl[low]) - np.median(lvl[voiced]):+.1f} dB vs typical (near 0 = real singing, much lower = bleed)")
    print(f"guide quality: pitch jumps >1 st on {100 * np.mean(jumps > 1):.1f}% of frames (under ~6% is fine)")

    print("\n== HOW THE ORIGINAL VOCAL IS PRODUCED")
    dev = np.abs(mi - np.round(mi)) * 100
    wob = np.abs(np.diff(music.hz_to_midi(f0))) * 100
    wob = wob[~np.isnan(wob) & (wob < 50)]
    # natural singers land ~25-35% of frames within 10 cents; tuned vocals ~70%
    tuned = np.mean(dev < 10) > 0.55
    hard = tuned and np.median(wob) < 2.0
    style = "HARD AUTOTUNE (no movement)" if hard else ("TUNED with some movement" if tuned else "natural / not autotuned")
    print(f"pitch: {np.median(dev):.0f} cents off-grid, {100 * np.mean(dev < 10):.0f}% within 10, wobble {np.median(wob):.1f}"
          f" -> {style}")
    # (harmonics-to-noise is left out: separated stems all read ~12-16 dB, so it can't tell
    #  distortion or a vocoder from bleed and reverb)
    f, ps = welch(vm[sung], sr, nperseg=4096)
    mid = ps[(f >= 600) & (f < 1200)].mean()
    bands = [(80, 150), (150, 300), (300, 600), (1200, 2500), (2500, 5000), (5000, 8000), (8000, 12000)]
    rel = {b: 10 * np.log10(ps[(f >= b[0]) & (f < b[1])].mean() / mid) for b in bands}
    print("tone vs 600-1200 Hz: " + "  ".join(f"{lo}-{hi} {v:+.0f}" for (lo, hi), v in rel.items()))
    mt = match.metrics(vm, sr, sung)
    print(f"evenness (loudness spread) {mt['spread']:.1f} dB, room tail {mt['tail']:.1f} dB, "
          f"peak-to-average {mt['crest']:.1f} dB")
    floor = match.noise_floor(vm, sr, sung)
    floor_db = match.db(np.sqrt(np.mean(floor ** 2))) if floor is not None else float("nan")
    st = lambda v: v if v.shape[1] > 1 else np.repeat(v, 2, 1)  # noqa: E731
    balance = meter.integrated_loudness(st(vs)) - meter.integrated_loudness(st(ins))
    if vs.shape[1] > 1:
        side_mid = 10 * np.log10(np.sum(((vs[:, 0] - vs[:, 1]) / 2)[sung] ** 2) / np.sum(((vs[:, 0] + vs[:, 1]) / 2)[sung] ** 2) + 1e-12)
    else:
        side_mid = -120.0
    rev = match.reverb_profile(vs, sr)
    # a vocal can read wide only because its reverb is stereo: then the side channel is far stronger in the
    # tails than while singing, and the cover gets a stereo room instead of double-tracking
    reverb_width = rev["stereo"] and rev["tail_side_mid_db"] - side_mid > 8
    wide = side_mid > -20 and not reverb_width
    print("reverb: " + (f"{rev['rt60']:.1f} s decay ({rev['decay_db_per_s']:+.0f} dB/s)"
                        + (", stereo" if rev["stereo"] else "") if rev["rt60"] else
                        f"none (the gaps hold steady bleed/noise, {rev['decay_db_per_s']:+.0f} dB/s)"))
    print(f"stereo width (side/mid while singing) {side_mid:.1f} dB -> "
          + ("WIDE vocal" if wide else "wide only through its stereo reverb" if reverb_width and side_mid > -20
             else "mono / centered")
          + f"; vocal-stem noise floor {floor_db:.1f} dBFS; vocal vs instrumental {balance:+.1f} LU")
    import deess
    import scipy.signal as ss
    fq, _, spec = ss.stft(vm, sr, nperseg=deess.NPERSEG, noverlap=deess.NPERSEG - deess.HOP)
    pw = np.abs(spec) ** 2
    full = 10 * np.log10(pw.sum(axis=0) + 1e-12)
    rel_s = 10 * np.log10(pw[fq >= 4500].sum(axis=0) + 1e-12) - full
    loud = full > full.max() - 50
    sib = 100 * np.mean(rel_s[loud] > deess.AMOUNTS["light"][0] + 1.5)
    print(f"consonant brightness: ~{sib:.0f}% of frames sibilant (de-ess the take to about this; most songs ~3-7%)")
    import distort
    kurt, flat = distort.grit(vm, sr, sung)
    # a clearly driven vocal: flattened waveform AND grit between harmonics (tape hiss also raises
    # flatness, so lo-fi originals with a hiss floor don't count on flatness alone)
    distorted = kurt < -0.6 and flat > 0.045
    print(f"saturation: kurtosis {kurt:+.2f}, grit (flatness 1-5 kHz) {flat:.3f} -> "
          f"{'DISTORTED vocal (amp/drive)' if distorted else 'clean'}")

    print("\n== RECOMMENDED SETTINGS")
    # snapping to a key only when the song stays in it; otherwise nearest semitone
    key_arg = f'"{music.key_name(t1, m1)}"' if (stable and in_key > 0.85) else "chromatic"
    if tuned:
        print(f"autotune.py --melody song_vocals.wav --key {key_arg} --strength 1 --retune-ms {10 if hard else 25} --max-shift 1.5")
    else:
        print(f"autotune.py --melody song_vocals.wav --key {key_arg} --strength 0.8 --retune-ms 90 --max-shift 1.5"
              "   (gentle; check pitch character vs the original after)")
    flags = []  # (flag, why)
    if rel[(5000, 8000)] < -28:
        flags.append(("--max-cut 40", "very dark top end; the default 12 dB cut can't reach it"))
    if floor_db > -85 and (rel[(8000, 12000)] < -40 or song_lufs < -11):
        flags.append(("--hiss", "old/lo-fi recording: match its noise floor"))
    print("match.py --ref song_vocals.wav " + " ".join(fl for fl, _ in flags))
    for fl, why in flags:
        print(f"   {fl}: {why}")
    if distorted:
        print("distort.py <deessed> --drive N, with N chosen so the matched vocal's kurtosis/grit approach the "
              "original's (match.py then needs --sung-from <tuned>)")
    if wide:
        print("double.py <processed> --tightness tight --with-lead --match-width song_vocals.wav   (after match.py)")
    if tuned:
        print("vocoder: song_features.py decides (what is written about the song + a carrier check on the stem)")
    print(f"mixdown.py: target vocal balance {balance + 2:+.1f} LU (original {balance:+.1f} + 2, user preference)"
          + (", --mono" if mono_song else "") + f", loudness {song_lufs:.1f} LUFS")
    low_med = np.median(mi)
    if low_med < 52:
        print(f"note: melody centers on {music.midi_name(low_med)}, low for the user (~A#3-C4); singing an octave up is fine")

    r1 = lambda v: round(float(v), 1)  # noqa: E731
    existing = songfile.load(d)
    songfile.merge(
        d,
        song={**tags, "duration_s": r1(info.duration), "mono": bool(mono_song)},
        analysis={
            "loudness_lufs": r1(song_lufs), "peak_dbfs": r1(match.db(np.abs(x).max())),
            "key": music.key_name(t1, m1), "key_stable": stable, "key_sections": sections,
            "tempo_bpm": r1(tempo[1]), "tempo_range": [r1(tempo[0]), r1(tempo[2])],
            "vocal_sections": [f"{fmt(s)}-{fmt(e)}" for s, e in vocal_sections(vm, sr)],
            "range": [music.midi_name(np.percentile(mi, 5)), music.midi_name(np.percentile(mi, 95))],
            "range_median": music.midi_name(low_med), "notes_in_key_pct": r1(100 * in_key),
            "pitch_style": style, "off_grid_cents": r1(np.median(dev)),
            "within_10_cents_pct": r1(100 * np.mean(dev < 10)), "wobble_cents": r1(np.median(wob)),
            "tone_vs_1k_db": {f"{lo}-{hi}": r1(v) for (lo, hi), v in rel.items()},
            "evenness_db": r1(mt["spread"]), "room_tail_db": r1(mt["tail"]),
            "reverb_s": rev["rt60"] and r1(rev["rt60"]), "reverb_stereo": rev["stereo"], "peak_to_average_db": r1(mt["crest"]),
            "stereo_side_mid_db": r1(side_mid), "wide": bool(wide), "noise_floor_dbfs": r1(floor_db),
            "vocal_vs_instrumental_lu": r1(balance), "sibilant_pct": r1(sib),
            "kurtosis": round(kurt, 2), "grit_flatness": round(flat, 3), "distorted": bool(distorted),
        },
        recommended={
            "autotune": {"key": key_arg.strip('"'), "strength": 1.0 if tuned else 0.8,
                         "retune_ms": (10 if hard else 25) if tuned else 90, "max_shift": 1.5},
            "deess_target_pct": r1(sib),
            "match_flags": [fl for fl, _ in flags],
            "widen": bool(wide), "vocoder_optional": bool(tuned), "distortion": bool(distorted),
            "mixdown": {"vocal_balance_lu": r1(balance + 2), "mono": bool(mono_song), "loudness_lufs": r1(song_lufs)},
        },
        status=existing.get("status", "analyzed"),
    )
    print(f"\nSaved to {songfile.path(d)}")


STEPW = 0.01

if __name__ == "__main__":
    main()
