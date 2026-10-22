#!/usr/bin/env python3
"""Agent Monitor: local dashboard of running AI coding agents, grouped by account.

Run:  python3 server.py [--port 8787]
Open: http://127.0.0.1:8787
Read-only; binds to localhost only. Standard library only.
"""
import argparse, base64, glob, json, os, pwd, re, secrets, signal, subprocess, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_RE = re.compile(r"^(?:.*?/)?(claude|codex|gemini|aider)(?: |$)")
SKIP = ("disclaimer", "/bin/dash", "chrome-native-host", "crashpad", "--type=")
_cache = {}  # (path) -> (mtime, value)
_prev = {}  # pid -> (cpu_seconds, wall_time) from the previous scan
RECENT = 30  # seconds of transcript/CPU activity that count as "active"


def sh(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def read_json(path):
    try:
        m = os.stat(path).st_mtime
        if path in _cache and _cache[path][0] == m:
            return _cache[path][1]
        with open(path) as f:
            v = json.load(f)
        _cache[path] = (m, v)
        return v
    except Exception:
        return None


def jwt_email(tok):
    try:
        p = tok.split(".")[1]
        d = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        return d.get("email") or d.get("https://api.openai.com/profile", {}).get("email")
    except Exception:
        return None


def account_for(kind, user, config_dir):
    """Best-effort identity of the login an agent process is using."""
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        return None
    if kind == "claude":
        paths = []
        if config_dir:
            paths.append(os.path.join(config_dir, ".claude.json"))
        paths.append(os.path.join(home, ".claude.json"))
        for p in paths:
            o = (read_json(p) or {}).get("oauthAccount")
            if o and o.get("emailAddress"):
                return o["emailAddress"]
    elif kind == "codex":
        a = read_json(os.path.join(home, ".codex", "auth.json")) or {}
        tok = (a.get("tokens") or {}).get("id_token")
        return jwt_email(tok) if tok else None
    return None


def cpu_secs(s):
    p = s.split(":")
    t = 0.0
    for x in p:
        t = t * 60 + float(x)
    return t


def last_write(cwd):
    """Age in seconds of the newest Claude transcript for this folder, if any."""
    if not cwd:
        return None
    try:
        d = os.path.expanduser("~/.claude/projects/") + re.sub(r"[^A-Za-z0-9]", "-", cwd)
        m = max(os.stat(os.path.join(d, f)).st_mtime for f in os.listdir(d) if f.endswith(".jsonl"))
        return time.time() - m
    except Exception:
        return None


def transcript_title(home, sid):
    """Latest rename (custom-title) or auto title (ai-title) from the session transcript."""
    for p in glob.glob(os.path.join(home, ".claude", "projects", "*", sid + ".jsonl")):
        try:
            m = os.stat(p).st_mtime
            if p in _cache and _cache[p][0] == m:
                return _cache[p][1]
            with open(p, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 400_000))
                tail = f.read().decode("utf-8", "ignore")
            best = None
            for kind, key in (("ai-title", "aiTitle"), ("custom-title", "customTitle")):  # custom wins
                hits = re.findall(r'"type":"%s","%s":"((?:[^"\\]|\\.)*)"' % (kind, key), tail)
                if hits:
                    best = json.loads('"%s"' % hits[-1])
            _cache[p] = (m, best)
            return best
        except Exception:
            pass
    return None


def desktop_title(home, cli_sid):
    base = os.path.join(home, "Library", "Application Support", "Claude", "claude-code-sessions")
    for p in glob.glob(os.path.join(base, "*", "*", "local_*.json")):
        d = read_json(p) or {}
        if d.get("cliSessionId") == cli_sid and d.get("title"):
            return d["title"]
    return None


def session_title(home, sf):
    """Chat name: user-set name, else Desktop title, else transcript title, else the auto name."""
    if not sf:
        return None
    sid = sf.get("sessionId")
    user_name = sf.get("name") if sf.get("nameSource") == "user" else None
    return (user_name or desktop_title(home, sid) or (transcript_title(home, sid) if sid else None)
            or sf.get("name"))


