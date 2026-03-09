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

*(其余保持你现有英文 README 内容：架构、工具、拓扑、μ 估计等)*

## Documentation

- English paper (LaTeX): [`docs/monica_theory_en.tex`](docs/monica_theory_en.tex)
- Chinese paper (LaTeX): [`docs/monica_theory_zh.tex`](docs/monica_theory_zh.tex)

