"""Property-based fuzzing of the proxy's request handling, with Hypothesis.

Three layers: the request-head parser, the public port's gate (refuse), and a live proxy fed raw
generated requests over a socket, checking the model is loaded exactly when the gate allows it.

Needs Python 3.11+ and `pip install --require-hashes -r tests/requirements-fuzz.txt`; it's skipped
otherwise (the proxy itself runs on the system Python 3.9). HYPOTHESIS_PROFILE=ci runs far more
examples, as CI does.
"""
import os
import re
import socket
import string
import unittest

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st
except ImportError:
    raise unittest.SkipTest("hypothesis isn't installed (see tests/requirements-fuzz.txt)") from None

from test_proxy import BASE, ProxyFixture, load_proxy

settings.register_profile("dev", max_examples=300)
settings.register_profile("ci", max_examples=3000)
PROFILE = os.environ.get("HYPOTHESIS_PROFILE", "dev")
settings.load_profile(PROFILE)
LIVE_EXAMPLES = 400 if PROFILE == "ci" else 60

mod = load_proxy()

# -- strategies ----------------------------------------------------------------------------------

TOKEN = string.ascii_letters + string.digits + "!#$%&'*+-.^_`|~"
token = st.text(TOKEN, min_size=1, max_size=20)
# anything latin-1 except the line breaks that end a header
value = st.text(st.characters(max_codepoint=255, exclude_characters="\r\n"), max_size=60)
loopback = st.builds(lambda h, p: h + p,
                     st.sampled_from(["127.0.0.1", "localhost", "LocalHost", "[::1]"]),
                     st.sampled_from(["", ":8010", ":%d" % BASE, ":1"]))
domain = st.from_regex(r"[a-z][a-z0-9-]{0,15}\.(com|net|example|dev|io)", fullmatch=True)
foreign_host = st.one_of(domain, domain.map(lambda d: d + ":%d" % BASE), value)
host = st.one_of(loopback, foreign_host)
scheme = st.sampled_from(["http", "https", "file", ""])
origin = st.one_of(
    st.builds(lambda s, h: s + "://" + h, scheme, st.one_of(loopback, domain)),
    # bracketed hosts: what urlsplit chokes on when the IPv6 literal is malformed
    st.builds(lambda s, inner, end: s + "://[" + inner + end, scheme, st.text(string.hexdigits + ":.z%", max_size=12),
              st.sampled_from(["", "]", "]:80", "]x"])),
    st.just("null"),
    value,
)
fetch_site = st.one_of(st.sampled_from(["none", "same-origin", "same-site", "cross-site"]), value)
method = st.one_of(st.sampled_from(["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"]), token)
path = st.one_of(
    st.builds(lambda tail: "/v1/" + tail, st.text(TOKEN + "/?=&", max_size=30)),
    st.sampled_from(["/", "/v1/models", "/v1/chat/completions", "/favicon.ico", "/v1", "//v1/x"]),
    st.builds(lambda tail: "/" + tail, st.text(TOKEN + "/?=&%", max_size=30)),
)


NO_KEY = st.none()


@st.composite
def request(draw, with_key=NO_KEY, hosts=host):
    """(method, path, headers): headers as given to refuse(), names lowercased."""
    hdrs = {"host": draw(hosts)}
    if draw(st.booleans()):
        hdrs["origin"] = draw(origin)
    if draw(st.booleans()):
        hdrs["sec-fetch-site"] = draw(fetch_site)
    key = draw(with_key)
    if key is not None:
        hdrs[draw(st.sampled_from(["authorization", "x-api-key"]))] = key
    for name, val in draw(st.dictionaries(token.map(str.lower), value, max_size=4)).items():
        hdrs.setdefault(name, val)
    return draw(method), draw(path), {k: v.strip() for k, v in hdrs.items()}


# requests from a local client: these get past the Host check, so the rest of the gate is exercised
local_request = st.one_of(
    request(hosts=loopback),
    request(hosts=loopback).map(lambda r: ("POST", "/v1/chat/completions", r[2])),
)


def to_bytes(meth, pth, hdrs):
    head = "%s %s HTTP/1.1\r\n" % (meth, pth) + "".join("%s: %s\r\n" % kv for kv in hdrs.items()) + "\r\n"
    return head.encode("latin-1")


def gate(api_key=None):
    p = mod.Proxy.__new__(mod.Proxy)
    p.settings = {"api_key": api_key}
    return p


# -- the parser ----------------------------------------------------------------------------------


class ParseHead(unittest.TestCase):
    @given(st.binary(max_size=2000))
    def test_never_crashes(self, data):
        meth, pth, hdrs = mod.parse_head(data + b"\r\n\r\n")
        self.assertIsInstance(meth, str)
        self.assertIsInstance(pth, str)
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k, v in hdrs.items()))

    @given(request())
    def test_round_trips(self, req):
        meth, pth, hdrs = req
        self.assertEqual(mod.parse_head(to_bytes(meth, pth, hdrs)), (meth, pth, hdrs))