def _clip(t, n=140):
    t = re.sub(r"\s+", " ", re.sub(r"[`*#>_]|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", t or "")).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _tool_line(b):
    n, i = b.get("name", "tool"), b.get("input") or {}
    f = lambda k: os.path.basename(str(i.get(k) or ""))
    if n == "Bash":
        cmd = re.sub(r"^\s*cd\s+\S+\s*(&&|;)\s*", "", str(i.get("command") or "")).strip().split("\n")[0]
        return "Running: " + _clip(i.get("description") or cmd, 100)
    if n in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"{ {'Read': 'Reading', 'Write': 'Writing'}.get(n, 'Editing') } {f('file_path') or f('notebook_path')}"
    if n in ("Grep", "Glob"):
        return f"Searching: {_clip(i.get('pattern'), 60)}"
    if n in ("Task", "Agent"):
        return "Delegating: " + _clip(i.get("description") or i.get("prompt"), 100)
    if n.startswith("mcp__"):
        return "Calling " + n.split("__", 1)[-1].replace("__", " · ")
    return f"Using {n}"


def transcript_state(home, sid):
    """(what it is doing now if mid-turn, last assistant sentence) from the transcript tail."""
    for p in glob.glob(os.path.join(home, ".claude", "projects", "*", sid + ".jsonl")):
        try:
            st = os.stat(p)
            key = ("state", p)
            if key in _cache and _cache[key][0] == (st.st_mtime, st.st_size):
                return _cache[key][1]
            with open(p, "rb") as f:
                f.seek(max(0, st.st_size - 300_000))
                lines = f.read().decode("utf-8", "ignore").splitlines()[1:]
            now_doing = last_text = None
            for ln in reversed(lines):
                if '"type":"assistant"' not in ln and '"type":"user"' not in ln:
                    continue
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                c = (o.get("message") or {}).get("content")
                if o.get("type") == "user":
                    if isinstance(c, list) and any(b.get("type") == "tool_result" for b in c):
                        continue  # tool output, keep looking back for the call
                    break  # a real prompt: nothing newer from the assistant
                for b in reversed(c if isinstance(c, list) else []):
                    if b.get("type") == "tool_use" and not now_doing:
                        now_doing = _tool_line(b)
                    elif b.get("type") == "text" and b.get("text", "").strip() and not last_text:
                        last_text = _clip(b["text"].split("\n\n")[0] if len(b["text"]) > 400 else b["text"])
                if now_doing or last_text:
                    break
            val = (now_doing, last_text)
            _cache[key] = ((st.st_mtime, st.st_size), val)
            return val
        except Exception:
            pass
    return (None, None)


def desktop_summary(home, cli_sid):
    base = os.path.join(home, "Library", "Application Support", "Claude", "claude-code-sessions")
    for p in glob.glob(os.path.join(base, "*", "*", "local_*.json")):
        d = read_json(p) or {}
        if d.get("cliSessionId") == cli_sid:
            return (d.get("postTurnSummary") or {}).get("status_detail")
    return None


_fds = {}  # pid -> (stdin_path, stdout_path)


def std_files(pid):
    """Files behind a process's stdin/stdout (a codex job reads its brief and writes its log)."""
    if pid not in _fds:
        res = {}
        cur = None
        for ln in sh(["lsof", "-a", "-p", str(pid), "-d", "0,1", "-Ffn"]).splitlines():
            if ln.startswith("f"):
                cur = ln[1:]
            elif ln.startswith("n") and cur is not None and ln[1:].startswith("/"):
                res[cur] = ln[1:]
        _fds[pid] = (res.get("0"), res.get("1"))
    return _fds[pid]


def codex_job_line(pid):
    brief, log = std_files(pid)
    task = None
    try:
        if brief:
            with open(brief, errors="ignore") as f:
                for ln in f.read(3000).splitlines():
                    if ln.startswith("#"):
                        task = _clip(re.sub(r"^#+\s*", "", ln).split("—")[-1], 70)
                        break
    except OSError:
        pass
    now = None
    try:
        if log:
            st = os.stat(log)
            key = ("codexlog", log)
            if key in _cache and _cache[key][0] == (st.st_mtime, st.st_size):
                now = _cache[key][1]
            else:
                with open(log, "rb") as f:
                    f.seek(max(0, st.st_size - 150_000))
                    L = f.read().decode("utf-8", "ignore").splitlines()
                ex = max((i for i, x in enumerate(L) if x == "exec"), default=-1)
                cx = max((i for i, x in enumerate(L) if x == "codex"), default=-1)
                done = ex >= 0 and any(re.match(r"\s*(succeeded|exited|failed)", x) for x in L[ex + 1:])
                say = None
                if cx >= 0:
                    para = []
                    for x in L[cx + 1:]:
                        if not x.strip() and para:
                            break
                        if x.strip():
                            para.append(x)
                    say = _clip(" ".join(para), 110)
                cmd = None
                if ex >= 0 and ex + 1 < len(L):
                    cmd = re.sub(r"^/bin/(zsh|bash|sh) -\w+ ['\"]?", "", L[ex + 1])
                    cmd = _clip(cmd.split(" in /")[0], 90)
                now = ("Running: " + cmd) if (ex > cx and not done and cmd) else (say or (cmd and "Last ran: " + cmd))
                _cache[key] = ((st.st_mtime, st.st_size), now)
    except OSError:
        pass
    return " — ".join(x for x in (task, now) if x) or None


