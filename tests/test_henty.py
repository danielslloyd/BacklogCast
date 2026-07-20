"""Stage C: Henty client + ASR feedback loop.

The HTTP client is checked with a mocked transport; the retry loop is checked
with a scripted fake client (no HTTP, no GPU).
"""
import json

import httpx

from app import henty
from app.henty import ChunkResult, HentyClient, synthesize_chunk_with_retries


def test_resolve_markup():
    assert henty.resolve_markup("{Beauchamp|BEE-chum} spoke") == "BEE-chum spoke"
    assert henty.resolve_markup("no markup here") == "no markup here"
    assert henty.resolve_markup("{just display}") == "{just display}"


def test_norm_similarity():
    assert henty._norm_similarity(92.5) == 0.925   # Henty's 0..100 scale
    assert henty._norm_similarity(0.9) == 0.9      # already 0..1
    assert henty._norm_similarity(None) == 0.0


class FakeClient:
    """Scripted client: `script` is a list of (audio_file, truncated, sim_0_100)
    returned by successive generate_chunk calls."""

    def __init__(self, script, info=None):
        self.script = list(script)
        self.i = 0
        self.best_set = []
        self.stitched = []
        self._info = info or {"metadata": {"chapters": []}}
        self._last_sim = 0.0

    def generate_chunk(self, tfid, cid, text, **kw):
        af, trunc, sim = self.script[self.i]
        self.i += 1
        self._last_sim = sim
        return {"audio_file": af, "possibly_truncated": trunc}

    def transcribe_take(self, audio_file, text, tfid, cid):
        return {"similarity_score": self._last_sim}

    def set_best_take(self, tfid, cid, fname):
        self.best_set.append((tfid, cid, fname))
        return {"success": True}

    def project_info(self):
        return self._info

    def stitch_chapter(self, cid):
        self.stitched.append(cid)
        return {"stitched_filename": f"ch{cid}.wav", "duration_seconds": 5}


def test_loop_stops_when_threshold_met():
    c = FakeClient([("a1.wav", False, 50), ("a2.wav", False, 90)])
    res = synthesize_chunk_with_retries(c, "ch0", 1, "hello", threshold=0.85, max_retries=4)
    assert res.attempts == 2 and res.ok
    assert res.similarity == 0.9
    assert c.best_set == [("ch0", 1, "a2.wav")]  # best (and only good) take set


def test_loop_keeps_best_when_never_meets_threshold():
    c = FakeClient([("a1.wav", False, 60), ("a2.wav", False, 80), ("a3.wav", False, 70)])
    res = synthesize_chunk_with_retries(c, "ch0", 2, "hi", threshold=0.85, max_retries=2)
    assert res.attempts == 3 and not res.ok
    assert res.similarity == 0.8
    assert c.best_set == [("ch0", 2, "a2.wav")]  # highest-scoring take kept


def test_loop_prefers_non_truncated_on_tie():
    # first take scores high but is truncated; second ties but is clean -> wins
    c = FakeClient([("a1.wav", True, 95), ("a2.wav", False, 95)])
    res = synthesize_chunk_with_retries(c, "ch0", 3, "hi", threshold=0.85, max_retries=3)
    assert res.attempts == 2 and res.ok
    assert res.audio_file == "a2.wav" and not res.truncated
    assert c.best_set == [("ch0", 3, "a2.wav")]


def test_synthesize_project_iterates_and_stitches():
    info = {"metadata": {"chapters": [
        {"id": "c1", "chunks": [
            {"id": 1, "type": "text", "text": "one"},
            {"id": 2, "type": "text", "text": "two"},
            {"id": 3, "type": "pause", "text": ""},      # skipped
        ]},
        {"id": "c2", "chunks": [{"id": 4, "type": "text", "text": "three"}]},
    ]}}
    # 3 voiceable chunks, each good on first try
    c = FakeClient([("t.wav", False, 99)] * 3, info=info)
    out = henty.synthesize_project(c, threshold=0.85, max_retries=1)
    assert len(out["reports"]) == 3
    assert all(isinstance(r, ChunkResult) and r.ok for r in out["reports"])
    assert c.stitched == ["c1", "c2"]
    assert len(out["stitched"]) == 2


def test_http_client_sends_key_and_payload():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("X-API-Key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"audio_file": "x.wav", "possibly_truncated": False})

    client = HentyClient(base_url="http://henty.test", api_key="secret",
                         transport=httpx.MockTransport(handler))
    out = client.generate_chunk("ch0", 1, "hello", voice_sample="Haggard")
    assert out["audio_file"] == "x.wav"
    assert seen["path"] == "/api/project/generate-chunk-audio"
    assert seen["key"] == "secret"
    assert seen["body"]["chunk_text"] == "hello"
    assert seen["body"]["voice_sample"] == "Haggard"
    client.close()
