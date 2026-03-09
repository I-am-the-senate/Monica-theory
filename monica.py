# by newton
#!/usr/bin/env python3
"""
Monica Theory v6
- 8K context, 2K history budget
- 100-char per-agent persistent memory
- 4 tools: MSG, READ, ADD, MEMORY
- GUI: Output | Memory | Network Graph | Config
- Network Graph: 20 agent circles, animated fading edges on MSG

Run:   python monica.py
Deps:  pip install openai pyyaml
"""

import sys, logging, re, json, time, threading, asyncio, math, queue
from pathlib import Path

# Auto-install missing deps
def _ensure(pkg, import_name=None):
    import importlib, subprocess
    name = import_name or pkg
    try:
        importlib.import_module(name)
    except ImportError:
        print(f"[setup] installing {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

_ensure("pyyaml",  "yaml")
_ensure("openai")

import yaml
from collections import defaultdict, deque
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════
CFG_FILE = Path("monica_config.yaml")

def load_cfg() -> dict:
    if not CFG_FILE.exists():
        sys.exit(f"[ERROR] {CFG_FILE} not found.")
    return yaml.safe_load(CFG_FILE.read_text(encoding="utf-8"))

CFG    = load_cfg()
_api   = CFG["api"]
_net   = CFG["network"]
_ctx   = CFG["context"]
_tools = CFG["tools"]
_files = CFG["files"]
_gui   = CFG["gui"]
_T     = _gui["theme"]

# Only log to GUI queue, never to terminal
log = logging.getLogger("monica")
log.setLevel(logging.INFO)
log.propagate = False   # don't send to root logger / stderr

_log_q = queue.Queue(maxsize=4000)  # debug tab feed
_debug_on = False  # Debug tab logging toggle
_msg_q = queue.Queue(maxsize=8000)  # messages tab feed

class _QH(logging.Handler):
    """Push every log record into _log_q for the GUI debug tab."""
    def emit(self, record):
        try:
            _log_q.put_nowait(f"[{record.levelname[0]}] {self.format(record)}")
        except Exception:
            pass

_qh = _QH()
_qh.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_qh)

MEM_DIR = Path(_files["mem_dir"])
MEM_DIR.mkdir(exist_ok=True)

def uid(n: int) -> str:
    return str(n)
def _id_range() -> str:
    b = _net["bits"]
    return f"{uid(1)}–{uid(_net['num_agents'])}"
def _compute_neighbors(agent_id: int) -> list:
    """
    Return neighbours: near ring first, then far long-range.
    near: ±1 sequential (left + right)
    far:  hash-based long-range links (fewer, lower priority)
    """
    n    = _net["num_agents"]
    near = _net.get("neighbors_near", 1)
    far  = _net.get("neighbors_far",  1)
    near_nbrs = []
    for d in range(1, near + 1):
        near_nbrs.append((agent_id - d - 1) % n + 1)
        near_nbrs.append((agent_id + d - 1) % n + 1)
    far_set = set()
    for i in range(far):
        h = int((agent_id * 2654435761 + i * 40503) % n) + 1
        if h != agent_id and h not in near_nbrs:
            far_set.add(h)
    # near first (high priority), far last (low priority)
    return [x for x in near_nbrs if x != agent_id] + sorted(far_set)

def _neighbors_str(agent_id: int) -> str:
    nbrs = _compute_neighbors(agent_id)
    parts = []
    for nid in nbrs:
        parts.append(f"{uid(nid)} (#{nid})")
    return "  ".join(parts)

def _build_tool_block() -> str:
    lines = []
    for key in ("msg","read","add","memory"):
        t = _tools.get(key,{})
        if t.get("enabled"):
            lines.append(f"{key.upper():<7} {t['tag_open']}…{t['tag_close']}  {t['description'].strip()}")
    return "\n".join(lines)

TOOL_BLOCK = _build_tool_block()

# ═══════════════════════════════════════════════════════════════════
#  SHARED STATE
# ═══════════════════════════════════════════════════════════════════
_lock         = threading.Lock()
_active_agents: set[str] = set()  # agents with a running task
_idle_sem    = None
_idle_client = None
_inbox_evt: dict[str, asyncio.Event] = {}  # set when inbox gets a message
_log_q = queue.Queue(maxsize=4000)  # GUI debug tab feed
_msg_q = queue.Queue(maxsize=8000)  # GUI messages tab feed

SHARED = {
    "input":  (Path(_files["input"]).read_text(encoding="utf-8")
               if Path(_files["input"]).exists()
               else "你好啊"),
    "output":  "",
    "running": False,
    "cfg":     {"endpoint":_api["endpoint"],"api_key":_api["api_key"],"model":_api["model"]},
    "errors":  [],   # list of (timestamp, agent_id, round, msg)
    "stats":   {"done":0,"total":0,"msgs":0,"chars":0,"mem_writes":0,
               "rework_attempts":0,"rework_success":0,"parse_calls":0,"hb_ticks":0,"last_msg_ts":0.0},
    # edge events for network graph: list of (src_int, dst_int, timestamp)
    "edges":   [],   # (src_int, dst_int, timestamp)
    "flashes": [],   # agent_int that just produced ADD output
}

def sg(k):
    with _lock: return SHARED[k]
def ss(k,v):
    with _lock: SHARED[k]=v
def sinc(k):
    with _lock: SHARED["stats"][k]+=1
def sappend(ch:str):
    with _lock:
        SHARED["output"]+=ch[:1]
        SHARED["stats"]["chars"]+=1

def push_error(agent_name:str, rnd:int, msg:str):
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        SHARED["errors"].append((ts, agent_name, rnd, str(msg)[:200]))
        if len(SHARED["errors"]) > 300:
            SHARED["errors"] = SHARED["errors"][-300:]

def push_edge(src_int:int, dst_int:int):
    with _lock:
        SHARED["edges"].append((src_int, dst_int, time.time()))
        # keep only last 500 edge events
        if len(SHARED["edges"]) > 500:
            SHARED["edges"] = SHARED["edges"][-500:]

def pop_edges() -> list:
    with _lock:
        e = list(SHARED["edges"])
        SHARED["edges"].clear()
        return e

def push_flash(agent_int: int):
    with _lock:
        SHARED["flashes"].append(agent_int)
        if len(SHARED["flashes"]) > 1000:
            SHARED["flashes"] = SHARED["flashes"][-1000:]

def pop_flashes() -> list:
    with _lock:
        f = list(SHARED["flashes"])
        SHARED["flashes"].clear()
    return f

def read_input()       -> str: return sg("input")[:_tools["read"]["read_input_max"]]
def read_output_tail() -> str: return sg("output")[-_tools["read"]["read_output_tail"]:]
def read_memory(name:str) -> str:
    p = MEM_DIR/f"{name}.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""
def write_memory(name:str, text:str):
    (MEM_DIR/f"{name}.txt").write_text(text[:_ctx["memory_max_chars"]], encoding="utf-8")

# ═══════════════════════════════════════════════════════════════════
#  HISTORY TRUNCATION
# ═══════════════════════════════════════════════════════════════════
def _truncate_history(history:list, budget:int) -> list:
    total=0; keep=[]
    for msg in reversed(history):
        t = len(msg["content"])//4+4
        if total+t > budget: break
        keep.append(msg); total+=t
    return list(reversed(keep))

# ═══════════════════════════════════════════════════════════════════
#  AGENT NETWORK
# ═══════════════════════════════════════════════════════════════════
from openai import AsyncOpenAI

