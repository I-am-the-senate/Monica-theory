#!/usr/bin/env python3
"""
monica_parser.py — Custom tool grammar enforcement for Monica Theory

三层防御：
  Layer 1: ToolGrammarFSM  — 正则驱动轻量 FSM，解析多标签工具调用
  Layer 2: repair_json()   — 修复常见 JSON 错误（漏引号/尾逗号/括号不平衡）
  Layer 3: retry loop      — 最多重试 N 次，每次将错误反馈给模型

注意：agent ID 为十进制数字 format(n, '010b') 生成的纯 [01]{10} 字符串
  agent 1   → "0000000001"
  agent 2   → "0000000010"
  agent 99  → "0001100011"
  agent 600 → "1001011000"
"""

import re, json
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════════════
#  TOOL SCHEMA
# ═══════════════════════════════════════════════════════════════════
@dataclass
class ToolSchema:
    name:       str
    tag_open:   str
    tag_close:  str
    required:   list
    types:      dict
    validators: dict

def _valid_bin_id(x):
    """Valid 10-bit binary agent ID: only '0' and '1', exactly 10 chars."""
    return isinstance(x, str) and len(x) == 10 and all(c in "01" for c in x)

TOOL_SCHEMAS = {
    "msg": ToolSchema(
        name="msg", tag_open="<S>", tag_close="</S>",
        required=["t","m"],
        types={"t":list, "m":str},
        validators={
            "t": lambda v: isinstance(v,list) and len(v)>=1 and all(_valid_bin_id(x) for x in v),
            "m": lambda v: isinstance(v,str)  and 0<len(v)<=80,
        }
    ),
    "read": ToolSchema(
        name="read", tag_open="<R>", tag_close="</R>",
        required=["s"],
        types={"s":str},
        validators={"s": lambda v: v in ("in","out","mem")},
    ),
    "add": ToolSchema(
        name="add", tag_open="<E>", tag_close="</E>",
        required=["c"],
        types={"c":str},
        validators={"c": lambda v: isinstance(v,str) and len(v)==1},
    ),
    "memory": ToolSchema(
        name="memory", tag_open="<M>", tag_close="</M>",
        required=["v"],
        types={"v":str},
        validators={"v": lambda v: isinstance(v,str) and 0<len(v)<=100},
    ),
}

# ═══════════════════════════════════════════════════════════════════
#  JSON REPAIR  (Layer 2)
#  Handles the most common small-model JSON mistakes
# ═══════════════════════════════════════════════════════════════════
def repair_json(s: str) -> str:
    s = s.strip()
    # strip markdown fences
    s = re.sub(r"```(?:json)?", "", s).strip("`").strip()
    # single quotes → double quotes
    s = re.sub(r"(?<![\\])'", '"', s)
    # trailing commas before } or ]
    s = re.sub(r",\s*([\]}])", r"\1", s)
    # unquoted JSON keys  {key: → {"key":
    s = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', s)
    # unquoted enum values for "s" key  "s":out → "s":"out"
    s = re.sub(r'("s"\s*:\s*)(in|out|mem)\b', r'\1"\2"', s)
    # balance brackets iteratively (handles one extra closing brace etc.)
    for _ in range(4):
        try:
            json.loads(s)
            break   # already valid
        except json.JSONDecodeError as e:
            msg = str(e)
            if "Extra data" in msg:
                # trim from the position of the extra data
                pos = e.pos if hasattr(e,'pos') else s.rfind("}")
                s = s[:pos].rstrip()
            elif s.count("{") > s.count("}"):
                s += "}"
            elif s.count("[") > s.count("]"):
                s += "]"
            else:
                break
    return s


# ═══════════════════════════════════════════════════════════════════
#  TOOL GRAMMAR FSM  (Layer 1)
# ═══════════════════════════════════════════════════════════════════
@dataclass
class ParsedTool:
    tool:   str
    data:   dict
    valid:  bool
    errors: list = field(default_factory=list)

