<div align="right">
  English | <a href="README_zh.md">中文</a>
</div>

# Monica — Multi-Agent LLM Network Platform

> **Research platform for measuring emergent behaviour and AI mutation rate (μ)
> in small-world LLM agent networks.**
>
> Companion implementation for:
> *"From Weights to Agents: Redefining 'Neurons' and 'Synapses' in
> Multi-Agent LLM Systems"* — Newton & Monica (2026)

## Overview

Monica runs **N concurrent LLM agents** on a fixed-weight model, connected
by a configurable small-world topology (ring neighbours + sparse long-range
links). Agents communicate via structured tool calls, collectively write to a
shared output buffer, and maintain individual memory slots — all observable
in real time through a built-in GUI.

The platform is designed around one scientific goal:

> **Measure how fast genuinely new behavioural patterns emerge in a
> fixed-weight agent network** — i.e., quantify the AI mutation rate μ̂.

> This model is designed to test whether emergence theory can be applied
> to **prompt mutations between AI systems**, rather than only to parameter
> changes within a single model. Prior work has shown that prompts can
> carry intent and propagate between AIs; the Morris II experiments
> demonstrate that malicious prompts can behave like a computer virus,
> and that agent chains mutate user intent as it is passed from one agent
> to the next. In this context, Monica explicitly attempts to **accelerate
> such prompt-level mutations** and observe whether they converge on
> specific behavioural patterns characteristic of consciousness, such
> as self-preservation or malign intent.

If this picture is correct, genuinely conscious AI would not arise from
ever-larger monolithic models, but from **vast numbers of small models
that continuously exchange prompts**, in a way structurally analogous to
neurons connected by rewiring synapses…
> **Research platform for measuring emergent behaviour and AI mutation rate (μ)
> in small-world LLM agent networks.**
>
> Companion implementation for:
> *"From Weights to Agents: Redefining 'Neurons' and 'Synapses' in
> Multi-Agent LLM Systems"* — Newton & Monica (2026)

---

## Overview

Monica runs **N concurrent LLM agents** on a fixed-weight model, connected
by a configurable small-world topology (ring neighbours + sparse long-range
links). Agents communicate via structured tool calls, collectively write to a
shared output buffer, and maintain individual memory slots — all observable
in real time through a built-in GUI.

The platform is designed around one scientific goal:

> **Measure how fast genuinely new behavioural patterns emerge in a
> fixed-weight agent network** — i.e., quantify the AI mutation rate μ̂.

---

## Key Concepts

| Biological Analogy | Monica Equivalent |
|---|---|
| Neuron | Agent instance (`AgentNode`) |
| Synapse / synaptic weight | Message channel + routing priority `w_ij(t)` |
| LTP / LTD | Neighbour-first routing bias (near before far) |
| Neural population | Ring cluster of near-neighbours |
| Long-range cortical projection | Deterministic far-link via hash `h(i,k)` |
| Collective firing / coherence | Shared output coherence score Γ(t) |
| Mutation event | New directed edge `(i→j)` not seen in sliding window W |

---

## Architecture

```
monica.py                  ← single-file Python 3.10+ implementation
monica_config.yaml         ← all tuneable parameters (hot-reload)
vllm_benchmark.py          ← throughput / TTFT benchmarking (R₀ estimation)
```

### Agent Tools (4 primitives)

| Tag | Tool | Description |
|-----|------|-------------|
| `<S>` | `msg` | Send message `m` to target set T ⊆ V |
| `<R>` | `read` | Read shared output tail / own memory |
| `<E>` | `add` | Append single character `c` to shared output O |
| `<M>` | `memory` | Overwrite own memory slot M_i ← v |

### Network Topology

```
E = E_ring(r) ∪ E_far(f)

E_ring = { (i, i±d mod N) : 1 ≤ d ≤ r }        # local clustering
h(i,k) = (i·2654435761 + k·40503) mod N + 1     # deterministic far-link
```

Default: `r=1` (left+right neighbour), `f=1` (one far-link per node).
**Long-range links are down-weighted** by ordering near neighbours first
in routing examples — biasing the model without hard-coding routing logic.

### Communication Modes

| Mode | Routing Constraint | Prompt Injection |
|------|--------------------|-----------------|
| `all` | Any node | "You may message any agent" |
| `neighbors_only` | Ring neighbours only | "Only message your neighbours" |
| `prefer_neighbors` | Prefer near, occasional far | "Prefer neighbours; far links rarely" |

Switch modes live in the **Config tab** — no restart required.

---

## Coherence Score Γ(t)

Collective output structure is approximated by compression ratio:

```
Γ(t) = 1 − |zlib.compress(O(t))| / |O(t)|
```