def one_liner(r, sf, home):
    """One line on what this agent is doing, from local files only (nothing leaves the machine)."""
    if r["kind"] == "claude" and sf:
        sid = sf.get("sessionId")
        doing, last = transcript_state(home, sid) if sid else (None, None)
        if sf.get("status") == "busy" or r["status"] == "active":
            if doing:
                return doing
        return desktop_summary(home, sid) or last or doing or ("Working…" if sf.get("status") == "busy" else None)
    if r["kind"] == "codex":
        c = r["full_cmd"]
        if "app-server" in c:
            return "Codex app server (background service)"
        if "exec-server" in c:
            return "Codex remote exec server"
        job = re.search(r"-o\s+\S*?/([^/\s]+?)(?:\.out)?\.md", c)
        cdir = re.search(r"\s-C\s+(\S+)", c)
        where = os.path.basename(cdir.group(1) if cdir else (r["cwd"] or ""))
        if " exec " in c and r["user"] == pwd.getpwuid(os.getuid()).pw_name:
            line = codex_job_line(r["pid"])
            if line:
                return line
        if " exec" in c:
            return "Codex job " + (job.group(1) if job else "") + (f" in {where}" if where else "")
    return None


def account_home(user):
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return "/nonexistent"


def etime_secs(s):
    d = 0
    if "-" in s:
        d, s = s.split("-")
        d = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return d * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def cwds(pids):
    out = {}
    if not pids:
        return out
    cur = None
    for line in sh(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))]).splitlines():
        if line.startswith("p"):
            cur = int(line[1:])
        elif line.startswith("n") and cur:
            out[cur] = line[1:]
    return out