_inboxes:   dict[str,deque] = defaultdict(deque)
_ilock:     dict[str,asyncio.Lock] = defaultdict(asyncio.Lock)
_histories: dict[str,deque] = defaultdict(
    lambda: deque(maxlen=_ctx["history_turns_max"]*2))
_stop_evt: asyncio.Event|None = None

def _sysprompt(name:str, inbox_str:str, agent_id:int) -> str:
    nb_list = _compute_neighbors(agent_id)          # list of ints
    nb_ids  = [uid(i) for i in nb_list]             # binary strings
    nb_str  = " ".join(nb_ids)
    nb_json = ",".join(f'"{x}"' for x in nb_ids)       # neighbors as JSON
    first_nb = nb_ids[0] if nb_ids else "0000000010"
    _comm_mode = _net.get("comm_mode", "prefer_neighbors")
    sender   = inbox_str.split(":")[0].strip() if ":" in inbox_str else ""
    # exclude sender from forward targets
    fwd = [n for n in nb_ids if n != sender] or nb_ids
    fwd_json = '", "'.join(fwd)
    mem_val  = read_memory(name)
    out_tail = read_output_tail()
    inp_val  = read_input()
    _task    = CFG.get("task", "探索涌现意识的最小智能体数量")
    suffix   = ""
    if inp_val:  suffix += f"\n用户输入：{inp_val[:200]}"
    if out_tail: suffix += f"\n当前共享输出：{out_tail}"
    if mem_val:  suffix += f"\n你的记忆：{mem_val}"
    # example targets: sender first (ensures reply), then near-neighbors
    _example_ids = ([sender] if sender and sender != "⚡" and sender != "♡" else []) + \
                   [x for x in nb_ids if x != sender]
    _example_ids = _example_ids or nb_ids
    _ex_json     = ",".join(f'"{x}"' for x in _example_ids)
    if _comm_mode == "all":
        _comm_hint = f"可向任意节点（1-{_net['num_agents']}）自由发消息。"
    elif _comm_mode == "neighbors_only":
        _comm_hint = f"只能向近邻节点（{_ex_json}）发消息。"
    else:  # prefer_neighbors
        _comm_hint = f"优先向近邻（{_ex_json}）发消息，偶尔可联系其他节点。"
    return CFG["system_prompt"].format(
        name=name,
        inbox=inbox_str,
        nb_json=_ex_json,
        comm_hint=_comm_hint,
        num_agents=_net["num_agents"],
        task=_task) + suffix

def _pat(tk_):
    o=re.escape(_tools[tk_]["tag_open"]); c=re.escape(_tools[tk_]["tag_close"])
    return f"{o}(.*?){c}"

async def run_agent(agent_id:int, sem:asyncio.Semaphore, client:AsyncOpenAI) -> None:
    name   = uid(agent_id)

    _histories[name].clear()          # fresh history on network start
    log.debug("Agent %s START", name)
    rnd = 0
    while not (_stop_evt and _stop_evt.is_set()):
        # Wait for inbox message
        evt = _inbox_evt.get(name)
        if evt:
            try: await asyncio.wait_for(evt.wait(), timeout=5.0)
            except asyncio.TimeoutError: continue
            evt.clear()
        async with _ilock[name]:
            if not _inboxes[name]: continue
            msgs = list(_inboxes[name])
            _inboxes[name].clear()

        inbox_str = " | ".join(f"{m['f']}:{m['m']}" for m in msgs[-5:]) or "—"

        sys_p  = _sysprompt(name, inbox_str, agent_id)
        trimmed= _truncate_history(list(_histories[name]), _ctx["history_token_budget"])
        messages=[{"role":"system","content":sys_p}]+trimmed+[{"role":"user","content":"输出工具。"}]

        _prompt_chars = sum(len(m.get("content","")) for m in messages)
        log.debug("Agent %s rnd=%d CALLING API prompt_chars=%d", name, rnd, _prompt_chars)
        _t0 = __import__("time").monotonic()
        try:
            await asyncio.wait_for(sem.acquire(), timeout=15.0)
        except asyncio.TimeoutError:
            log.warning("Agent %s sem-timeout rnd=%d — skipping", name, rnd)
            rnd += 1; continue

        try:
            content = ""
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=sg("cfg")["model"],
                    messages=messages,
                    max_tokens=128,
                    temperature=0.3,
                    top_p=0.9,
                    stream=False,
                ), timeout=30.0)
            content = resp.choices[0].message.content or ""
            _elapsed = __import__("time").monotonic() - _t0
            log.debug("Agent %s rnd=%d API done in %.1fs", name, rnd, _elapsed)
        except asyncio.TimeoutError:
            push_error(name, rnd, "timeout 30s")
            sem.release(); rnd += 1; continue
        except Exception as exc:
            import traceback as _tb
            push_error(name, rnd, exc)
            sem.release(); rnd += 1; continue
        else:
            sem.release()

        sinc("parse_calls")
        log.info("Agent %s rnd=%d RAW[%d]: %s",
                 name, rnd, len(content), content[:300].replace("\n"," "))

        # rework disabled

        _found_tools = any(
            re.search(_pat(tk_), content, re.DOTALL)
            for tk_ in ("msg","read","add","memory")
            if _tools.get(tk_,{}).get("enabled")
        )
        log.debug("Agent %s rnd=%d tools_found=%s", name, rnd, _found_tools)
        if _found_tools:
            _histories[name].append({"role":"user","content":"输出工具。"})
            _histories[name].append({"role":"assistant","content":content[:400]})

        # MSG
        if _tools["msg"]["enabled"]:
            for m in re.finditer(_pat("msg"), content, re.DOTALL):
                try:
                    d=json.loads(m.group(1).strip())
                    txt=str(d.get("m",""))[:_tools["msg"]["max_msg_chars"]]
                    b=_net["bits"]
                    _valid_tgts = []
                    for tgt in d.get("t",[])[:_tools["msg"]["max_targets"]]:
                        tgt = str(tgt)
                        if tgt.isdigit() and 1 <= int(tgt) <= _net["num_agents"] and tgt != name:
                            async with _ilock[tgt]:
                                _deliver(tgt, name, txt, sem, client)
                            sinc("msgs")
                            push_edge(int(name), int(tgt))
                            with _lock: SHARED["stats"]["last_msg_ts"] = __import__("time").monotonic()
                            _valid_tgts.append(int(tgt))
                    if _valid_tgts:
                        import time as _time
                        _src_dec = int(name)
                        _tgt_str = ",".join(str(t) for t in _valid_tgts)
                        try: _msg_q.put_nowait((_time.strftime("%H:%M:%S"), _src_dec, _tgt_str, txt))
                        except Exception: pass
                except (json.JSONDecodeError,KeyError): pass

        # READ — if used, feed result back and re-call API immediately
        if _tools["read"]["enabled"]:
            results=[]
            for m in re.finditer(_pat("read"), content, re.DOTALL):
                try:
                    d=json.loads(m.group(1).strip()); s=d.get("s","")
                    if   s=="in":  results.append(f"[INPUT]: {read_input()}")
                    elif s=="out": results.append(f"[OUTPUT]: {read_output_tail()}")
                    elif s=="mem": results.append(f"[MEMORY]: {read_memory(name)}")
                except (json.JSONDecodeError,KeyError): pass
            if results:
                _read_result = "\n".join(results)
                _histories[name].append({"role":"user",    "content": _read_result})
                _histories[name].append({"role":"assistant","content": content[:400]})
                # Re-call API immediately with read results visible
                _trimmed2 = _truncate_history(list(_histories[name]), _ctx["history_token_budget"])
                _msgs2 = [{"role":"system","content":sys_p}] + _trimmed2 + [{"role":"user","content":"输出工具。"}]
                async with sem:
                    try:
                        _resp2 = await asyncio.wait_for(
                            client.chat.completions.create(
                                model=sg("cfg")["model"], messages=_msgs2,
                                max_tokens=128, temperature=0.3, top_p=0.9, stream=False),
                            timeout=30.0)
                        content = _resp2.choices[0].message.content or content
                        log.info("Agent %s READ→re-call RAW[%d]: %s", name, len(content), content[:200])
                    except Exception as _re: log.warning("Agent %s READ re-call failed: %s", name, _re)

        # ADD
        if _tools["add"]["enabled"]:
            for m in re.finditer(_pat("add"), content, re.DOTALL):
                try:
                    inner = m.group(1).strip()
                    try:
                        d = json.loads(inner); ch = str(d.get("c", ""))
                    except (json.JSONDecodeError, ValueError):
                        ch = inner  # plain char fallback
                    if ch:
                        sappend(ch[0])
                        push_flash(int(name))
                except Exception: pass

        # TAGLESS FALLBACK: model stripped tags but output valid JSON
        if not _found_tools:
            _stripped = content.strip().lstrip("`").rstrip("`").strip()
            # remove markdown code fence if present
            _stripped = re.sub(r"^```[a-z]*\n?", "", _stripped, flags=re.I).rstrip("`").strip()
            try:
                _j = json.loads(_stripped)
                if "t" in _j and "m" in _j:           # MSG-like
                    _synthetic = f'<S>{_stripped}</S>'
                    for _m2 in re.finditer(_pat("msg"), _synthetic, re.DOTALL):
                        _d2 = json.loads(_m2.group(1).strip())
                        _txt2 = str(_d2.get("m",""))[:_tools["msg"]["max_msg_chars"]]
                        for _tgt2 in _d2.get("t",[])[:_tools["msg"]["max_targets"]]:
                            if str(_tgt2).isdigit() and 1 <= int(_tgt2) <= _net["num_agents"] and _tgt2 != name:
                                async with _ilock[_tgt2]:
                                    _deliver(_tgt2, name, _txt2, sem, client)
                                sinc("msgs")
                                push_edge(int(name),int(_tgt2))
                elif "c" in _j:                        # ADD-like
                    _ch = str(_j.get("c",""))
                    if _ch: sappend(_ch[0]); push_flash(int(name))
            except (json.JSONDecodeError, Exception): pass

        # MEMORY WRITE
        if _tools.get("memory",{}).get("enabled"):
            for m in re.finditer(_pat("memory"), content, re.DOTALL):
                try:
                    d=json.loads(m.group(1).strip()); v=str(d.get("v",""))
                    if v: write_memory(name,v); sinc("mem_writes")
                except (json.JSONDecodeError,KeyError): pass


        rnd += 1