class ToolGrammarFSM:
    """
    Scans raw LLM output for tool invocation tags.
    Returns list of ParsedTool (validated) + hard error list.
    """
    def __init__(self, schemas=TOOL_SCHEMAS):
        self.schemas  = schemas
        self._patterns = {
            name: re.compile(
                re.escape(s.tag_open) + r"(.*?)" + re.escape(s.tag_close),
                re.DOTALL
            )
            for name, s in schemas.items()
        }

    def parse(self, text: str) -> tuple:
        results, hard_errors = [], []

        for name, pattern in self._patterns.items():
            schema = self.schemas[name]
            for m in pattern.finditer(text):
                raw  = m.group(1).strip()
                errs, warns = [], []
                data = None

                # ── JSON parse with repair fallback ───────────────
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    repaired = repair_json(raw)
                    try:
                        data = json.loads(repaired)
                        warns.append(f"JSON auto-repaired")
                    except json.JSONDecodeError as e:
                        errs.append(f"JSON unrecoverable ({str(e)[:50]})")
                        results.append(ParsedTool(name, {}, False, errs))
                        hard_errors.extend(errs)
                        continue

                # ── Required keys ─────────────────────────────────
                for key in schema.required:
                    if key not in data:
                        errs.append(f"missing required key '{key}'")

                # ── Type + validator ──────────────────────────────
                for key in schema.required:
                    val = data.get(key)
                    if val is None:
                        continue
                    expected_type = schema.types.get(key)
                    if expected_type and not isinstance(val, expected_type):
                        errs.append(f"'{key}' must be {expected_type.__name__}, got {type(val).__name__}")
                        continue
                    validator = schema.validators.get(key)
                    if validator:
                        try:
                            if not validator(val):
                                errs.append(f"'{key}' failed validation: {repr(val)[:60]}")
                        except Exception as ex:
                            errs.append(f"validator crashed on '{key}': {ex}")

                valid = len(errs) == 0
                all_msgs = warns + errs
                results.append(ParsedTool(name, data or {}, valid, all_msgs))
                hard_errors.extend(errs)

        return results, hard_errors

    def error_feedback(self, errors: list) -> str:
        """Compact feedback string to inject as next user turn (saves tokens)."""
        if not errors:
            return ""
        lines = ["TOOL ERRORS (re-emit corrected):"]
        for e in errors[:5]:
            lines.append(f"• {e}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  RETRY WRAPPER  (Layer 3)
# ═══════════════════════════════════════════════════════════════════
MAX_RETRIES = 2

async def chat_with_grammar(client, model, messages, options, fsm):
    """
    Drop-in replacement for client.chat().
    Returns (final_content_str, list_of_valid_ParsedTool).
    """
    content, all_tools, msgs = "", [], list(messages)
    for attempt in range(MAX_RETRIES + 1):
        resp    = await client.chat(model=model, messages=msgs, options=options)
        content = resp["message"]["content"]
        tools, errors = fsm.parse(content)
        valid_tools   = [t for t in tools if t.valid]
        if not errors or attempt == MAX_RETRIES:
            all_tools = valid_tools
            break
        # Inject error feedback — costs ~10-20 extra tokens per retry
        msgs = msgs + [
            {"role": "assistant", "content": content},
            {"role": "user",      "content": fsm.error_feedback(errors)},
        ]
    return content, all_tools


# ═══════════════════════════════════════════════════════════════════
#  UNIT TESTS
# ═══════════════════════════════════════════════════════════════════
def _run_tests():
    fsm = ToolGrammarFSM()
    # Use real binary IDs: format(n, '010b')
    A2   = format(2,   '010b')   # 0000000010
    A3   = format(3,   '010b')   # 0000000011
    A99  = format(99,  '010b')   # 0001100011
    A512 = format(512, '010b')   # 1000000000

    cases = [
        ("perfect MSG (2 targets)",
            f'<S>{{"t":["{A2}","{A99}"],"m":"hello"}}</S>',              1),
        ("single-quote JSON repaired",
            f"<S>{{'t': ['{A2}'], 'm': 'hi'}}</S>",                       1),
        ("trailing comma repaired",
            f'<S>{{"t":["{A2}",],"m":"test",}}</S>',                      1),
        ("missing key m",
            f'<S>{{"t":["{A2}"]}}</S>',                                    0),
        ("non-binary ID rejected",
            '<S>{"t":["agent_2"],"m":"hi"}</S>',                           0),
        ("ADD ok",
            '<E>{"c":"A"}</E>',                                            1),
        ("ADD multi-char rejected",
            '<E>{"c":"AB"}</E>',                                           0),
        ("READ in",
            '<R>{"s":"in"}</R>',                                           1),
        ("READ out",
            '<R>{"s":"out"}</R>',                                          1),
        ("READ bad source",
            '<R>{"s":"global"}</R>',                                       0),
        ("MEMORY ok",
            '<M>{"v":"I know agent 2 works on vowels."}</M>',              1),
        ("MEMORY too long",
            '<M>{"v":"' + "x"*101 + '"}</M>',                             0),
        ("unquoted enum repaired",
            '<R>{"s":out}</R>',                                            1),
        ("extra closing brace repaired",
            f'<S>{{"t":["{A3}"],"m":"saw input"}}}}</S>',                  1),
        ("3 tools in one response",
            f'<R>{{"s":"in"}}</R><S>{{"t":["{A3}"],"m":"noted"}}</S><E>{{"c":"M"}}</E>',
            3),
    ]

    print(f"\n{'Case':<44} {'Exp':>5} {'Got':>5} {'':>4}")
    print("─" * 62)
    passed = 0
    for desc, text, expect in cases:
        tools, errors = fsm.parse(text)
        valid = sum(1 for t in tools if t.valid)
        ok = "✓" if valid == expect else "✗"
        if valid == expect: passed += 1
        print(f"  {desc:<42} {expect:>5} {valid:>5}  {ok}")
        if valid != expect and errors:
            for e in errors[:2]: print(f"    ↳ {e}")

    print(f"\n{passed}/{len(cases)} passed")
    return passed, len(cases)


if __name__ == "__main__":
    _run_tests()