ENV_KEYS = ("CLAUDE_CONFIG_DIR", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_USER_EMAIL")


def env_of(pid):
    """Selected env vars of a process (only readable for our own processes)."""
    out = sh(["ps", "eww", "-p", str(pid)])
    return {k: m.group(1) for k in ENV_KEYS if (m := re.search(r"\b%s=(\S+)" % k, out))}


def scan():
    rows = []
    allp = []
    now = time.time()
    for line in sh(["ps", "-axo", "pid=,ppid=,user=,etime=,%cpu=,rss=,time=,command="]).splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid, ppid, user, et, cpu, rss, ct, cmd = parts
        allp.append((int(pid), int(ppid)))
        m = AGENT_RE.match(cmd)
        if not m or any(s in cmd for s in SKIP) or "Helper" in cmd.split(" ")[0]:
            continue
        rows.append(dict(pid=int(pid), ppid=int(ppid), user=user, up=etime_secs(et),
                         cpu=float(cpu), ct=cpu_secs(ct), mem_mb=round(int(rss) / 1024), kind=m.group(1), cmd=cmd))
    wd = cwds([r["pid"] for r in rows])
    pids = {r["pid"] for r in rows}
    for r in rows:
        r["cwd"] = wd.get(r["pid"])
        env = env_of(r["pid"])
        cfg = env.get("CLAUDE_CONFIG_DIR") if r["kind"] == "claude" else None
        r["config_dir"] = cfg
        r["env_owner_pid"] = int(env["CLAUDE_PID"]) if env.get("CLAUDE_PID") else None
        r["env_owner_sid"] = env.get("CLAUDE_CODE_SESSION_ID")
        # A Claude process knows its own login; others fall back to their auth files.
        r["account"] = ((env.get("CLAUDE_CODE_USER_EMAIL") if r["kind"] == "claude" else None)
                        or account_for(r["kind"], r["user"], cfg) or "unknown account")
        r["child"] = False  # set below once ancestry is known
        sf = read_json(os.path.join(account_home(r["user"]), ".claude", "sessions", f"{r['pid']}.json")) if r["kind"] == "claude" else None
        r["session_id"] = (sf or {}).get("sessionId")
        r["entrypoint"] = (sf or {}).get("entrypoint")
        r["title"] = session_title(account_home(r["user"]), sf)
        r["_sf"] = sf
        sf = r["_sf"] or {}
        ep = sf.get("entrypoint") or ""
        r["mode"] = ("sdk" if ep.startswith("sdk") or "claude-agent-sdk" in r["cmd"] else
                     "desktop chat" if r["kind"] == "claude" and sf.get("hostSessionId") else
                     "headless (-p)" if r["kind"] == "claude" and re.search(r"\s(-p|--print)(\s|$)", r["cmd"]) else
                     "cli" if ep == "cli" else
                     "sdk/embedded" if "stream-json" in r["cmd"] else
                     "app-server" if "app-server" in r["cmd"] else
                     "exec-server" if "exec-server" in r["cmd"] else
                     "exec" if " exec" in r["cmd"] else "interactive")
        prev = _prev.get(r["pid"])
        live = prev and (r["ct"] - prev[0]) / max(now - prev[1], 0.1) * 100
        r["cpu"] = round(live, 1) if prev else r["cpu"]
        busy_kids = any(pp == r["pid"] and p not in pids for p, pp in allp)
        age = last_write(r["cwd"]) if r["kind"] == "claude" else None
        r["status"] = ("active" if (live or 0) >= 1 or busy_kids or (age is not None and age < RECENT)
                       else "idle")
        r["full_cmd"] = re.sub(r"^.*?/(claude|codex|gemini|aider)(?= |$)", r"\1", r["cmd"])
        r["cmd"] = re.sub(r"^.*?/(claude|codex|gemini|aider)(?= |$)", r"\1", r["cmd"])[:240]
        if r["_sf"] and r["_sf"].get("status") == "busy":
            r["status"] = "active"
        r["summary"] = one_liner(r, r.pop("_sf"), account_home(r["user"]))
        del r["full_cmd"]
    _termed.intersection_update(pids)
    for r in rows:
        r["stopping"] = r["pid"] in _termed
    for pid in [p for p in _fds if p not in pids]:
        del _fds[pid]
    _prev.clear()
    _prev.update({r["pid"]: (r["ct"], now) for r in rows})
    # Ancestry: nearest agent process up the parent chain owns this one.
    ppid_of = dict(allp)
    by_pid = {r["pid"]: r for r in rows}
    for r in rows:
        p, seen, parent = r["ppid"], set(), None
        while p > 1 and p not in seen:
            if p in by_pid:
                parent = p
                break
            seen.add(p)
            p = ppid_of.get(p, 0)
        if parent is None:
            # Re-parented to launchd (nohup, scripts)? The env inherited from the owning session still names it.
            sid_owner = next((o["pid"] for o in rows if r["env_owner_sid"] and o["session_id"] == r["env_owner_sid"]), None)
            cand = r["env_owner_pid"] if r["env_owner_pid"] in by_pid else sid_owner
            if cand and cand != r["pid"]:
                parent = cand
        r["parent_pid"] = parent
        r["child"] = parent is not None
    for r in rows:  # break any ownership cycle
        seen, x = {r["pid"]}, r
        while x["parent_pid"]:
            if x["parent_pid"] in seen:
                r["parent_pid"] = None
                break
            seen.add(x["parent_pid"])
            x = by_pid[x["parent_pid"]]
    def root(r):
        while r["parent_pid"]:
            r = by_pid[r["parent_pid"]]
        return r
    def depth(r):
        d = 0
        while r["parent_pid"]:
            r, d = by_pid[r["parent_pid"]], d + 1
        return d
    for r in rows:
        r["root_pid"], r["depth"] = root(r)["pid"], depth(r)
    # tree order: each root followed by its descendants, depth-first, oldest first
    kids = {}
    for r in sorted(rows, key=lambda r: -r["up"]):
        kids.setdefault(r["parent_pid"], []).append(r)
    out = []
    def walk(r):
        out.append(r)
        for k in kids.get(r["pid"], []):
            walk(k)
    for r in kids.get(None, []):
        walk(r)
    return out


def demo_agents():
    """Synthetic data for screenshots (python3 server.py --demo)."""
    def a(pid, parent, root, depth, acct, kind, mode, status, title, summary, cwd, up, cpu, mem, sid=None, ep=None, user="alex", cmd=None):
        return dict(pid=pid, ppid=parent or 1, user=user, up=up, cpu=cpu, ct=0, mem_mb=mem, kind=kind, cmd=cmd or kind,
                    cwd=cwd, config_dir=None, account=acct, child=bool(parent), mode=mode, status=status,
                    session_id=sid, entrypoint=ep, title=title, summary=summary, parent_pid=parent,
                    root_pid=root, depth=depth)
    w, g = "alex@acme.dev", "alex.dev@gmail.com"
    return [
        a(4101, None, 4101, 0, w, "claude", "desktop chat", "active", "Checkout API refactor", "Running: npm test -- orders", "/Users/alex/code/shop-api", 9300, 6.2, 612, "9f1c2ab4", "claude-desktop", cmd="claude --output-format stream-json --verbose"),
        a(4188, 4101, 4101, 1, w, "claude", "headless (-p)", "active", "orders-migration-3f", "Editing 0042_add_order_status.sql", "/Users/alex/code/shop-api/.claude/worktrees/orders", 2100, 3.1, 340, "c77d90e1", "claude-desktop", cmd="claude -p --model sonnet --output-format stream-json"),
        a(4230, 4101, 4101, 1, "alex@acme.dev", "codex", "exec", "active", None, "Review the orders migration — Running: rg \"order_status\" src", "/Users/alex/code/shop-api", 840, 4.4, 280, cmd="codex exec -s read-only -C /Users/alex/code/shop-api"),
        a(4377, None, 4377, 0, w, "claude", "desktop chat", "idle", "Docs site redesign", "Hero section done; waiting on feedback for the pricing table", "/Users/alex/code/docs-site", 51200, 0.0, 480, "21ab55d0", "claude-desktop", cmd="claude --output-format stream-json --verbose"),
        a(4512, None, 4512, 0, g, "claude", "cli", "active", "Rust CLI: add --json flag", "Searching: fn parse_args", "/Users/alex/side/ripgrep-lite", 1300, 2.0, 310, "5e08fa13", "cli", cmd="claude"),
        a(4560, 4512, 4512, 1, g, "claude", "sdk", "idle", "Benchmark harness", "Benchmarks finished; summarising results", "/Users/alex/side/ripgrep-lite/bench", 600, 0.0, 190, "b2d61c9e", "sdk-ts", cmd="claude --output-format stream-json (claude-agent-sdk)"),
        a(4801, None, 4801, 0, g, "claude", "cli", "idle", "Weekend game jam notes", None, "/Users/alex/side/jam", 86400 * 2 + 3600, 0.0, 250, "0d4e7a22", "cli", cmd="claude"),
        a(5003, None, 5003, 0, "alex@acme.dev", "codex", "app-server", "idle", None, "Codex app server (background service)", "/Users/alex/code/shop-api", 86400 * 5, 0.0, 120, cmd="codex app-server --listen unix://"),
    ]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local_request(self):
        """Reject cross-site and DNS-rebinding requests: Host and Origin must be this server."""
        ok = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in ok and (origin is None or origin.split("://", 1)[-1] in ok)

    def do_POST(self):
        if self.path != "/api/stop":
            return self.send(404, b"not found", "text/plain")
        if DEMO or not self._local_request() or self.headers.get("X-Monitor-Token") != TOKEN:
            return self.send(403, b'{"error":"forbidden"}', "application/json")
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            pid = int(req["pid"])
        except (ValueError, KeyError, TypeError):
            return self.send(400, b'{"error":"bad request"}', "application/json")
        # Only a currently detected agent owned by us may be signalled (guards against PID reuse and arbitrary pids).
        target = next((r for r in scan() if r["pid"] == pid), None)
        if not target or target["user"] != pwd.getpwuid(os.getuid()).pw_name:
            return self.send(404, b'{"error":"not a running agent you own"}', "application/json")
        sig = signal.SIGKILL if pid in _termed else signal.SIGTERM
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return self.send(200, b'{"ok":true,"gone":true}', "application/json")
        except PermissionError:
            return self.send(403, b'{"error":"permission denied"}', "application/json")
        _termed.add(pid)
        self.send(200, json.dumps({"ok": True, "signal": sig.name}).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not self._local_request():
            return self.send(403, b"forbidden", "text/plain")
        if path == "/api/agents":
            body = json.dumps({"ts": time.time(), "host": "macbook" if DEMO else os.uname().nodename,
                               "cores": os.cpu_count(), "me": pwd.getpwuid(os.getuid()).pw_name, "agents": demo_agents() if DEMO else scan()})
            self.send(200, body.encode(), "application/json")
        elif path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(200, f.read().replace(b"__TOKEN__", b"" if DEMO else TOKEN.encode()), "text/html; charset=utf-8")
        else:
            self.send(404, b"not found", "text/plain")


DEMO = False
TOKEN = secrets.token_urlsafe(24)  # per-run secret; only the served page knows it
_termed = set()  # pids already sent SIGTERM; the next stop on them is SIGKILL
PORT = 8787

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="serve synthetic data (for screenshots)")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    port, DEMO = args.port, args.demo
    PORT = port
    print(f"Agent Monitor on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
