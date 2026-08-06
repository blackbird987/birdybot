"""Local meeting capture + transcription (Teams, Zoom, any audio).

Records BOTH sides of a call locally — the system audio coming out of your
speakers (everyone else) and your microphone (you) — mixes them, and
transcribes with a local Whisper model. Nothing leaves the machine.

This is Teams-independent: it captures whatever is playing, so it works
regardless of tenant policy, IT restrictions, or which app the call is in.

Requires: soundcard + faster-whisper + numpy
    pip install soundcard faster-whisper numpy

CLI usage:
    python meeting.py devices
    python meeting.py record <name> [--minutes N]       # record to WAV, then stop
    python meeting.py transcribe <wav> [--model M] [--lang nl]
    python meeting.py live <name> [--model M] [--chunk 20]   # rolling transcript
    python meeting.py stop <name>                       # stop a live/record session
    python meeting.py status <name>                     # show rolling transcript

Files live under data/meeting/<name>/:
    audio.wav        full recording (record mode)
    transcript.txt   growing transcript with timestamps (live mode)
    .stop            sentinel file; its presence stops the loop
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import wave

log = logging.getLogger(__name__)

SAMPLERATE = 16000  # Whisper wants 16 kHz mono
BLOCK = 1600        # 0.1 s per recorder read

# --- Graceful imports ---

try:
    import numpy as np

    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

try:
    import soundcard as sc

    SOUNDCARD_AVAILABLE = True
except Exception:  # soundcard can raise non-ImportError on missing audio backend
    SOUNDCARD_AVAILABLE = False


def _register_cuda_dlls():
    """Make pip-installed NVIDIA CUDA libs (cuBLAS/cuDNN) loadable on Windows.

    faster-whisper's CUDA backend (ctranslate2) needs cublas64_12.dll /
    cudnn*.dll at runtime. When those come from the nvidia-*-cu12 wheels they
    land in site-packages/nvidia/*/bin, which is not on the DLL search path.

    ctranslate2 loads them with a bare LoadLibrary("cublas64_12.dll"), which
    ignores os.add_dll_directory but DOES search PATH — so we prepend the bin
    dirs to PATH (and register them for Python's own loader too, belt-and-braces).
    """
    import glob
    import site

    roots = []
    try:
        roots.extend(site.getsitepackages())
    except Exception:  # noqa: BLE001 - some embedded interpreters lack this
        pass
    if hasattr(site, "getusersitepackages"):
        roots.append(site.getusersitepackages())

    bindirs = []
    for sp in roots:
        bindirs.extend(glob.glob(os.path.join(sp, "nvidia", "*", "bin")))

    for bindir in bindirs:
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(bindir)
            except OSError:
                pass
    if bindirs:
        existing = os.environ.get("PATH", "")
        parts = existing.split(os.pathsep)
        new = [b for b in bindirs if b not in parts]
        if new:
            os.environ["PATH"] = os.pathsep.join(new + parts)


_register_cuda_dlls()


# --- Paths ---

def _base_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", ".."))
    return os.path.join(root, "data", "meeting")


def _session_dir(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "session"
    d = os.path.join(_base_dir(), safe)
    os.makedirs(d, exist_ok=True)
    return d


def _stop_path(name: str) -> str:
    return os.path.join(_session_dir(name), ".stop")


def _clear_stop(path: str):
    """Remove the stop sentinel if present (idempotent)."""
    if os.path.exists(path):
        os.remove(path)


# --- Audio capture ---

class _StreamRecorder(threading.Thread):
    """Continuously records one device into a queue of mono float32 blocks."""

    def __init__(self, device, label: str):
        super().__init__(daemon=True)
        self.device = device
        self.label = label
        self.q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.error: str | None = None

    def run(self):
        # soundcard's MediaFoundation backend needs COM initialised per thread.
        co_init = False
        try:
            import pythoncom

            pythoncom.CoInitialize()
            co_init = True
        except Exception:  # noqa: BLE001 - non-Windows / pywin32 missing
            pass
        try:
            with self.device.recorder(samplerate=SAMPLERATE, blocksize=BLOCK) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=BLOCK)  # (frames, channels)
                    if data.ndim > 1 and data.shape[1] > 1:
                        data = data.mean(axis=1)
                    else:
                        data = data.reshape(-1)
                    self.q.put(data.astype("float32"))
        except Exception as e:  # noqa: BLE001 - surface to caller thread
            self.error = str(e)
            log.exception("Recorder %s failed", self.label)
        finally:
            if co_init:
                try:
                    import pythoncom

                    pythoncom.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self):
        self._stop.set()

    def drain(self, min_frames: int, timeout: float = 5.0):
        """Pull queued blocks until at least *min_frames* collected (or timeout)."""
        collected = []
        have = 0
        deadline = time.time() + timeout
        while have < min_frames and time.time() < deadline:
            try:
                block = self.q.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            collected.append(block)
            have += len(block)
        if not collected:
            return np.zeros(0, dtype="float32")
        return np.concatenate(collected)


def _open_devices():
    """Return (mic_device, loopback_device). Loopback = system audio out."""
    if not SOUNDCARD_AVAILABLE:
        raise RuntimeError("soundcard not installed — run: pip install soundcard")
    mic = sc.default_microphone()
    loopback = None
    default_spk = sc.default_speaker()
    for dev in sc.all_microphones(include_loopback=True):
        if getattr(dev, "isloopback", False) and default_spk.name in dev.name:
            loopback = dev
            break
    if loopback is None:
        # fall back to any loopback device
        for dev in sc.all_microphones(include_loopback=True):
            if getattr(dev, "isloopback", False):
                loopback = dev
                break
    if loopback is None:
        raise RuntimeError("No loopback (system-audio) device found")
    return mic, loopback


def _mix(mic_buf, spk_buf):
    """Sum mic + system audio to a mono float32 stream, length = min of the two."""
    n = min(len(mic_buf), len(spk_buf))
    if n == 0:
        return np.zeros(0, dtype="float32")
    mixed = mic_buf[:n] + spk_buf[:n]
    peak = float(np.max(np.abs(mixed))) if n else 0.0
    if peak > 1.0:
        mixed = mixed / peak  # avoid clipping when both sides are loud
    return mixed.astype("float32")


# --- Whisper ---

_model = None
_model_name = None


def _load_model(model: str):
    global _model, _model_name
    if _model is not None and _model_name == model:
        return _model
    from faster_whisper import WhisperModel

    last_err = None
    for device, compute in (("cuda", "float16"), ("cpu", "int8")):
        try:
            m = WhisperModel(model, device=device, compute_type=compute)
            # Constructing the model does NOT touch cuBLAS/cuDNN — those only
            # load at the first encode(). Run a tiny inference so a machine whose
            # GPU can't actually run (missing CUDA libs) falls back to CPU here
            # instead of failing every real transcription.
            if device == "cuda":
                list(m.transcribe(np.zeros(SAMPLERATE, dtype="float32"), language="en"))
            _model = m
            _model_name = model
            log.info("Whisper %s loaded on %s/%s", model, device, compute)
            return _model
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("Whisper load/validate failed on %s: %s", device, str(e)[:160])
    raise RuntimeError(f"Could not load Whisper model on GPU or CPU: {last_err}")


def _is_noise_segment(s) -> bool:
    """True for likely Whisper hallucinations on silence/near-silence.

    On quiet stretches Whisper invents filler ("thank you", "vielen dank",
    "bedankt", "goedendag"). Measured on this machine, real speech scores
    no_speech_prob ~0.1 / avg_logprob ~-0.25, while silence hallucinations sit
    at no_speech_prob >0.6 OR avg_logprob <-1.0 — so either signal alone is
    enough to drop. The -1.0 floor keeps moderately quiet real speech
    (avg_logprob ~-0.5 to -0.7). Whisper's own gate requires BOTH at once,
    which is why these slip through by default.
    """
    if s.no_speech_prob > 0.6:  # Whisper itself thinks this is not speech
        return True
    if s.avg_logprob < -1.0:  # very low confidence — usually noise/hallucination
        return True
    if s.compression_ratio > 2.4:  # runaway repetition
        return True
    return False


def _transcribe_array(arr, model: str, lang: str | None):
    m = _load_model(model)
    segments, info = m.transcribe(
        arr,
        language=lang,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    kept = [s for s in segments if not _is_noise_segment(s)]
    return kept, info


def _fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# --- Commands ---

def cmd_devices():
    mic, loopback = _open_devices()
    print("Microphone (you):   ", mic.name)
    print("System audio (them):", loopback.name)


def cmd_record(name: str, minutes: float | None):
    mic, loopback = _open_devices()
    d = _session_dir(name)
    wav_path = os.path.join(d, "audio.wav")
    stop = _stop_path(name)
    _clear_stop(stop)

    mic_rec = _StreamRecorder(mic, "mic")
    spk_rec = _StreamRecorder(loopback, "system")
    mic_rec.start()
    spk_rec.start()
    print(f"Recording '{name}' -> {wav_path}")
    print(f"Stop with: python meeting.py stop {name}" + (f"  (auto-stop {minutes}m)" if minutes else ""))

    deadline = time.time() + minutes * 60 if minutes else None
    total = 0
    # Stream mixed audio straight to disk so memory stays flat and a crash
    # keeps whatever was recorded so far, instead of buffering the whole call.
    w = _open_wav(wav_path)
    try:
        while True:
            if os.path.exists(stop):
                break
            if deadline and time.time() >= deadline:
                break
            if mic_rec.error or spk_rec.error:
                break
            mic_buf = mic_rec.drain(SAMPLERATE, timeout=2.0)  # ~1 s
            spk_buf = spk_rec.drain(SAMPLERATE, timeout=2.0)
            mixed = _mix(mic_buf, spk_buf)
            if len(mixed):
                w.writeframes(_to_pcm16(mixed).tobytes())
                total += len(mixed)
    finally:
        mic_rec.stop()
        spk_rec.stop()
        time.sleep(0.3)
        # flush any audio still buffered in the queues after the stop signal
        tail = _mix(mic_rec.drain(10**9, timeout=0.5), spk_rec.drain(10**9, timeout=0.5))
        if len(tail):
            w.writeframes(_to_pcm16(tail).tobytes())
            total += len(tail)
        w.close()

    if mic_rec.error:
        print(f"WARN mic recorder: {mic_rec.error}")
    if spk_rec.error:
        print(f"WARN system recorder: {spk_rec.error}")

    print(f"Saved {total / SAMPLERATE:.0f}s of audio -> {wav_path}")
    _clear_stop(stop)


def _to_pcm16(audio):
    """Convert a float32 [-1, 1] mono stream to little-endian 16-bit PCM."""
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")


def _open_wav(path: str):
    """Open a WAV file configured for 16 kHz mono 16-bit, ready for writeframes."""
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SAMPLERATE)
    return w


def cmd_transcribe(wav_path: str, model: str, lang: str | None, out: str | None):
    if not os.path.exists(wav_path):
        raise SystemExit(f"No such file: {wav_path}")
    with wave.open(wav_path, "rb") as w:
        n = w.getnframes()
        ch = w.getnchannels()
        rate = w.getframerate()
        raw = w.readframes(n)
    arr = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if not len(arr):
        print("(empty audio file — nothing to transcribe)", file=sys.stderr)
        return
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    if rate != SAMPLERATE and len(arr):
        # linear resample to 16 kHz
        new_n = int(len(arr) * SAMPLERATE / rate)
        arr = np.interp(
            np.linspace(0, len(arr), new_n, endpoint=False),
            np.arange(len(arr)),
            arr,
        ).astype("float32")
    print(f"Transcribing {len(arr)/SAMPLERATE:.0f}s with {model} ...", file=sys.stderr)
    segments, info = _transcribe_array(arr, model, lang)
    lines = []
    for s in segments:
        lines.append(f"[{_fmt_ts(s.start)}] {s.text.strip()}")
    text = "\n".join(lines)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote transcript ({len(lines)} segments, lang={info.language}) -> {out}", file=sys.stderr)
    print(text)


def cmd_live(name: str, model: str, lang: str | None, chunk: float):
    mic, loopback = _open_devices()
    d = _session_dir(name)
    tpath = os.path.join(d, "transcript.txt")
    stop = _stop_path(name)
    _clear_stop(stop)

    # Start capturing BEFORE loading the model: large-v3 takes several seconds
    # to load into VRAM, and any audio during that window would otherwise be
    # lost (you'd miss the opening of every meeting). It buffers in the queues
    # and gets transcribed in the first chunk instead.
    mic_rec = _StreamRecorder(mic, "mic")
    spk_rec = _StreamRecorder(loopback, "system")
    mic_rec.start()
    spk_rec.start()

    elapsed = 0.0
    need = int(chunk * SAMPLERATE)
    # Everything past this point is under try/finally so the recorders (already
    # running) are always stopped — even if model load or the file open fails.
    try:
        print(f"Loading Whisper {model} ...", file=sys.stderr)
        _load_model(model)

        with open(tpath, "a", encoding="utf-8") as tf:
            tf.write(f"\n=== live session '{name}' started ===\n")
            tf.flush()
        print(f"Live '{name}' -> {tpath} (chunk {chunk}s). Stop: python meeting.py stop {name}", file=sys.stderr)

        while True:
            if os.path.exists(stop) or mic_rec.error or spk_rec.error:
                break
            mic_buf = mic_rec.drain(need, timeout=chunk + 3)
            spk_buf = spk_rec.drain(need, timeout=chunk + 3)
            mixed = _mix(mic_buf, spk_buf)
            if len(mixed) < SAMPLERATE // 2:  # <0.5s, skip
                continue
            segments, _ = _transcribe_array(mixed, model, lang)
            block_lines = []
            for s in segments:
                block_lines.append(f"[{_fmt_ts(elapsed + s.start)}] {s.text.strip()}")
            elapsed += len(mixed) / SAMPLERATE
            if block_lines:
                with open(tpath, "a", encoding="utf-8") as tf:
                    tf.write("\n".join(block_lines) + "\n")
                    tf.flush()
                print("\n".join(block_lines), flush=True)
    finally:
        mic_rec.stop()
        spk_rec.stop()
        _clear_stop(stop)
    print(f"Live session '{name}' ended ({elapsed:.0f}s).", file=sys.stderr)


def cmd_stop(name: str):
    open(_stop_path(name), "w").close()
    print(f"Stop signal sent to '{name}'.")


def cmd_status(name: str):
    tpath = os.path.join(_session_dir(name), "transcript.txt")
    if not os.path.exists(tpath):
        print(f"No transcript yet for '{name}'.")
        return
    with open(tpath, "r", encoding="utf-8") as f:
        print(f.read())


# --- CLI ---

def _arg(flag: str, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _positional(idx: int, what: str) -> str:
    if len(sys.argv) <= idx:
        raise SystemExit(f"Missing argument: {what}")
    return sys.argv[idx]


def main():
    logging.basicConfig(level=logging.WARNING)
    # Transcripts contain accented/Dutch characters that crash the default
    # Windows console codec (cp1252). Force UTF-8 so printing never fails.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - not all streams support it
            pass
    if not NUMPY_AVAILABLE:
        raise SystemExit("numpy not installed — run: pip install numpy")
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    model = _arg("--model", os.getenv("MEETING_WHISPER_MODEL", "large-v3"))
    lang = _arg("--lang", "nl")
    if lang == "auto":
        lang = None

    if cmd == "devices":
        cmd_devices()
    elif cmd == "record":
        name = _positional(2, "session name")
        minutes = _arg("--minutes")
        cmd_record(name, float(minutes) if minutes else None)
    elif cmd == "transcribe":
        cmd_transcribe(_positional(2, "wav path"), model, lang, _arg("--out"))
    elif cmd == "live":
        cmd_live(_positional(2, "session name"), model, lang, float(_arg("--chunk", "20")))
    elif cmd == "stop":
        cmd_stop(_positional(2, "session name"))
    elif cmd == "status":
        cmd_status(_positional(2, "session name"))
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
