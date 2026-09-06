"""Tests for the music library module + the silent-video mux hook.

Runs with plain python3 — no pytest, no network, no PIL, no requests, no
ffmpeg: stub modules are injected BEFORE src imports (the test_day_complete.py
pattern), so src.main (which pulls render->PIL and requests transitively)
imports clean. subprocess.run and time.sleep are stubbed after the src
imports (attribute lookup happens at call time), so ffprobe/ffmpeg/
push_file.sh calls become deterministic fakes and _wait_fetchable's poll
is instant.

Covers the non-fatal mux contract: a queued video that already carries
audio publishes as-is; a silent one gets a library track muxed in, with
every failure (no library / download / mux / push / fetchable) falling
back to publishing the silent original plus a "reel_music" failure note.

Run:  python3 tests/test_music.py     (exit 0 = all passed)
"""
import datetime as dt
import functools
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ----------------------------------------------------------------- stub requests
_requests = types.ModuleType("requests")


class _RequestException(Exception):
    pass


# Per-test knobs for the fake network layer.
FAKE = {
    "get_status": 200, "get_exc": None, "head_status": 200,
    "fail_ffmpeg": False, "fail_push": False,
}


class _Resp:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


def _fake_get(url, timeout=None):
    if FAKE["get_exc"]:
        raise _RequestException()
    return _Resp(FAKE["get_status"], b"audio-bytes")


def _fake_head(url, timeout=None):
    return _Resp(FAKE["head_status"])


_requests.RequestException = _RequestException
_requests.get = _fake_get
_requests.post = lambda *a, **k: None
_requests.head = _fake_head
sys.modules["requests"] = _requests

# --------------------------------------------------------------------- stub PIL
_pil = types.ModuleType("PIL")
for _sub in ("Image", "ImageDraw", "ImageFont", "ImageOps"):
    _m = types.ModuleType(f"PIL.{_sub}")
    if _sub == "ImageFont":  # render.py type-annotates FreeTypeFont at def time
        _m.FreeTypeFont = object
    setattr(_pil, _sub, _m)
    sys.modules[f"PIL.{_sub}"] = _m
sys.modules["PIL"] = _pil

# ------------------------------------------------------------ imports under test
from src import main, music, state  # noqa: E402
from src.config import music_url, reel_url as reel_media_url  # noqa: E402

CFG = {"repo_owner": "ompatel1209", "repo_name": "instagram-agent"}
DATE = dt.date(2026, 9, 12)
DATE_STR = "2026-09-12"

OUT_DIR = ROOT / "tmp_music_test_out"
STATE_TMP = ROOT / "tmp_music_test_state.json"

# state.save would clobber the real state.json — point it at a scratch file.
state.STATE_PATH = STATE_TMP


# ------------------------------------------------------------ fake subprocess.run
class FakeProc:
    def __init__(self, args):
        self.args = args

    @property
    def returncode(self):
        if self.args[0] == "ffprobe":
            return 0
        if self.args[0] == "ffmpeg":
            if FAKE["fail_ffmpeg"]:
                return 1
            out = pathlib.Path(self.args[-1])  # emulate real ffmpeg output
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"muxed-bytes")
            return 0
        if self.args[0] == "bash":  # scripts/push_file.sh
            return 1 if FAKE["fail_push"] else 0
        raise AssertionError(f"unexpected command: {self.args}")

    @property
    def stdout(self):
        if self.args[0] == "ffprobe":
            name = pathlib.Path(self.args[-1]).name
            return "aac" if name.startswith("hasaudio") else ""
        return ""

    @property
    def stderr(self):
        return ""


