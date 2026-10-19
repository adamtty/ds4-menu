"""ds4proxy: an oMLX-style front door for antirez's ds4-server.

Listens on 127.0.0.1:8010 all the time and costs almost nothing. The first request starts
ds4-server on a private port and is forwarded once the model is loaded; after a stretch with
no traffic the server is stopped again so its ~90 GB of RAM goes back to the Mac.
A request naming another model family ("model": "glm-5.3-flash" while DeepSeek is loaded)
switches models first, so clients can pick the model the usual way.

Started (and supervised) by the ds4 menu bar app, never by launchd directly: the model lives in
~/Documents, which macOS only lets the app read, and children inherit that permission.

Control API on 127.0.0.1:8011, every request needs the X-Token header (token file in the state
dir, mode 600):
  GET  /status
  POST /start  /stop  /settings  {"pinned": bool, "idle_min": int, "model": "file.gguf"}
"""
import asyncio
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time

DS4_DIR = os.environ.get("DS4_DIR", os.path.expanduser("~/Documents/code-work/ds4"))
STATE = os.path.expanduser("~/.local/share/ds4-menu")
LISTEN_PORT, CONTROL_PORT, BACKEND_PORT = 8010, 8011, 8012
LOCK_FILE = "/tmp/ds4.lock"  # ds4's own one-process-at-a-time lock
DEFAULTS = {"model": "", "pinned": False, "idle_min": 15, "models": {}, "agent_args": []}
SKIP_GGUF = re.compile(r"encoder|dspark|\.part$", re.I)
SPEED_RE = re.compile(r" (prefill|decoding) .*avg=([\d.]+) t/s")
MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"]+)"')
FAMILIES = ("glm", "deepseek", "qwen")
# tables ds4 reads straight from the file and never maps, so they don't count toward RAM
DISK_ONLY = {"qwen3.8-flash-next": int(95.37 * (1 << 30))}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def ram_bytes():
    return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout)


def available_bytes():
    """Free + reclaimable pages; close enough to 'what a new 90 GB mmap can get'."""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", out).group(1))
    n = {k: int(v) for k, v in re.findall(r"Pages (\w+):\s+(\d+)", out)}
    return page * sum(n.get(k, 0) for k in ("free", "inactive", "speculative", "purgeable"))


def pid_command(pid):
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return out.strip()


