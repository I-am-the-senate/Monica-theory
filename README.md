# Monica Theory — 使用文档

> by newton

---

## 概述

Monica Theory 是一个多智能体网络实验平台。
N 个 LLM 节点组成一个环形拓扑网络，每个节点持续接收消息、调用 API 推理、
向邻居传递消息，并协作向共享输出写入字符。
整个过程通过实时 GUI 可视化监控。

---

## 快速开始

### 环境要求

- Python 3.10+
- 依赖库：`openai` `httpx` `tkinter`（标准库）`pyyaml`
- 本地或远程 OpenAI 兼容推理服务（如 vLLM、Ollama、LM Studio）

```bash
pip install openai httpx pyyaml
python monica.py
```

### 启动步骤

1. 在顶部工具栏填写 **Endpoint**、**API Key**、**Model**
2. 点击 **▶ Start** 启动网络
3. 切换到 **Network Graph** 标签查看节点通信动态
4. 切换到 **Shared Output** 标签查看协作输出内容
5. 点击 **■ Stop** 停止网络

---

## GUI 界面

### 顶部工具栏

| 控件 | 说明 |
|---|---|
| Endpoint | 推理服务地址，默认 `http://localhost:8000` |
| API Key | API 密钥，本地服务填 `EMPTY` |
| Model | 模型名称，需与推理服务一致 |
| ▶ Start | 启动节点网络 |
| ■ Stop | 停止网络（保留记忆文件） |
| ↺ Reload | 热重载 `monica_config.yaml`，无需重启 |

### 标签页

| 标签 | 内容 |
|---|---|
| **Shared Output** | 所有节点协作写入的共享字符流 |
| **Network Graph** | 实时节点通信动态图，粉色连线为消息边 |
| **Agent Memory** | 各节点当前记忆内容（悬停查看） |
| **Errors** | API 超时、解析失败等错误记录 |
| **Debug** | 详细日志流（可开关） |
| **Messages** | 节点间消息明细（发送方→接收方：内容） |
| **⚙ Config** | 在线编辑 `monica_config.yaml` 并保存重载 |

### Config 标签快捷控件

**第一行：**
- `Idle wake ms`：空闲唤醒间隔（毫秒），点 Apply 立即生效

**第二行（网络拓扑）：**

| 控件 | 说明 |
|---|---|
| Agents | 总节点数（需重启生效） |
| Concurrent | 最大并发 API 调用数（需重启生效） |
| Apply | 保存到 YAML |
| 🌐 全网 | 节点可向任意其他节点发消息 |
| ⭕ 仅近邻 | 节点只能向直接邻居发消息 |
| ⭐ 优先近邻 | 优先联系邻居，偶尔可跨远程（默认） |

---

## 配置文件：`monica_config.yaml`

### `api` — 推理接口

```yaml
api:
  endpoint: http://localhost:8000   # 推理服务地址
  api_key: EMPTY                    # API 密钥
  model: Qwen/Qwen2.5-3B-Instruct  # 模型名
```

### `network` — 网络参数

```yaml
network:
  num_agents: 20        # 节点总数
  max_concurrent: 10    # 同时进行的 API 调用上限
  max_tokens: 128       # 每次推理最大 token 数
  neighbors_near: 1     # 环形近邻距离（左右各 N 个）
  neighbors_far: 1      # 长程随机链接数（0 = 纯环形）
  comm_mode: prefer_neighbors  # all | neighbors_only | prefer_neighbors
```

### `idle_wake` — 空闲唤醒

网络静默超过 `timeout_ms` 毫秒后，自动向 `targets` 节点发送唤醒消息，
防止网络陷入沉默。

```yaml
idle_wake:
  enabled: true
  timeout_ms: 1000
  message: "网络启动，请向邻居发送消息"
  targets: [1, 2, 3]
```

### `tools` — 节点工具

每个工具可独立 `enabled: true/false`：

| 工具 | 标签 | 功能 |
|---|---|---|
| msg | `<S>…</S>` | 向其他节点发消息（支持多目标） |
| read | `<R>…</R>` | 读取用户输入 / 共享输出 / 自身记忆 |
| add | `<E>X</E>` | 向共享输出追加单个字符 |
| memory | `<M>…</M>` | 覆写自身 100 字记忆 |

### `task` — 任务指令

```yaml
task: "在共享输出里写出斐波那契数列"
```

留空则节点自由协作。修改后点 **↺ Reload** 立即生效，无需重启。

### `context` — 上下文控制

```yaml
context:
  history_token_budget: 2048   # 每次推理保留的历史 token 上限
  memory_max_chars: 100        # 每个节点记忆文件最大字符数
```

---

## 工作原理

```
启动
 └─ 所有节点同时上线，等待 inbox 事件
     └─ idle_wake 定时向种子节点投递消息
         └─ 节点被唤醒 → 调用 API → 解析工具调用
             ├─ <S> → 向邻居投递消息（邻居被唤醒）
             ├─ <E> → 向共享输出追加字符
             ├─ <M> → 更新自身记忆
             └─ <R> → 读取输入/输出/记忆，立即再次推理
```

网络通过消息驱动自我维持，只要有节点在通信，
`idle_wake` 就不会触发；网络沉默后由 `idle_wake` 重新激活。

---

## 节点拓扑

20 个节点默认组成**带长程链接的环形网络**：

```
1 ─ 2 ─ 3 ─ … ─ 20 ─ 1   （环形近邻）
 ╲         ╱            （长程随机链接，每节点 1 条）
```

- `neighbors_near: 1` → 每节点左右各 1 个近邻
- `neighbors_far: 1` → 每节点额外 1 条确定性长程链接
- 长程链接固定（基于节点 ID 哈希），不随机变化

---

## 文件说明

| 文件/目录 | 说明 |
|---|---|
| `monica.py` | 主程序 |
| `monica_config.yaml` | 全部配置，运行中可热重载 |
| `monica_input.txt` | 用户向网络注入的输入文本 |
| `monica_output.txt` | 网络协作产生的输出（自动追加） |
| `monica_memory/` | 每个节点的记忆文件（`1.txt` … `N.txt`） |

---

## 常见问题

**Q：节点只响应一次就停了**
A：检查 `idle_wake.enabled: true` 且 `timeout_ms` 不要太大；
也可在 Config 标签实时调整 Idle wake ms。

**Q：Shared Output 始终为空**
A：确认 `tools.add.enabled: true`；
给 `task` 字段一个明确指令，如"将字母A写入输出"。

**Q：想换更大的模型**
A：修改 `api.model`，同步调大 `network.max_tokens` 和
`context.history_token_budget`，然后 ↺ Reload。

**Q：如何清空所有节点记忆**
A：删除 `monica_memory/` 目录下所有 `.txt` 文件，
或在 Agent Memory 标签中逐个清除。