def _deliver(tgt: str, sender: str, txt: str,
             sem=None, client=None) -> None:
    """Deposit message and wake the agent via its inbox event."""
    _inboxes[tgt].append({"f": sender, "m": txt})
    evt = _inbox_evt.get(tgt)
    if evt:
        evt.set()

def _spawn_agent(agent_id: int, sem=None, client=None) -> None:
    pass  # agents are always-on; no spawning needed

async def _idle_watcher(sem: asyncio.Semaphore, client: AsyncOpenAI) -> None:
    """Wake agent #1 only after it has been truly silent for timeout_ms."""
    import time as _time
    global _idle_sem, _idle_client
    _idle_sem, _idle_client = sem, client
    with _lock: SHARED["stats"]["last_msg_ts"] = _time.monotonic()
    while not (_stop_evt and _stop_evt.is_set()):
        idle_s = CFG.get("idle_wake", {}).get("timeout_ms", 1000) / 1000.0
        await asyncio.sleep(idle_s)           # sleep the FULL timeout first
        if _stop_evt and _stop_evt.is_set():
            break
        if not CFG.get("idle_wake", {}).get("enabled", True):
            continue
        with _lock:
            last = SHARED["stats"]["last_msg_ts"]
        if _time.monotonic() - last >= idle_s:  # still silent after sleeping?
            _iw_cfg  = CFG.get("idle_wake", {})
            _iw_base = _iw_cfg.get("message", "网络沉默，请发起协作")
            _inp     = read_input()
            _iw_msg  = f"{_iw_base}\n用户输入：{_inp}" if _inp else _iw_base
            _targets = _iw_cfg.get("targets", [1])
            for _tid in _targets:
                _tname = uid(_tid)
                async with _ilock[_tname]:
                    _deliver(_tname, "⚡", _iw_msg, _idle_sem, _idle_client)
            with _lock:
                SHARED["stats"]["hb_ticks"] += 1
                SHARED["stats"]["last_msg_ts"] = _time.monotonic()
            log.info("⚡ IDLE-WAKE #%d → %s (silent≥%.0fms)",
                     SHARED["stats"]["hb_ticks"], _targets, idle_s * 1000)

async def _run_network() -> None:
    global _stop_evt; _stop_evt=asyncio.Event()
    cfg_ = sg("cfg")
    log.info("Network start | model=%s | agents=%d | ctx=%d | concurrent=%d",
             cfg_["model"],_net["num_agents"],_net["ctx_size"],_net["max_concurrent"])

    # ── Connection pre-flight ───────────────────────────────────────────────
    try:
        test_client = AsyncOpenAI(
            base_url=cfg_["endpoint"].rstrip("/")+"/v1",
            api_key =cfg_["api_key"] or "EMPTY",
        )
        test_resp = await asyncio.wait_for(
            test_client.chat.completions.create(
                model=cfg_["model"],
                messages=[{"role":"user","content":"hi"}],
                max_tokens=4,
            ), timeout=10.0
        )
        log.info("Connection OK — model=%s", cfg_["model"])
    except asyncio.TimeoutError:
        push_error("PREFLIGHT", 0, f"Connection timeout (10s) → {cfg_['endpoint']}")
        ss("running", False); return
    except Exception as exc:
        push_error("PREFLIGHT", 0, f"API error: {exc}")
        ss("running", False); return

    with _lock:
        SHARED["stats"]={"done":0,"total":_net["num_agents"],"msgs":0,"chars":0,"mem_writes":0,"rework_attempts":0,"rework_success":0,"parse_calls":0,"hb_ticks":0,"last_msg_ts":0.0}
        SHARED["errors"]=[]
        SHARED["edges"]=[]
        SHARED["flashes"]=[]
    # Create per-agent events; seed agent #1
    for _i in range(1, _net["num_agents"]+1):
        _inbox_evt[uid(_i)] = asyncio.Event()
    _hb_msg = CFG.get("heartbeat",{}).get("message","♡ tick")
    sem=asyncio.Semaphore(_net["max_concurrent"])
    _shared_client = AsyncOpenAI(
        base_url=cfg_["endpoint"].rstrip("/") + "/v1",
        api_key =cfg_["api_key"] or "EMPTY",
    )
    # Launch ALL agents upfront — they wait on inbox events
    agent_tasks = []
    for _ai in range(1, _net["num_agents"]+1):
        agent_tasks.append(asyncio.create_task(run_agent(_ai, sem, _shared_client)))
    # Seed agent 1 with startup message
    async with _ilock[uid(1)]:
        _inboxes[uid(1)].append({"f": "♡", "m": "网络启动，请向邻居发消息"})
        _inbox_evt[uid(1)].set()
    log.info("Network ready — %d agents active", _net["num_agents"])
    idle_task = asyncio.create_task(_idle_watcher(sem, _shared_client))
    await _stop_evt.wait()  # run until Stop button
    for t in agent_tasks: t.cancel()
    idle_task.cancel()
    try: await idle_task
    except asyncio.CancelledError: pass
    ss("running",False)