class Proxy:
    def __init__(self):
        self.settings = dict(DEFAULTS)
        try:
            with open(os.path.join(STATE, "settings.json")) as f:
                self.settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.ram = ram_bytes()
        self.proc = None
        self.state = "idle"  # idle | loading | ready | stopping
        self.error = ""
        self.loaded_at = 0.0
        self.start_lock = asyncio.Lock()
        self.switch_lock = asyncio.Lock()
        self.conns = {}  # id -> [last client byte, last server byte]
        self.last_activity = time.time()
        self.speeds = {}
        if not self.settings["model"] or self.settings["model"] not in self.models():
            self.settings["model"] = self.default_model()

    # -- settings / models ------------------------------------------------------

    def save(self):
        tmp = os.path.join(STATE, "settings.json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.settings, f, indent=2)
        os.replace(tmp, os.path.join(STATE, "settings.json"))

    def models(self):
        d = os.path.join(DS4_DIR, "gguf")
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".gguf") and not SKIP_GGUF.search(n))
        except OSError:
            return {}
        return {n: self.ram_size(n, os.path.getsize(os.path.join(d, n))) for n in names}

    def ram_size(self, name, size):
        """Bytes a model needs in memory: its file size minus any disk-only tables."""
        skip = next((b for k, b in DISK_ONLY.items() if k in name.lower()), 0)
        return max(size - skip, 1)

    def default_model(self):
        link = os.path.join(DS4_DIR, "ds4flash.gguf")
        target = os.path.basename(os.path.realpath(link)) if os.path.exists(link) else ""
        models = self.models()
        return target if target in models else next(iter(models), "")

    def model_for(self, request):
        """The gguf a request asks for, when it names another model family than the loaded one."""
        m = MODEL_RE.search(request)
        want = m.group(1).decode(errors="replace").lower() if m else ""
        family = next((f for f in FAMILIES if f in want), None)
        if not family or family in self.settings["model"].lower():
            return None
        return next((n for n in self.models() if family in n.lower()), None)

    def streaming(self, size):
        return size > 0.8 * self.ram

    def server_args(self):
        name = self.settings["model"]
        size = self.models().get(name, 0)
        over = self.settings["models"].get(name, {})
        # SSD streaming models start with a small context, as ds4's docs suggest
        ctx = over.get("ctx", 32768 if self.streaming(size) else 100000)
        args = ["./ds4-server", "-m", os.path.join("gguf", name), "--host", "127.0.0.1",
                "--port", str(BACKEND_PORT), "--ctx", str(ctx),
                "--kv-disk-dir", os.path.expanduser("~/.ds4/server-kv"), "--kv-disk-space-mb", "16384"]
        if self.streaming(size):
            args.append("--ssd-streaming")
        return args + over.get("args", [])

    # -- the ds4 lock: who else is using the GPU/RAM -----------------------------

    def other_ds4(self):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip() or 0)
        except (OSError, ValueError):
            return None
        if not pid or (self.proc and pid == self.proc.pid):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        cmd = pid_command(pid)
        return {"pid": pid, "command": cmd} if "ds4" in cmd else None

    # -- server lifecycle -------------------------------------------------------

    async def ensure_started(self, force=False):
        """Returns "" when the backend is ready, otherwise why it isn't."""
        async with self.start_lock:
            if self.state == "ready" and self.proc and self.proc.returncode is None:
                return ""
            other = self.other_ds4()
            if other:
                self.error = "another ds4 is running: " + other["command"][:120]
                return self.error
            name = self.settings["model"]
            size = self.models().get(name)
            if not size:
                self.error = "no model found in " + os.path.join(DS4_DIR, "gguf")
                return self.error
            need = (self.ram // 8) if self.streaming(size) else size
            # macOS hands an unloaded model's RAM back over a few seconds, so give it time
            for _ in range(20):
                if force or available_bytes() + (4 << 30) >= need:
                    break
                await asyncio.sleep(1)
            else:
                self.error = ("not enough free RAM to load %s (%.0f GB needed, %.0f GB free) "
                              "- is an oMLX model loaded? Use Load now in the menu to force it."
                              % (name, need / 1e9, available_bytes() / 1e9))
                return self.error
            self.state, self.error, self.speeds = "loading", "", {}
            self.loaded_at = time.time()
            log("starting:", " ".join(self.server_args()))
            self.proc = await asyncio.create_subprocess_exec(
                *self.server_args(), cwd=DS4_DIR, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            asyncio.ensure_future(self.pump_output(self.proc))
            while self.proc.returncode is None:
                if await self.backend_up():
                    self.state = "ready"
                    self.last_activity = time.time()
                    log("ready in %.1fs" % (time.time() - self.loaded_at))
                    return ""
                await asyncio.sleep(0.5)
            self.state = "idle"
            self.error = "ds4-server exited during load (code %s), see the log" % self.proc.returncode
            return self.error

    async def backend_up(self):
        try:
            r, w = await asyncio.open_connection("127.0.0.1", BACKEND_PORT)
        except OSError:
            return False
        w.write(b"GET /v1/models HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        try:
            line = await asyncio.wait_for(r.readline(), 5)
        except asyncio.TimeoutError:
            line = b""
        w.close()
        return b" 200 " in line

    async def pump_output(self, proc):
        buf = b""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for raw in lines:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                m = SPEED_RE.search(line)
                if m:  # progress lines arrive many times a second; keep only the numbers
                    self.speeds["prefill" if m.group(1) == "prefill" else "gen"] = float(m.group(2))
                else:
                    print(line, flush=True)
        code = await proc.wait()
        log("ds4-server exited", code)
        if self.proc is proc:
            self.proc, self.state = None, "idle"

    async def stop(self, why):
        p = self.proc
        if not p or p.returncode is not None:
            self.proc, self.state = None, "idle"
            return
        log("stopping:", why)
        self.state = "stopping"
        p.terminate()
        try:
            await asyncio.wait_for(p.wait(), 30)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
        self.proc, self.state = None, "idle"

    def idle_seconds(self):
        return time.time() - self.last_activity

    def busy(self):
        # a request is in flight if the client spoke after the server last answered
        # (covers long prefills, when the server is silent for minutes)
        return any(up > down for up, down in self.conns.values())

    async def watchdog(self):
        while True:
            await asyncio.sleep(10)
            if os.getppid() == 1:  # the menu app went away without telling us
                await self.stop("menu app exited")
                os._exit(0)
            if (self.state == "ready" and not self.settings["pinned"] and not self.busy()
                    and self.idle_seconds() > self.settings["idle_min"] * 60):
                await self.stop("idle for %d min" % self.settings["idle_min"])

    # -- the public port: raw TCP piping, so streaming just works -----------------

    async def client(self, reader, writer):
        cid = id(writer)
        # read the request head first: it names the model, and a refusal can be a proper HTTP
        # reply (ds4-server answers one request per connection, so this is the only one)
        try:
            first = await asyncio.wait_for(reader.read(65536), 30)
        except asyncio.TimeoutError:
            first = b""
        if not first:
            writer.close()
            return
        err = ""
        async with self.switch_lock:  # two requests for the new model switch once
            want = self.model_for(first)
            if want and self.busy():
                err = "busy with %s for another request, retry when it's done" % self.settings["model"]
            elif want:
                log("request for", want)
                await self.change({"model": want})
        if not err and self.state != "ready":
            err = await self.ensure_started()
        if err:
            body = json.dumps({"error": {"type": "unavailable", "message": "ds4: " + err}}).encode()
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                         b"Connection: close\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
            writer.close()
            return
        try:
            br, bw = await asyncio.open_connection("127.0.0.1", BACKEND_PORT)
        except OSError:
            writer.close()
            return
        now = time.time()
        self.conns[cid] = [now, 0.0]
        self.last_activity = now
        bw.write(first)

        async def pipe(src, dst, slot):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    t = time.time()
                    self.conns[cid][slot] = t
                    self.last_activity = t
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(pipe(reader, bw, 0), pipe(br, writer, 1))
        self.conns.pop(cid, None)
        self.last_activity = time.time()

    # -- control API --------------------------------------------------------------

    def status(self):
        models = self.models()
        size = models.get(self.settings["model"], 0)
        left = max(0, self.settings["idle_min"] * 60 - self.idle_seconds())
        return {
            "state": self.state, "error": self.error, "model": self.settings["model"],
            "streaming": self.streaming(size), "pinned": self.settings["pinned"],
            "idle_min": self.settings["idle_min"], "unload_in": None if self.state != "ready" or self.settings["pinned"] else int(left),
            "busy": self.busy(), "connections": len(self.conns), "speeds": self.speeds,
            "loading_for": int(time.time() - self.loaded_at) if self.state == "loading" else 0,
            "models": [{"file": n, "bytes": s, "streaming": self.streaming(s)} for n, s in models.items()],
            "model_bytes": size, "free_bytes": available_bytes(), "ram_bytes": self.ram,
            "other_ds4": self.other_ds4(), "endpoint": "http://127.0.0.1:%d" % LISTEN_PORT,
            "pid": self.proc.pid if self.proc else None,
            "ds4_dir": DS4_DIR, "agent_args": self.settings["agent_args"],
        }

    async def control(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin-1").split("\r\n")
            method, path = lines[0].split(" ")[:2]
            hdrs = {k.lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            body = await reader.readexactly(int(hdrs.get("content-length", 0) or 0))
            if not secrets.compare_digest(hdrs.get("x-token", ""), self.token):
                code, out = 403, {"error": "bad token"}
            elif (method, path) == ("GET", "/status"):
                code, out = 200, self.status()
            elif (method, path) == ("POST", "/start"):
                err = await self.ensure_started(force=True)
                code, out = (500, {"error": err}) if err else (200, self.status())
            elif (method, path) == ("POST", "/stop"):
                await self.stop("asked from the menu")
                code, out = 200, self.status()
            elif (method, path) == ("POST", "/settings"):
                code, out = await self.change(json.loads(body or b"{}"))
            else:
                code, out = 404, {"error": "not found"}
        except Exception as e:  # malformed request; nothing to keep alive for
            code, out = 400, {"error": str(e)}
        data = json.dumps(out).encode()
        writer.write(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nConnection: close\r\n"
                     b"Content-Length: %d\r\n\r\n" % (code, len(data)) + data)
        await writer.drain()
        writer.close()

    async def change(self, req):
        if "pinned" in req:
            self.settings["pinned"] = bool(req["pinned"])
            self.last_activity = time.time()
        if "idle_min" in req:
            self.settings["idle_min"] = max(1, min(24 * 60, int(req["idle_min"])))
        if "model" in req and req["model"] != self.settings["model"]:
            if req["model"] not in self.models():
                return 400, {"error": "unknown model"}
            was_running = self.state in ("ready", "loading")
            await self.stop("switching model")
            self.settings["model"] = req["model"]
            self.save()
            if was_running:
                asyncio.ensure_future(self.ensure_started(force=True))
        self.save()
        return 200, self.status()

    async def main(self):
        os.makedirs(STATE, exist_ok=True)
        self.token = secrets.token_hex(16)
        path = os.path.join(STATE, "token")
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, self.token.encode())
        os.close(fd)
        os.replace(path + ".tmp", path)
        self.save()
        public = await asyncio.start_server(self.client, "127.0.0.1", LISTEN_PORT)
        ctl = await asyncio.start_server(self.control, "127.0.0.1", CONTROL_PORT)
        log("listening on 127.0.0.1:%d (control :%d), model %s" % (LISTEN_PORT, CONTROL_PORT, self.settings["model"]))
        loop = asyncio.get_event_loop()
        done = asyncio.Event()
        for s in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(s, done.set)
        wd = asyncio.ensure_future(self.watchdog())
        await done.wait()
        wd.cancel()
        public.close()
        ctl.close()
        await self.stop("shutting down")


if __name__ == "__main__":
    if not os.path.isdir(DS4_DIR):
        sys.exit("ds4 not found at " + DS4_DIR)
    asyncio.get_event_loop().run_until_complete(Proxy().main())