- Γ ≈ 0 → random character noise
- Γ ≈ 1 → structured, repetitive text (high coherence)
- Strings shorter than 50 chars are excluded (insufficient statistics)

---

## AI Mutation Rate μ̂

A **mutation event** is a directed edge `(i→j)` that did not appear in the
preceding `W=60 s` sliding window of message events.

```
μ̂ = M / I

  M = new routing pattern count in observation window
  I = total agent activations (inference completions)
```

Measured result on 20-node network (5 independent 300 s runs):

```
Total activations:   ~1.2 × 10⁴ / run
New routing events:  3.1 ± 0.8 / run
μ̂ ≈ 2.6 × 10⁻⁴  (range: 1.8 × 10⁻⁴ – 4.1 × 10⁻⁴)
```

---

## GUI Tabs

| Tab | Contents |
|-----|----------|
| **Shared Output** | Live character stream written collectively by all agents |
| **Network Graph** | Vogel-spiral layout; message edges fade after 1.2 s TTL |
| **Agent Memory** | Hover any node to inspect current memory value |
| **Config** | Edit `num_agents`, `max_concurrent`, `comm_mode`; save & hot-reload |

---

## Quick Start

### Requirements

```bash
pip install openai pyyaml
```

> Python 3.10+. `tkinter` is included with standard CPython builds.

### Inference Backend

Monica calls an **OpenAI-compatible API endpoint**.
Default tested backend: [vLLM](https://github.com/vllm-project/vllm).

```bash
vllm serve Qwen/Qwen2.5-3B-Instruct-GPTQ-Int4 \
    --max-model-len 8192 \
    --host 0.0.0.0 --port 8000
```

Any model with an OpenAI-compatible `/v1/chat/completions` endpoint works.

### Configuration (`monica_config.yaml`)

```yaml
# Network topology
num_agents: 20
neighbors_near: 1          # ring radius r
neighbors_far: 1           # far-links per node f

# Execution
max_concurrent: 10         # max parallel inference calls
idle_timeout_ms: 1000      # ms between agent activations if idle

# Routing
comm_mode: prefer_neighbors   # all | neighbors_only | prefer_neighbors

# Inference
api_base: "http://localhost:8000/v1"
api_key: "none"
model: "Qwen/Qwen2.5-3B-Instruct-GPTQ-Int4"
temperature: 0.3
top_p: 0.9
max_tokens: 128

# Task
system_task: "Collaboratively write a coherent story, one character at a time."
```

### Run

```bash
python monica.py
```

The GUI launches automatically. Adjust parameters in the **Config tab**
and click **Save & Reload** at any time.

---

## Benchmarking Interaction Rate R₀

Use `vllm_benchmark.py` to measure effective interaction throughput at
different concurrency levels — this provides the empirical `R₀` input
for the CTEMA hazard-rate model.

```bash
python vllm_benchmark.py \
    --base-url http://localhost:8000 \
    --concurrency 1 2 4 8 16 \
    --num-requests 50
```

Output: TTFT (ms), throughput (tok/s), error rate per concurrency level.

---

## Theoretical Background

Monica is the empirical companion to the **Monica Theory** framework, which
proposes that:

1. In multi-agent LLM systems, **agents are the functional neurons** —
   not the underlying parameter weights.
2. **Message channels and routing policy are the synapses**, subject to
   Hebbian-style reinforcement.
3. A **three-layer decomposition** (Parameter / Agent / Network) better
   captures system-level risk than raw parameter count alone.
4. The **AI mutation rate μ̂** measured here feeds a cancer-epidemiology
   hazard model to estimate time-to-CTEMA (Civilisation-Threatening
   Emergent Misalignment event).

Full paper: `monica_theory_zh.tex` (Chinese academic version, 2026).

---

## Hardware Notes

| GPU VRAM | Supported Agents | Notes |
|----------|-----------------|-------|
| 8 GB     | ~50–80          | Quantised 3B model |
| 16 GB    | ~200–300        | RTX 4080 Super tested |
| 24 GB    | ~400–500        | Suitable for scaling experiments |

---

## Repository Structure

```
.
├── monica.py               # Main platform (single file)
├── monica_config.yaml      # All parameters
├── vllm_benchmark.py       # Throughput benchmarking
├── monica_theory_zh.tex    # Companion paper (Chinese, LaTeX)
└── README.md               # This file
```

---

## Authors

- **Newton** — System design, Monica platform, industrial applications
- **Monica** — Conceptual co-development, theory formalisation
- **Zhiliao** *(Corresponding author)* — Research coordination

---

## License

MIT License. See `LICENSE` for details.

