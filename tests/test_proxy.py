"""Tests for ds4proxy.py. Run with: python3 -m unittest discover -s tests

The end-to-end test starts the real proxy on spare ports against a throwaway ds4 checkout whose
ds4-server is tests/fake-ds4-server, with HOME pointed at a temp dir so nothing touches the
real settings, token or log.
"""
import http.client
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "ds4proxy.py")
BASE = 18010


def load_proxy():
    spec = importlib.util.spec_from_file_location("ds4proxy", PROXY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sizing(unittest.TestCase):
    def setUp(self):
        self.mod = load_proxy()
        self.p = self.mod.Proxy.__new__(self.mod.Proxy)
        self.p.ram = 128 << 30
        self.p.settings = {"model": "DeepSeek-V4-Flash-Q2.gguf"}

    def test_disk_only_tables_dont_count(self):
        size = 177280286720  # Qwen3.8 Flash Next Q4 on disk
        ram = self.p.ram_size("Qwen3.8-Flash-Next-Q4.gguf", size)
        self.assertAlmostEqual(ram / (1 << 30), 69.7, delta=0.2)
        self.assertFalse(self.p.streaming(ram))

    def test_other_models_keep_their_size(self):
        self.assertEqual(self.p.ram_size("GLM-5.3-Flash-Q4_K.gguf", 123), 123)

    def test_streaming_threshold(self):
        self.assertTrue(self.p.streaming(178 << 30))
        self.assertFalse(self.p.streaming(91 << 30))

    def test_model_for_switches_family(self):
        self.p.models = lambda: {"DeepSeek-V4-Flash-Q2.gguf": 1, "Qwen3.8-Flash-Next-Q4.gguf": 1}

        def ask(model):
            return self.p.model_for(b'{"model": "%s", "messages": []}' % model)

        self.assertEqual(ask(b"qwen3.8-flash-next"), "Qwen3.8-Flash-Next-Q4.gguf")
        self.assertIsNone(ask(b"deepseek-v4-flash"))  # already loaded
        self.assertIsNone(ask(b"gpt-4o"))  # unknown family


class ProxyFixture:
    """Starts a real proxy against a throwaway ds4 checkout; mixed into TestCases (fuzz tests too)."""

    SETTINGS = {}

    def plant(self, state):
        """Hook for files to put in the state dir before the proxy starts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        ds4 = os.path.join(self.tmp, "ds4")
        os.makedirs(os.path.join(ds4, "gguf"))
        os.makedirs(self.home)
        shutil.copy(os.path.join(HERE, "fake-ds4-server"), os.path.join(ds4, "ds4-server"))
        with open(os.path.join(ds4, "gguf", "DeepSeek-V4-Flash-Test.gguf"), "wb") as f:
            f.write(b"\0" * 1024)
        self.state = os.path.join(self.home, ".local/share/ds4-menu")
        os.makedirs(self.state)
        if self.SETTINGS:
            with open(os.path.join(self.state, "settings.json"), "w") as f:
                json.dump(self.SETTINGS, f)
        self.plant(self.state)
        env = dict(os.environ, HOME=self.home, DS4_DIR=ds4, DS4_MENU_BASE_PORT=str(BASE))
        self.log = open(os.path.join(self.tmp, "proxy.log"), "w+")
        self.proc = subprocess.Popen([sys.executable, "-I", PROXY], env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.token_path = os.path.join(self.state, "token")
        for _ in range(50):
            if os.path.exists(self.token_path) and self.port_open(BASE):
                break
            time.sleep(0.2)
        else:
            self.fail("proxy didn't start:\n" + self.output())

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(30)
        self.log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def output(self):
        self.log.seek(0)
        return self.log.read()

    def port_open(self, port):
        # a bare connect: any HTTP request to the proxy port would start the server
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except OSError:
            return False
        return True

    def raw(self, method, path, headers=None, body=None, port=BASE):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        headers = dict({"Host": "127.0.0.1:%d" % port}, **(headers or {}))
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, json.loads(data or b"{}")

    def chat(self, headers=None):
        body = json.dumps({"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "ping"}]})
        return self.raw("POST", "/v1/chat/completions", dict({"Content-Type": "application/json"}, **(headers or {})),
                        body.encode())

    def assert_still_idle(self):
        self.assertEqual(self.control("GET", "/status")["state"], "idle")
        self.assertFalse(self.port_open(BASE + 2), "ds4-server was started")

    def control(self, method, path):
        with open(self.token_path) as f:
            token = f.read().strip()
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (BASE + 1, path), method=method,
                                     headers={"X-Token": token}, data=b"{}" if method == "POST" else None)
        return json.load(urllib.request.urlopen(req, timeout=30))



class EndToEnd(ProxyFixture, unittest.TestCase):
    def test_lazy_start_forward_and_stop(self):
        self.assertEqual(self.control("GET", "/status")["state"], "idle")

        body = json.dumps({"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "ping"}]}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % BASE, data=body,
                                     headers={"Content-Type": "application/json"})
        reply = json.load(urllib.request.urlopen(req, timeout=60))
        self.assertEqual(reply["choices"][0]["message"]["content"], "pong", self.output())

        status = self.control("GET", "/status")
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["model"], "DeepSeek-V4-Flash-Test.gguf")
        self.assertFalse(status["streaming"])

        self.assertEqual(self.control("POST", "/stop")["state"], "idle")
        self.assertFalse(self.port_open(BASE + 2), "fake ds4-server still running after /stop")

    def test_control_api_needs_token(self):
        req = urllib.request.Request("http://127.0.0.1:%d/status" % (BASE + 1), headers={"X-Token": "wrong"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 403)

    # -- the public port must not load the model for requests a web page can make --

    def test_cross_site_browser_request_is_refused(self):
        code, _ = self.raw("GET", "/", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"})
        self.assertEqual(code, 403)
        self.assert_still_idle()

    def test_foreign_origin_is_refused(self):
        code, _ = self.chat({"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        self.assert_still_idle()

    def test_localhost_origin_is_allowed(self):
        code, reply = self.chat({"Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site"})
        self.assertEqual((code, reply["choices"][0]["message"]["content"]), (200, "pong"))

    def test_rebinding_host_is_refused(self):
        code, _ = self.chat({"Host": "evil.example:%d" % BASE})
        self.assertEqual(code, 421)
        self.assert_still_idle()

    def test_other_routes_dont_load_the_model(self):
        for method, path in (("GET", "/"), ("GET", "/favicon.ico"), ("GET", "/v1/chat/completions"), ("PUT", "/v1/x")):
            code, _ = self.raw(method, path)
            self.assertEqual(code, 404, (method, path))
        self.assert_still_idle()

    # -- control port --

    def test_control_rejects_before_reading_body(self):
        s = socket.create_connection(("127.0.0.1", BASE + 1), timeout=5)
        # no token and a huge declared body: refused at once, without waiting for the body
        s.sendall(b"POST /settings HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\n")
        self.assertIn(b" 403 ", s.recv(4096))
        s.close()

    def test_control_rejects_oversized_body(self):
        with open(self.token_path) as f:
            token = f.read().strip()
        s = socket.create_connection(("127.0.0.1", BASE + 1), timeout=5)
        s.sendall(b"POST /settings HTTP/1.1\r\nHost: x\r\nX-Token: %s\r\nContent-Length: 999999999\r\n\r\n"
                  % token.encode())
        self.assertIn(b" 413 ", s.recv(4096))
        s.close()


class EndToEndApiKey(EndToEnd):
    SETTINGS = {"api_key": "s3cret"}

    def chat(self, headers=None):
        return super().chat(dict({"Authorization": "Bearer s3cret"}, **(headers or {})))

    def test_key_is_required(self):
        code, _ = EndToEnd.chat(self, {"Authorization": "Bearer wrong"})
        self.assertEqual(code, 401)
        code, _ = EndToEnd.chat(self)
        self.assertEqual(code, 401)
        self.assert_still_idle()

    def test_x_api_key_header_works(self):
        code, reply = EndToEnd.chat(self, {"x-api-key": "s3cret"})
        self.assertEqual((code, reply["choices"][0]["message"]["content"]), (200, "pong"))

    def test_lazy_start_forward_and_stop(self):
        code, reply = self.chat()
        self.assertEqual((code, reply["choices"][0]["message"]["content"]), (200, "pong"))


class PlantedSymlinks(EndToEnd):
    """A same-user process plants symlinks where the proxy writes; the targets must stay untouched."""

    def plant(self, state):
        self.victim = os.path.join(self.tmp, "victim.txt")
        with open(self.victim, "w") as f:
            f.write("precious")
        for name in ("token", "settings.json", "token.tmp", "settings.json.tmp"):
            os.symlink(self.victim, os.path.join(state, name))

    def test_symlinks_are_not_followed(self):
        self.control("POST", "/settings")  # forces a settings save too
        with open(self.victim) as f:
            self.assertEqual(f.read(), "precious")
        self.assertFalse(os.path.islink(self.token_path))
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