def start_network():
    if sg("running"): return
    _inboxes.clear(); _histories.clear(); _ilock.clear(); _active_agents.clear()
    MEM_DIR.mkdir(exist_ok=True); ss("running",True)
    def _t():
        try:
            loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            loop.run_until_complete(_run_network()); loop.close()
        except Exception as exc:
            import traceback
            push_error("NETWORK", 0, traceback.format_exc())
            ss("running", False)
            log.error("Network thread crashed: %s", exc)
    threading.Thread(target=_t, daemon=True, name="monica-net").start()

def stop_network():
    if _stop_evt: _stop_evt.set()
    ss("running",False)

# ═══════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════
import tkinter as tk
from tkinter import ttk, scrolledtext

# ── Network Graph Canvas ──────────────────────────────────────────
FADE_DURATION = 0.10  # seconds for edge to disappear (100ms)
NODE_R        = 6     # node circle radius
FONT_ID       = ("Consolas", 5)

class NetworkCanvas(tk.Canvas):
    """Pure-tk renderer: nodes as ovals, edges as create_line. No PIL needed."""
    EDGE_TTL   = 1.2
    POOL_SIZE  = 200
    EDGE_COL   = "#ff79c6"
    FLASH_COL  = "#f9e2af"
    NODE_COL   = "#2a2a6e"
    NODE_RING  = "#4444aa"
    NODE_R     = 6

    def __init__(self, parent, n_agents: int, **kw):
        super().__init__(parent, bg="#0d0d1a", highlightthickness=0, **kw)
        self.n           = n_agents
        self._positions  : dict[int, tuple] = {}
        self._built      = False
        self._edges      : list = []
        self._flashes    : dict = {}
        self._node_items : dict[int, int] = {}   # agent_id → canvas oval id
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, _=None):
        self._built = False
        self.after(80, self._try_build)

    def _try_build(self):
        if self.winfo_width() < 10:
            self.after(150, self._try_build)
            return
        self._build_nodes()

    def _build_nodes(self):
        W, H = self.winfo_width(), self.winfo_height()
        if W < 10 or H < 10:
            self.after(150, self._try_build); return
        self.delete("all")
        self._edges.clear()
        self._positions.clear()
        self._node_items.clear()
        cx, cy  = W / 2, H / 2
        golden  = math.pi * (3 - math.sqrt(5))
        R_max   = min(cx, cy) - self.NODE_R - 4
        r       = self.NODE_R
        for i in range(self.n):
            rad = R_max * math.sqrt((i + 0.5) / self.n)
            t   = i * golden
            x   = cx + rad * math.cos(t)
            y   = cy + rad * math.sin(t)
            self._positions[i + 1] = (x, y)
            oid = self.create_oval(x-r, y-r, x+r, y+r,
                                   fill=self.NODE_COL, outline=self.NODE_RING,
                                   tags="node")
            self._node_items[i + 1] = oid
        self._built = True

    def draw_edge(self, src: int, dst: int):
        if not self._built: return
        self._edges.append((src, dst, time.time()))

    def tick_fade(self):
        if not self._built: return
        now = time.time()
        self.delete("edge")
        self._edges = [(s,d,t) for s,d,t in self._edges if now - t < self.EDGE_TTL]
        for s, d, t in self._edges[-self.POOL_SIZE:]:
            if s in self._positions and d in self._positions:
                x1, y1 = self._positions[s]
                x2, y2 = self._positions[d]
                age     = (now - t) / self.EDGE_TTL
                bright  = int(255 * (1 - age))
                col     = "#%02x%02x%02x" % (bright, int(121 * (1-age*0.5)), min(255, bright))
                self.create_line(x1, y1, x2, y2, fill=col, width=1, tags="edge")
        # raise nodes above edges
        self.tag_raise("node")
        # flashes
        self.delete("flash")
        rf = self.NODE_R + 3
        for nid, exp in list(self._flashes.items()):
            if now > exp:
                del self._flashes[nid]; continue
            if nid in self._positions:
                x, y = self._positions[nid]
                self.create_oval(x-rf, y-rf, x+rf, y+rf,
                                 fill=self.FLASH_COL, outline="", tags="flash")
        self.tag_raise("node")

    def flash_output(self, agent_int: int):
        if not self._built: return
        self._flashes[agent_int] = time.time() + 0.25

