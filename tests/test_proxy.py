"""Tests for ds4proxy.py. Run with: python3 -m unittest discover -s tests

The end-to-end test starts the real proxy on spare ports against a throwaway ds4 checkout whose
ds4-server is tests/fake-ds4-server, with HOME pointed at a temp dir so nothing touches the
real settings, token or log.
"""
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


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        ds4 = os.path.join(self.tmp, "ds4")
        os.makedirs(os.path.join(ds4, "gguf"))
        os.makedirs(self.home)
        shutil.copy(os.path.join(HERE, "fake-ds4-server"), os.path.join(ds4, "ds4-server"))
        with open(os.path.join(ds4, "gguf", "DeepSeek-V4-Flash-Test.gguf"), "wb") as f:
            f.write(b"\0" * 1024)
        env = dict(os.environ, HOME=self.home, DS4_DIR=ds4, DS4_MENU_BASE_PORT=str(BASE))
        self.log = open(os.path.join(self.tmp, "proxy.log"), "w+")
        self.proc = subprocess.Popen([sys.executable, "-I", PROXY], env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.token_path = os.path.join(self.home, ".local/share/ds4-menu/token")
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

    def control(self, method, path):
        with open(self.token_path) as f:
            token = f.read().strip()
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (BASE + 1, path), method=method,
                                     headers={"X-Token": token}, data=b"{}" if method == "POST" else None)
        return json.load(urllib.request.urlopen(req, timeout=30))

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


if __name__ == "__main__":
    unittest.main()
