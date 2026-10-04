import gzip
import json

import pytest
import requests

from dhandho.edgar import EdgarClient, EdgarError, RateLimiter, normalize_ticker


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_rate_limiter_spacing():
    c = FakeClock()
    rl = RateLimiter(8, clock=c.clock, sleep=c.sleep)
    times = []
    for _ in range(9):
        rl.wait()
        times.append(c.t)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g == pytest.approx(0.125) for g in gaps)
    assert times[-1] - times[0] == pytest.approx(1.0)  # 9 calls span 1s => never > 8/s


class FakeResp:
    def __init__(self, status, body=b"{}"):
        self.status_code = status
        self.content = body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_client(tmp_path, responses, refresh=False):
    c = FakeClock()
    sess = FakeSession(responses)
    client = EdgarClient("dhandho research a@b.com", tmp_path, RateLimiter(100, c.clock, c.sleep),
                         session=sess, refresh=refresh, sleep=c.sleep, backoff_base=2)
    return client, sess, c


def test_user_agent_header_and_cache(tmp_path):
    client, sess, _ = make_client(tmp_path, [FakeResp(200, b'{"cik": 1}')])
    assert sess.headers["User-Agent"] == "dhandho research a@b.com"
    assert client.companyfacts(1) == {"cik": 1}
    assert client.companyfacts(1) == {"cik": 1}  # second call from cache
    assert len(sess.urls) == 1
    path = tmp_path / "companyfacts" / "CIK0000000001.json.gz"
    with gzip.open(path, "rt") as fh:
        assert json.load(fh) == {"cik": 1}
    assert client.cache_meta("companyfacts", "CIK0000000001")["url"].endswith("CIK0000000001.json")


def test_refresh_refetches_once_per_run(tmp_path):
    client, sess, _ = make_client(tmp_path, [FakeResp(200, b'{"v": 1}')])
    client.companyfacts(1)
    client2, sess2, _ = make_client(tmp_path, [FakeResp(200, b'{"v": 2}')], refresh=True)
    assert client2.companyfacts(1) == {"v": 2}
    assert client2.companyfacts(1) == {"v": 2}
    assert len(sess2.urls) == 1


def test_retry_with_backoff_then_success(tmp_path):
    client, sess, clock = make_client(
        tmp_path, [FakeResp(429), requests.ConnectionError("boom"), FakeResp(200, b"{}")])
    assert client.submissions(5) == {}
    assert len(sess.urls) == 3
    assert [s for s in clock.sleeps if s >= 1] == [2, 4]


def test_404_raises_without_retry(tmp_path):
    client, sess, _ = make_client(tmp_path, [FakeResp(404)])
    with pytest.raises(EdgarError, match="404"):
        client.companyfacts(9)
    assert len(sess.urls) == 1


def test_gives_up_after_max_retries(tmp_path):
    client, sess, _ = make_client(tmp_path, [FakeResp(503)] * 5)
    with pytest.raises(EdgarError, match="giving up"):
        client.companyfacts(9)
    assert len(sess.urls) == 5


def test_requires_email_in_user_agent(tmp_path):
    with pytest.raises(EdgarError):
        EdgarClient("dhandho research", tmp_path, RateLimiter(1))


@pytest.mark.parametrize("raw,norm", [("brk.b", "BRK-B"), (" aapl ", "AAPL"), ("BRK/B", "BRK-B")])
def test_normalize_ticker(raw, norm):
    assert normalize_ticker(raw) == norm