class MonicaGUI:
    def __init__(self, root:tk.Tk):
        self.root = root
        root.title("Monica Theory  —  Network Monitor")
        root.configure(bg=_T["bg_primary"])
        root.geometry(_gui["window_size"])
        root.minsize(960,560)
        self._out_snap    = ""
        self._seen_edges  = 0
        self._seen_flashes = 0
        self._setup_styles()
        self._build_toolbar()
        self._build_cfg_bar()
        self._build_main()
        self._build_statusbar()
        self._tick()
        self._sched()
        self._sched_net()
        # Verify log pipeline

    def _setup_styles(self):
        s=ttk.Style(); s.theme_use("clam")
        s.configure("Card.TFrame",  background=_T["bg_secondary"])
        s.configure("H.TLabel",     background=_T["bg_secondary"],
                    foreground=_T["fg_accent"], font=("Consolas",11,"bold"))
        for n,bg in [("Start.TButton",_T["fg_green"]),("Stop.TButton",_T["fg_red"]),
                     ("Clr.TButton","#585b70"),("Rel.TButton","#45475a")]:
            s.configure(n,font=("Consolas",10,"bold"),
                        background=bg,foreground=_T["bg_primary"],relief="flat",padding=4)

    def _build_toolbar(self):
        tb=tk.Frame(self.root,bg=_T["bg_toolbar"],height=44); tb.pack(fill="x")
        tk.Label(tb,text="⬡  MONICA THEORY  v6  —  NETWORK MONITOR",
                 bg=_T["bg_toolbar"],fg=_T["fg_accent"],
                 font=("Consolas",13,"bold")).pack(side="left",padx=14,pady=8)
        tk.Label(tb,text="8K ctx | 2K history | 100ch memory | live graph",
                 bg=_T["bg_toolbar"],fg=_T["fg_muted"],
                 font=("Consolas",9)).pack(side="left",padx=6)
        self.clock=tk.Label(tb,text="",bg=_T["bg_toolbar"],fg=_T["fg_muted"],font=("Consolas",10))
        self.clock.pack(side="right",padx=14)

    def _build_cfg_bar(self):
        cf=tk.Frame(self.root,bg=_T["bg_dark"],pady=6); cf.pack(fill="x")
        def lbl(t): tk.Label(cf,text=t,bg=_T["bg_dark"],fg=_T["fg_muted"],
                              font=("Consolas",9)).pack(side="left",padx=(10,2))
        def ent(w,val):
            e=tk.Entry(cf,width=w,bg=_T["bg_toolbar"],fg=_T["fg_primary"],
                       insertbackground=_T["fg_accent"],font=("Consolas",10),relief="flat")
            e.insert(0,val); e.pack(side="left",padx=(0,6),ipady=3); return e
        lbl("Endpoint:"); self.e_ep =ent(28,sg("cfg")["endpoint"])
        lbl("API Key:");  self.e_key=ent(14,sg("cfg")["api_key"])
        lbl("Model:");    self.e_mod=ent(24,sg("cfg")["model"])
        tk.Label(cf,text="│",bg=_T["bg_dark"],fg=_T["bg_toolbar"],
                 font=("Consolas",14)).pack(side="left",padx=4)
        self.btn_start=ttk.Button(cf,text="▶  Start",style="Start.TButton",command=self._start)
        self.btn_start.pack(side="left",padx=4)
        self.btn_stop=ttk.Button(cf,text="■  Stop",style="Stop.TButton",command=self._stop,state="disabled")
        self.btn_stop.pack(side="left",padx=4)
        ttk.Button(cf,text="↺ Reload",style="Rel.TButton",command=self._reload).pack(side="left",padx=4)
        self.ind=tk.Label(cf,text="○ idle",bg=_T["bg_dark"],fg=_T["fg_muted"],font=("Consolas",9))
        self.ind.pack(side="left",padx=10)
        # Rework rate live display
        tk.Label(cf,text="│",bg=_T["bg_dark"],fg=_T["bg_toolbar"],
                 font=("Consolas",12)).pack(side="right",padx=2)
        self.rw_lbl=tk.Label(cf,text="rework: —",bg=_T["bg_dark"],
                              fg=_T["fg_orange"],font=("Consolas",9))
        self.rw_lbl.pack(side="right",padx=8)
        tk.Label(cf,text="rescued:",bg=_T["bg_dark"],fg=_T["fg_muted"],
                 font=("Consolas",9)).pack(side="right",padx=(8,0))
        self.rescued_lbl=tk.Label(cf,text="—",bg=_T["bg_dark"],
                                   fg=_T["fg_green"],font=("Consolas",9))
        self.rescued_lbl.pack(side="right",padx=(2,4))
        tk.Label(cf,text="│",bg=_T["bg_dark"],fg=_T["bg_toolbar"],
                 font=("Consolas",12)).pack(side="right",padx=2)
        self.hb_lbl=tk.Label(cf,text="♡ off",bg=_T["bg_dark"],
                              fg=_T["fg_muted"],font=("Consolas",9))
        self.hb_lbl.pack(side="right",padx=8)

    def _reload(self):
        global CFG,_api,_net,_ctx,_tools,_files,_gui,_T,TOOL_BLOCK
        CFG=load_cfg(); _api=CFG["api"]; _net=CFG["network"]
        _ctx=CFG["context"]
        _tools=CFG["tools"]; _files=CFG["files"]; _gui=CFG["gui"]; _T=_gui["theme"]
        TOOL_BLOCK=_build_tool_block()
        self.ind.config(text="✔ reloaded",fg=_T["fg_green"])
        self.root.after(2000,lambda:self.ind.config(text="○ idle",fg=_T["fg_muted"]))

    def _start(self):
        with _lock:
            SHARED["cfg"]["endpoint"]=self.e_ep.get().strip()
            SHARED["cfg"]["api_key"] =self.e_key.get().strip()
            SHARED["cfg"]["model"]   =self.e_mod.get().strip()
            SHARED["output"]=""
            SHARED["edges"]=[]
        self.out_box.config(state="normal"); self.out_box.delete("1.0","end")
        self.out_box.config(state="disabled"); self._out_snap=""
        self._seen_edges=0
        self._seen_flashes=0
        self.ind.config(text="⟳ connecting…", fg=_T["fg_blue"])
        self.root.update_idletasks()
        start_network()
        self.btn_start.config(state="disabled"); self.btn_stop.config(state="normal")
        # poll until running confirmed or error appears (max 12s)
        self._check_started(attempts=0)

    def _check_started(self, attempts:int):
        if sg("running"):
            self.ind.config(text="● running", fg=_T["fg_green"])
            return
        with _lock: errs = list(SHARED["errors"])
        if errs:
            # startup failed — re-enable Start, show in Errors tab
            self.btn_start.config(state="normal"); self.btn_stop.config(state="disabled")
            self.ind.config(text="✗ failed", fg=_T["fg_red"])
            self._refresh_errors()
            return
        if attempts > 24:   # 12s timeout
            self.btn_start.config(state="normal"); self.btn_stop.config(state="disabled")
            self.ind.config(text="✗ timeout", fg=_T["fg_red"])
            push_error("GUI", 0, "Start timed out — check endpoint and model name")
            self._refresh_errors()
            return
        self.root.after(500, lambda: self._check_started(attempts+1))

    def _stop(self):
        stop_network()
        self.btn_start.config(state="normal"); self.btn_stop.config(state="disabled")
        self.ind.config(text="○ stopped",fg=_T["fg_red"])

    def _build_main(self):
        pw=ttk.PanedWindow(self.root,orient="horizontal")
        pw.pack(fill="both",expand=True,padx=8,pady=6)

        # ── LEFT: user input ─────────────────────────────────────
        lf=ttk.Frame(pw,style="Card.TFrame"); pw.add(lf,weight=1)
        ttk.Label(lf,text="▶  USER INPUT",style="H.TLabel").pack(anchor="w",padx=10,pady=(10,0))
        tk.Label(lf,text="live — agents read via READ tool",
                 bg=_T["bg_secondary"],fg=_T["fg_muted"],font=("Consolas",8)).pack(anchor="w",padx=10)
        self.inp=scrolledtext.ScrolledText(
            lf,wrap="word",bg=_T["bg_dark"],fg=_T["fg_primary"],
            insertbackground=_T["fg_accent"],font=("Consolas",11),relief="flat",pady=6,padx=6)
        self.inp.pack(fill="both",expand=True,padx=8,pady=6)
        self.inp.insert("1.0",sg("input"))
        self.inp.bind("<KeyRelease>",self._inp_change)
        br=tk.Frame(lf,bg=_T["bg_secondary"]); br.pack(fill="x",padx=8,pady=(0,4))
        ttk.Button(br,text="✘ Clear",style="Clr.TButton",
                   command=lambda:self.inp.delete("1.0","end")).pack(side="left")
        self.inp_cc=tk.Label(br,text="0 ch",bg=_T["bg_secondary"],fg=_T["fg_muted"],font=("Consolas",8))
        self.inp_cc.pack(side="right")

        # ── RIGHT: notebook ──────────────────────────────────────
        rf=ttk.Frame(pw,style="Card.TFrame"); pw.add(rf,weight=3)
        nb=ttk.Notebook(rf); nb.pack(fill="both",expand=True,padx=8,pady=8)

        # Tab 1: Output
        ot=tk.Frame(nb,bg=_T["bg_dark"]); nb.add(ot,text="  ◉ Shared Output  ")
        self.out_box=scrolledtext.ScrolledText(
            ot,wrap="word",bg=_T["bg_dark"],fg=_T["fg_green"],
            font=("Consolas",11),relief="flat",state="disabled",pady=6,padx=6)
        self.out_box.pack(fill="both",expand=True)
        bot=tk.Frame(ot,bg=_T["bg_dark"]); bot.pack(fill="x",pady=4)
        self.out_cc=tk.Label(bot,text="0 chars",bg=_T["bg_dark"],fg=_T["fg_orange"],font=("Consolas",9))
        self.out_cc.pack(side="left",padx=10)
        ttk.Button(bot,text="✘ Clear",style="Clr.TButton",command=self._clear_out).pack(side="right",padx=8)

        # Tab 2: Network Graph ◄────── NEW ──────────────────────
        gt=tk.Frame(nb,bg="#0d0d1a"); nb.add(gt,text="  🕸 Network Graph  ")
        # legend bar
        lgd=tk.Frame(gt,bg="#0d0d1a"); lgd.pack(fill="x",padx=8,pady=(6,0))
        for col,txt in [("#ff79c6","sender"),("#50fa7b","receiver"),("#4444aa","idle")]:
            tk.Label(lgd,text="●",bg="#0d0d1a",fg=col,font=("Consolas",12)).pack(side="left",padx=(6,2))
            tk.Label(lgd,text=txt,bg="#0d0d1a",fg="#6c7086",font=("Consolas",9)).pack(side="left",padx=(0,10))
        self.edge_count_lbl=tk.Label(lgd,text="edges: 0",bg="#0d0d1a",fg="#6c7086",font=("Consolas",9))
        self.edge_count_lbl.pack(side="right",padx=10)

        self.net_canvas=NetworkCanvas(gt, _net["num_agents"])
        self.net_canvas.pack(fill="both",expand=True,padx=4,pady=4)
        # Rebuild when tab becomes visible (Map = canvas remapped to screen)
        self.net_canvas.bind("<Map>", lambda e: self.root.after(80, self.net_canvas._build_nodes))
        self.root.after(200, self.net_canvas._build_nodes)
        # Also trigger on notebook tab switch
        nb.bind("<<NotebookTabChanged>>",
                lambda e: self.root.after(80, self.net_canvas._build_nodes)
                if nb.index("current") == 1 else None)

        # Tab 3: Memory browser — list + hover preview
        mt=tk.Frame(nb,bg=_T["bg_dark"]); nb.add(mt,text="  🧠 Agent Memory  ")
        # header
        mh=tk.Frame(mt,bg=_T["bg_dark"]); mh.pack(fill="x",padx=8,pady=(6,2))
        tk.Label(mh,text="Agents with memory",bg=_T["bg_dark"],fg=_T["fg_muted"],
                 font=("Consolas",9)).pack(side="left")
        self.mw_lbl=tk.Label(mh,text="mem_writes: 0",bg=_T["bg_dark"],fg=_T["fg_orange"],
                              font=("Consolas",9))
        self.mw_lbl.pack(side="right",padx=10)
        # split: left list | right preview
        ms=tk.Frame(mt,bg=_T["bg_dark"]); ms.pack(fill="both",expand=True,padx=8,pady=4)
        ms.columnconfigure(1,weight=1)
        ms.rowconfigure(0,weight=1)
        # left: scrollable listbox of agent IDs
        lf=tk.Frame(ms,bg=_T["bg_secondary"]); lf.grid(row=0,column=0,sticky="ns",padx=(0,6))
        sb=tk.Scrollbar(lf,orient="vertical",bg=_T["bg_toolbar"]); sb.pack(side="right",fill="y")
        self.mem_list=tk.Listbox(lf,width=10,bg=_T["bg_secondary"],fg=_T["fg_primary"],
                                  selectbackground=_T["fg_accent"],selectforeground=_T["bg_dark"],
                                  font=("Consolas",11),relief="flat",activestyle="none",
                                  yscrollcommand=sb.set,exportselection=False,
                                  highlightthickness=0)
        self.mem_list.pack(side="left",fill="both",expand=True)
        sb.config(command=self.mem_list.yview)
        # right: preview pane
        self.mem_box=scrolledtext.ScrolledText(
            ms,wrap="word",bg=_T["bg_dark"],fg=_T["fg_accent"],
            font=("Consolas",11),relief="flat",state="disabled",pady=6,padx=8)
        self.mem_box.grid(row=0,column=1,sticky="nsew")
        # hover binding
        self.mem_list.bind("<Motion>",  self._mem_hover)
        self.mem_list.bind("<<ListboxSelect>>", self._mem_hover)
        self._mem_last_hover = -1

        # Tab 4: Errors ◄── NEW
        et=tk.Frame(nb,bg=_T["bg_dark"]); nb.add(et,text="  ⚠ Errors  ")
        eh=tk.Frame(et,bg=_T["bg_dark"]); eh.pack(fill="x",padx=8,pady=(8,2))
        self.err_count_lbl=tk.Label(eh,text="errors: 0",bg=_T["bg_dark"],
                                     fg=_T["fg_red"],font=("Consolas",10,"bold"))
        self.err_count_lbl.pack(side="left")
        ttk.Button(eh,text="✘ Clear errors",style="Clr.TButton",
                   command=self._clear_errors).pack(side="right",padx=4)
        self.err_box=scrolledtext.ScrolledText(
            et,wrap="word",bg="#11111b",fg=_T["fg_red"],
            font=("Consolas",10),relief="flat",state="disabled",pady=6,padx=6)
        self.err_box.pack(fill="both",expand=True,padx=8,pady=(0,8))
        self.err_box.tag_configure("ts",    foreground="#6c7086")
        self.err_box.tag_configure("agent", foreground="#fab387")
        self.err_box.tag_configure("msg",   foreground="#f38ba8")
        self._err_snapshot = 0

        # Tab 5: Debug log ◄── NEW
        dt=tk.Frame(nb,bg="#0d0d0d"); nb.add(dt,text="  🐛 Debug  ")
        self.dbg_box=scrolledtext.ScrolledText(
            dt,wrap="none",bg="#0d0d0d",fg="#a6e3a1",
            font=("Consolas",9),relief="flat",state="disabled")
        self.dbg_box.pack(fill="both",expand=True)
        self.dbg_box.tag_config("W", foreground="#f9e2af")
        self.dbg_box.tag_config("E", foreground="#f38ba8")
        self.dbg_box.tag_config("I", foreground="#89dceb")
        self.dbg_box.tag_config("D", foreground="#a6e3a1")
        db_bar=tk.Frame(dt,bg="#0d0d0d"); db_bar.pack(fill="x",pady=2)
        ttk.Button(db_bar,text="✘ Clear log",style="Clr.TButton",
                   command=self._dbg_clear).pack(side="left",padx=4)
        ttk.Button(db_bar,text="⎘ Copy all",style="Rel.TButton",
                   command=self._dbg_copy).pack(side="left",padx=4)
        self._dbg_lines = 0

        # Tab 6: Messages feed ◄── NEW
        mf=tk.Frame(nb,bg="#0d0d0d"); nb.add(mf,text="  💬 Messages  ")
        self.msg_box=scrolledtext.ScrolledText(
            mf,wrap="word",bg="#0d0d0d",fg="#cdd6f4",
            font=("Consolas",10),relief="flat",state="disabled")
        self.msg_box.pack(fill="both",expand=True)
        self.msg_box.tag_config("src",  foreground="#89b4fa")   # blue  = sender
        self.msg_box.tag_config("arr",  foreground="#585b70")   # grey  = arrow
        self.msg_box.tag_config("tgt",  foreground="#a6e3a1")   # green = receiver
        self.msg_box.tag_config("txt",  foreground="#cdd6f4")   # white = content
        self.msg_box.tag_config("ts",   foreground="#45475a")   # dark  = timestamp
        mf_bar=tk.Frame(mf,bg="#0d0d0d"); mf_bar.pack(fill="x",pady=2)
        ttk.Button(mf_bar,text="✘ Clear",style="Clr.TButton",
                   command=self._msg_clear).pack(side="left",padx=4)
        ttk.Button(mf_bar,text="⎘ Copy all",style="Rel.TButton",
                   command=self._msg_copy).pack(side="left",padx=4)
        self._msg_lines = 0

        # Tab 5: Config
        ct=tk.Frame(nb,bg=_T["bg_dark"]); nb.add(ct,text="  ⚙ Config  ")
        self.cfg_box=scrolledtext.ScrolledText(
            ct,wrap="none",bg=_T["bg_dark"],fg="#89dceb",
            font=("Consolas",9),relief="flat",pady=6,padx=6)
        self.cfg_box.pack(fill="both",expand=True)
        self.cfg_box.insert("1.0",CFG_FILE.read_text(encoding="utf-8") if CFG_FILE.exists() else "")
        cfg_bar=tk.Frame(ct,bg=_T["bg_dark"]); cfg_bar.pack(fill="x",pady=2)
        ttk.Button(cfg_bar,text="💾 Save & Reload",style="Start.TButton",
                   command=self._cfg_save).pack(side="left",padx=4)
        self.cfg_status=tk.Label(cfg_bar,text="",bg=_T["bg_dark"],
                                  fg=_T["fg_green"],font=("Consolas",9))
        self.cfg_status.pack(side="left",padx=8)
        # Idle-wake quick control
        tk.Label(cfg_bar,text="Idle wake ms:",bg=_T["bg_dark"],fg=_T["fg_muted"],
                 font=("Consolas",9)).pack(side="right",padx=(8,2))
        self.e_idle=tk.Entry(cfg_bar,width=6,bg=_T["bg_toolbar"],fg=_T["fg_primary"],
                             font=("Consolas",10),relief="flat")
        self.e_idle.insert(0,str(CFG.get("idle_wake",{}).get("timeout_ms",100)))
        self.e_idle.pack(side="right",padx=(0,4))
        ttk.Button(cfg_bar,text="Apply",style="Rel.TButton",
                   command=self._apply_idle).pack(side="right",padx=2)
        # ── Row 2: network topology controls ──────────────────────────
        net_bar=tk.Frame(ct,bg=_T["bg_dark"]); net_bar.pack(fill="x",pady=2)
        def _nlbl(t): tk.Label(net_bar,text=t,bg=_T["bg_dark"],fg=_T["fg_muted"],
                               font=("Consolas",9)).pack(side="left",padx=(10,2))
        def _nent(w,val):
            e=tk.Entry(net_bar,width=w,bg=_T["bg_toolbar"],fg=_T["fg_primary"],
                       insertbackground=_T["fg_accent"],font=("Consolas",10),relief="flat")
            e.insert(0,str(val)); e.pack(side="left",padx=(0,6),ipady=2); return e
        _nlbl("Agents:"); self.e_agents=_nent(5,_net["num_agents"])
        _nlbl("Concurrent:"); self.e_concurrent=_nent(5,_net["max_concurrent"])
        tk.Label(net_bar,text="│",bg=_T["bg_dark"],fg=_T["bg_toolbar"],
                 font=("Consolas",12)).pack(side="left",padx=6)
        tk.Label(net_bar,text="Comm:",bg=_T["bg_dark"],fg=_T["fg_muted"],
                 font=("Consolas",9)).pack(side="left",padx=(0,4))
        self._comm_var=tk.StringVar(value=_net.get("comm_mode","prefer_neighbors"))
        for _ml,_mv in [("🌐 全网","all"),("⭕ 仅近邻","neighbors_only"),("⭐ 优先近邻","prefer_neighbors")]:
            tk.Radiobutton(net_bar,text=_ml,variable=self._comm_var,value=_mv,
                           bg=_T["bg_dark"],fg=_T["fg_primary"],selectcolor=_T["bg_toolbar"],
                           activebackground=_T["bg_dark"],font=("Consolas",9),
                           command=self._apply_net).pack(side="left",padx=3)
        ttk.Button(net_bar,text="Apply",style="Rel.TButton",
                   command=self._apply_net).pack(side="left",padx=8)

    def _mem_hover(self, event=None):
        idx = self.mem_list.nearest(event.y) if event and hasattr(event,"y") else self.mem_list.curselection()
        if isinstance(idx, tuple): idx = idx[0] if idx else -1
        if idx < 0 or idx == self._mem_last_hover: return
        self._mem_last_hover = idx
        try:
            agent_id = self.mem_list.get(idx)
            txt = read_memory(str(agent_id)) or "(no memory)"
            self.mem_box.config(state="normal")
            self.mem_box.delete("1.0","end")
            self.mem_box.insert("1.0", f"Agent {agent_id}\n{'─'*30}\n{txt}")
            self.mem_box.config(state="disabled")
        except Exception: pass

    def _refresh_mem_list(self):
        """Rebuild list of agents that have memory files."""
        import os
        cur_sel = self.mem_list.curselection()
        cur_item = self.mem_list.get(cur_sel[0]) if cur_sel else None
        self.mem_list.delete(0,"end")
        try:
            files = sorted(MEM_DIR.glob("*.txt"), key=lambda f: int(f.stem))
            for f in files:
                self.mem_list.insert("end", f.stem)
            # restore selection
            if cur_item:
                items = list(self.mem_list.get(0,"end"))
                if cur_item in items:
                    i = items.index(cur_item)
                    self.mem_list.selection_set(i)
        except Exception: pass

    def _load_mem(self):   # legacy shim — kept so nothing breaks
        pass

    def _clear_errors(self):
        with _lock: SHARED["errors"]=[]
        self._err_snapshot=0
        self.err_box.config(state="normal"); self.err_box.delete("1.0","end")
        self.err_box.config(state="disabled")
        self.err_count_lbl.config(text="errors: 0")

    def _refresh_errors(self):
        with _lock: errors=list(SHARED["errors"])
        new_errs = errors[self._err_snapshot:]
        if not new_errs: return
        self._err_snapshot = len(errors)
        self.err_box.config(state="normal")
        for ts, agent, rnd, msg in new_errs:
            self.err_box.insert("end", f"[{ts}] ", "ts")
            self.err_box.insert("end", f"agent {agent} r{rnd}  ", "agent")
            self.err_box.insert("end", f"{msg}\n", "msg")
        self.err_box.see("end")
        self.err_box.config(state="disabled")
        n = len(errors)
        self.err_count_lbl.config(
            text=f"errors: {n}",
            fg=_T["fg_red"] if n>0 else _T["fg_muted"]
        )

    def _build_statusbar(self):
        sb=tk.Frame(self.root,bg="#11111b"); sb.pack(fill="x",side="bottom")
        self.stat=tk.Label(sb,text="Idle.",bg="#11111b",fg=_T["fg_muted"],
                            font=("Consolas",9),anchor="w")
        self.stat.pack(side="left",padx=10,pady=4)
        self.ref=tk.Label(sb,text="",bg="#11111b",fg="#585b70",font=("Consolas",9))
        self.ref.pack(side="right",padx=10)

    def _inp_change(self,_=None):
        t=self.inp.get("1.0","end-1c"); ss("input",t)
        self.inp_cc.config(text=f"{len(t)} ch")

    def _clear_out(self):
        with _lock: SHARED["output"]=""
        self._out_snap=""
        self.out_box.config(state="normal"); self.out_box.delete("1.0","end")
        self.out_box.config(state="disabled")

    # ── Main refresh (output + stats) every REFRESH_MS ────────────
    def _refresh(self):
        out=sg("output"); st=sg("stats")
        if out!=self._out_snap:
            new=out[len(self._out_snap):]; self._out_snap=out
            self.out_box.config(state="normal")
            self.out_box.insert("end",new); self.out_box.see("end")
            self.out_box.config(state="disabled")
            self.out_cc.config(text=f"{len(out)} chars")
        if not sg("running") and self.btn_stop["state"]=="normal": self._stop()
        self.mw_lbl.config(text=f"mem_writes: {st.get('mem_writes',0)}")
        self._refresh_mem_list()
        calls    = st.get("parse_calls", 0)
        tool_hit = st.get("msgs", 0) + st.get("chars", 0)
        hit_pct  = f"{tool_hit/calls*100:.1f}%" if calls > 0 else "—"
        self.rw_lbl.config(
            text=f"tool_hit: {hit_pct}",
            fg=_T["fg_green"] if calls > 0 and tool_hit/calls > 0.5 else _T["fg_orange"]
        )
        self.rescued_lbl.config(text=f"{st.get('msgs',0)} msgs")
        hb = st.get("hb_ticks", 0)
        self.hb_lbl.config(text=f"⚡ {hb} wakes", fg=_T["fg_blue"])
        self.stat.config(text=(
            f"agents: {st.get('done',0)}/{st.get('total',0)}  |  "
            f"msgs: {st.get('msgs',0)}  |  chars: {st.get('chars',0)}  |  "
            f"idle-wake: {hb}  |  tool_hit: {hit_pct}"
        ))
        self.ref.config(text=f"refreshed {datetime.now().strftime('%H:%M:%S')}")
        self._refresh_errors()

    def _sched(self):
        try:
            self._refresh()
            self._drain_log_q()
            self._drain_msg_q()
        except Exception as _e:
            import traceback
            print("_sched ERROR:", traceback.format_exc())
        finally:
            self.root.after(_gui["refresh_ms"], self._sched)

    # ── Network graph refresh every 50ms ──────────────────────────
    def _refresh_net(self):
        # Draw new MSG edges
        new_edges = pop_edges()
        self._seen_edges += len(new_edges)
        for src, dst, _ in new_edges:
            self.net_canvas.draw_edge(src, dst)
        self.net_canvas.tick_fade()
        self.edge_count_lbl.config(text=f"edges drawn: {self._seen_edges}")
        # Flash ADD output nodes
        for agent_int in pop_flashes():
            self.net_canvas.flash_output(agent_int)

    def _dbg_clear(self):
        self.dbg_box.config(state="normal")
        self.dbg_box.delete("1.0","end")
        self.dbg_box.config(state="disabled")
        self._dbg_lines = 0

    def _dbg_copy(self):
        txt = self.dbg_box.get("1.0","end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)

    def _cfg_save(self):
        txt = self.cfg_box.get("1.0","end-1c")
        try:
            import yaml as _yaml
            _yaml.safe_load(txt)           # validate before saving
            CFG_FILE.write_text(txt, encoding="utf-8")
            global CFG,_api,_net,_ctx,_tools,_files,_gui,_T
            CFG=load_cfg(); _api=CFG["api"]; _net=CFG["network"]
            _ctx=CFG["context"]; _tools=CFG["tools"]
            _files=CFG["files"]; _gui=CFG["gui"]; _T=_gui["theme"]
            self.cfg_status.config(text="✅ saved & reloaded", fg=_T["fg_green"])
        except Exception as e:
            self.cfg_status.config(text=f"❌ {e}", fg=_T["fg_red"])

    def _apply_idle(self):
        try:
            ms = int(self.e_idle.get())
            CFG.setdefault("idle_wake",{})["timeout_ms"] = ms
            self.cfg_status.config(
                text=f"⚡ idle_wake = {ms}ms (live)", fg=_T["fg_blue"])
        except ValueError:
            self.cfg_status.config(text="❌ enter integer ms", fg=_T["fg_red"])

    def _apply_net(self):
        global CFG, _net
        import yaml as _y
        try:
            n  = int(self.e_agents.get())
            c  = int(self.e_concurrent.get())
            cm = self._comm_var.get()
            CFG["network"]["num_agents"]     = n
            CFG["network"]["max_concurrent"] = c
            CFG["network"]["comm_mode"]      = cm
            _net = CFG["network"]
            Path("monica_config.yaml").write_text(
                _y.dump(CFG, allow_unicode=True, sort_keys=False), encoding="utf-8")
            self.cfg_status.config(
                text=f"✔ agents={n}  concurrent={c}  mode={cm}", fg=_T["fg_green"])
        except ValueError:
            self.cfg_status.config(text="❌ invalid value", fg=_T["fg_red"])

    def _toggle_debug(self):
        global _debug_on
        _debug_on = not _debug_on
        if _debug_on:
            self.dbg_toggle_btn.config(text="⏹ 关闭 Debug", style="Stop.TButton")
        else:
            self.dbg_toggle_btn.config(text="▶ 开启 Debug", style="Start.TButton")

    def _msg_clear(self):
        self.msg_box.config(state="normal")
        self.msg_box.delete("1.0","end")
        self.msg_box.config(state="disabled")
        self._msg_lines = 0

    def _msg_copy(self):
        txt = self.msg_box.get("1.0","end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)

    def _drain_msg_q(self):
        added = 0
        self.msg_box.config(state="normal")
        while added < 80:
            try:
                ts, src, tgt, txt = _msg_q.get_nowait()
            except Exception:
                break
            self.msg_box.insert("end", f"{ts} ", "ts")
            self.msg_box.insert("end", str(src),  "src")
            self.msg_box.insert("end", " → ",     "arr")
            self.msg_box.insert("end", str(tgt),  "tgt")
            self.msg_box.insert("end", f"  {txt}\n", "txt")
            self._msg_lines += 1
            added += 1
        if added:
            if self._msg_lines > 3000:
                self.msg_box.delete("1.0", f"{self._msg_lines-3000}.0")
                self._msg_lines = 3000
            self.msg_box.see("end")
        self.msg_box.config(state="disabled")

    def _drain_log_q(self):
        """Pull up to 50 records per tick from _log_q into the debug box."""
        added = 0
        self.dbg_box.config(state="normal")
        while added < 50:
            try:
                line = _log_q.get_nowait()
            except Exception:
                break
            lvl = line[1] if len(line) > 1 else "D"
            tag = lvl if lvl in ("W","E","I","D") else "D"
            self.dbg_box.insert("end", line+"\n", tag)
            self._dbg_lines += 1
            added += 1
        if added:
            # keep at most 2000 lines
            if self._dbg_lines > 2000:
                self.dbg_box.delete("1.0", f"{self._dbg_lines-2000}.0")
                self._dbg_lines = 2000
            self.dbg_box.see("end")
        self.dbg_box.config(state="disabled")

    def _sched_net(self):
        try:
            self._refresh_net()
        except Exception as _e:
            print("_sched_net ERROR:", _e)
        finally:
            self.root.after(50, self._sched_net)

    def _tick(self):
        self.clock.config(text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self.root.after(1000,self._tick)


if __name__ == "__main__":
    root = tk.Tk()
    MonicaGUI(root)
    root.mainloop()