# -- the gate ------------------------------------------------------------------------------------


class Gate(unittest.TestCase):
    @given(st.one_of(request(), local_request))
    def test_total_on_requests(self, req):
        """Realistic requests too: every one gets an answer, never an exception."""
        refused = gate().refuse(*req)
        self.assertIn(refused and refused[0], (None, 401, 403, 404, 421))

    @given(method, path, st.dictionaries(st.text(max_size=20), st.text(max_size=60)), st.none() | value)
    def test_total(self, meth, pth, hdrs, key):
        """Any input gets an answer, never an exception (which would drop the connection)."""
        refused = gate(key).refuse(meth, pth, hdrs)
        if refused is not None:
            code, why = refused
            self.assertIn(code, (401, 403, 404, 421))
            self.assertIsInstance(why, str)

    @given(request())
    def test_non_loopback_host_is_refused(self, req):
        meth, pth, hdrs = req
        if not mod.LOOPBACK.match(hdrs.get("host", "")):
            self.assertEqual(gate().refuse(meth, pth, hdrs)[0], 421)

    @given(request(), loopback, domain)
    def test_foreign_origin_is_refused(self, req, h, dom):
        meth, pth, hdrs = req
        hdrs.update(host=h, origin="https://" + dom)
        self.assertEqual(gate().refuse(meth, pth, hdrs)[0], 403)

    @given(request(), loopback)
    def test_cross_site_is_refused(self, req, h):
        meth, pth, hdrs = req
        hdrs.update(host=h, **{"sec-fetch-site": "cross-site"})
        hdrs.pop("origin", None)
        self.assertEqual(gate().refuse(meth, pth, hdrs)[0], 403)

    @given(st.text(min_size=1, max_size=40).filter(str.strip), request(with_key=st.none() | value), loopback)
    def test_wrong_key_is_refused(self, key, req, h):
        meth, pth, hdrs = req
        # the rule: x-api-key, or Authorization with an optional "Bearer " prefix, equal to the key
        presented = hdrs.get("x-api-key") or re.sub(r"^bearer\s+", "", hdrs.get("authorization", ""), flags=re.I)
        if presented == key:
            return
        hdrs.update(host=h)
        hdrs.pop("origin", None)
        hdrs.pop("sec-fetch-site", None)
        refused = gate(key).refuse(meth, pth, hdrs)
        self.assertIsNotNone(refused)
        self.assertIn(refused[0], (401, 404))

    @given(st.text(TOKEN + "/", max_size=30), loopback,
           st.none() | st.builds(lambda h: "http://" + h, loopback), st.sampled_from([None, "none", "same-origin"]))
    def test_plain_local_clients_are_allowed(self, tail, h, org, site):
        hdrs = {"host": h, "content-type": "application/json"}
        if org:
            hdrs["origin"] = org
        if site:
            hdrs["sec-fetch-site"] = site
        self.assertIsNone(gate().refuse("POST", "/v1/" + tail, hdrs))

    @given(st.binary(max_size=3000))
    def test_model_for_never_crashes(self, data):
        p = gate()
        p.settings["model"] = "DeepSeek-V4-Flash-Q2.gguf"
        p.models = lambda: {"DeepSeek-V4-Flash-Q2.gguf": 1, "Qwen3.8-Flash-Next-Q4.gguf": 1}
        self.assertIn(p.model_for(data), (None, "Qwen3.8-Flash-Next-Q4.gguf"))


# -- a live proxy --------------------------------------------------------------------------------


class LiveGate(ProxyFixture, unittest.TestCase):
    """Raw generated requests at a running proxy: the model loads if and only if the gate allows."""

    def send(self, data):
        s = socket.create_connection(("127.0.0.1", BASE), timeout=30)
        try:
            s.sendall(data)
            reply = b""
            while chunk := s.recv(65536):
                reply += chunk
        except OSError:
            pass
        finally:
            s.close()
        return reply

    @settings(max_examples=LIVE_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.one_of(request(), local_request))
    def test_model_loads_only_when_allowed(self, req):
        refused = gate().refuse(*req)
        reply = self.send(to_bytes(*req))
        state = self.control("GET", "/status")["state"]
        if refused is None:
            self.assertEqual(state, "ready", "allowed request didn't reach the model: %r" % (req,))
            self.control("POST", "/stop")
        else:
            self.assertTrue(reply.startswith(b"HTTP/1.1 %d " % refused[0]), (req, reply[:80]))
            self.assertEqual(state, "idle", "refused request loaded the model: %r" % (req,))
            self.assertFalse(self.port_open(BASE + 2))

    @settings(max_examples=LIVE_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.binary(max_size=4000))
    def test_garbage_never_loads_or_kills_it(self, data):
        self.send(data + b"\r\n\r\n")
        self.assertEqual(self.control("GET", "/status")["state"], "idle")


if __name__ == "__main__":
    unittest.main()