def _fake_run(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args")
    return FakeProc(cmd)


import subprocess  # noqa: E402
import time  # noqa: E402

subprocess.run = _fake_run
time.sleep = lambda s: None  # _wait_fetchable's poll is instant in tests


# ---------------------------------------------------------------------- runner
TESTS = []


def test(fn):
    @functools.wraps(fn)
    def wrapped():
        FAKE.update(get_status=200, get_exc=None, head_status=200,
                    fail_ffmpeg=False, fail_push=False)
        music._library_cache = None
        OUT_DIR.mkdir(exist_ok=True)
        for f in OUT_DIR.iterdir():
            if f.is_file():
                f.unlink()
        fn()
    TESTS.append(wrapped)
    return wrapped


def _failures(st):
    return st.get("days", {}).get(DATE_STR, {}).get("failures", [])


# ------------------------------------------------------------------- library()
@test
def library_manifest_loads_eighteen_tracks():
    tracks = music.library()
    assert len(tracks) == 18, f"expected 18 tracks, got {len(tracks)}"
    for t in tracks:
        assert {"id", "filename", "mood", "seconds"} <= set(t), t


@test
def library_is_cached_between_calls():
    first = music.library()
    assert music._library_cache is first
    assert music.library() is first  # second call must not reload


@test
def library_missing_file_returns_empty():
    saved = music.MANIFEST_PATH
    try:
        music.MANIFEST_PATH = ROOT / "no-such-music.json"
        music._library_cache = None
        assert music.library() == []
    finally:
        music.MANIFEST_PATH = saved
        music._library_cache = None


# ------------------------------------------------------------------ pick_track()
@test
def pick_track_is_deterministic():
    assert music.pick_track(DATE) is music.pick_track(DATE)


@test
def pick_track_rotates_daily():
    d0 = music.pick_track(DATE)
    d1 = music.pick_track(DATE + dt.timedelta(days=1))
    d18 = music.pick_track(DATE + dt.timedelta(days=18))
    assert d0["id"] != d1["id"]
    assert d18["id"] == d0["id"]  # full cycle returns to the same track


@test
def pick_track_empty_library_returns_none():
    music._library_cache = []
    assert music.pick_track(DATE) is None


# ------------------------------------------------------------------- music_url()
@test
def music_url_builds_media_branch_url():
    assert music_url(CFG, "music/rain-window.m4a") == (
        "https://raw.githubusercontent.com/ompatel1209/instagram-agent"
        "/media/music/rain-window.m4a"
    )


# ---------------------------------------------------------------- download_track()
@test
def download_track_ok_writes_file():
    dest = OUT_DIR / "t.m4a"
    assert music.download_track(CFG, {"filename": "music/rain-window.m4a"}, dest)
    assert dest.read_bytes() == b"audio-bytes"


@test
def download_track_non_200_returns_false():
    FAKE["get_status"] = 404
    dest = OUT_DIR / "t.m4a"
    assert not music.download_track(CFG, {"filename": "music/x.m4a"}, dest)
    assert not dest.exists()


@test
def download_track_exception_returns_false():
    FAKE["get_exc"] = True
    assert not music.download_track(CFG, {"filename": "music/x.m4a"},
                                     OUT_DIR / "t.m4a")


# ----------------------------------------------------------- has_audio / mux
@test
def has_audio_reads_ffprobe_verdict():
    loud = OUT_DIR / "hasaudio-q.mp4"
    loud.write_bytes(b"v")
    silent = OUT_DIR / "q.mp4"
    silent.write_bytes(b"v")
    assert music.has_audio(loud)
    assert not music.has_audio(silent)


@test
def mux_success_creates_output():
    v, a, out = OUT_DIR / "v.mp4", OUT_DIR / "a.m4a", OUT_DIR / "out.mp4"
    v.write_bytes(b"v")
    a.write_bytes(b"a")
    assert music.mux(v, a, out)
    assert out.read_bytes() == b"muxed-bytes"


@test
def mux_failure_returns_false():
    FAKE["fail_ffmpeg"] = True
    v, a, out = OUT_DIR / "v.mp4", OUT_DIR / "a.m4a", OUT_DIR / "out.mp4"
    v.write_bytes(b"v")
    a.write_bytes(b"a")
    assert not music.mux(v, a, out)
    assert not out.exists()


# ------------------------------------------------- _ensure_reel_audio contract
def _run_ensure(src_name):
    st = {"days": {}}
    src = OUT_DIR / src_name
    src.write_bytes(b"video")
    url = main._ensure_reel_audio(
        {"repo_owner": CFG["repo_owner"], "repo_name": CFG["repo_name"]},
        DATE, DATE_STR, st, "https://example.com/src.mp4", src, OUT_DIR,
    )
    return url, st


@test
def ensure_audio_video_publishes_as_is():
    url, st = _run_ensure("hasaudio-q.mp4")
    assert url == "https://example.com/src.mp4"
    assert _failures(st) == []  # no reel_music note — nothing was at risk
    assert not (OUT_DIR / f"{DATE_STR}-reel-muxed.mp4").exists()


@test
def ensure_silent_no_library_falls_back():
    music._library_cache = []
    url, st = _run_ensure("q.mp4")
    assert url == "https://example.com/src.mp4"
    assert _failures(st)[0]["where"] == "reel_music"
    assert "library" in _failures(st)[0]["message"]


@test
def ensure_silent_download_fail_falls_back():
    FAKE["get_status"] = 404
    url, st = _run_ensure("q.mp4")
    assert url == "https://example.com/src.mp4"
    assert _failures(st)[0]["where"] == "reel_music"
    assert "download failed" in _failures(st)[0]["message"]


@test
def ensure_silent_mux_fail_falls_back():
    FAKE["fail_ffmpeg"] = True
    url, st = _run_ensure("q.mp4")
    assert url == "https://example.com/src.mp4"
    assert _failures(st)[0]["message"] == "ffmpeg mux failed"


@test
def ensure_silent_push_fail_falls_back():
    FAKE["fail_push"] = True
    url, st = _run_ensure("q.mp4")
    assert url == "https://example.com/src.mp4"
    assert "push failed" in _failures(st)[0]["message"]


@test
def ensure_silent_not_fetchable_falls_back():
    FAKE["head_status"] = 404
    url, st = _run_ensure("q.mp4")
    assert url == "https://example.com/src.mp4"
    assert "fetchable" in _failures(st)[0]["message"]


@test
def ensure_silent_all_ok_publishes_muxed_url():
    url, st = _run_ensure("q.mp4")
    assert url == reel_media_url(CFG, DATE_STR)
    assert _failures(st) == []
    assert (OUT_DIR / f"{DATE_STR}-reel-muxed.mp4").read_bytes() == b"muxed-bytes"
    assert (OUT_DIR / f"{DATE_STR}-audio.m4a").exists()


@test
def ensure_failure_is_persisted_to_state_file():
    STATE_TMP.unlink(missing_ok=True)
    music._library_cache = []
    _run_ensure("q.mp4")
    on_disk = state.load()  # reads the patched scratch STATE_PATH
    saved = on_disk["days"][DATE_STR]["failures"]
    assert saved and saved[0]["where"] == "reel_music"


# ---------------------------------------------------------------------- runner
def run_tests():
    failed = []
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:
            failed.append(fn.__name__)
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - len(failed)}/{len(TESTS)} passed")
    # scratch cleanup
    STATE_TMP.unlink(missing_ok=True)
    for f in OUT_DIR.iterdir():
        if f.is_file():
            f.unlink()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_tests())
