#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地模型客户端（OpenAI 兼容 /v1），纯标准库，无第三方依赖。

适配过的端点形态：
  - FreeToken / vLLM / LM Studio / llama.cpp server 的 /v1
  - 可能不支持 response_format（无约束解码）→ 靠提示词 + 宽容 JSON 解析
  - 可能支持 chat_template_kwargs.enable_thinking=false（显著提速）；不支持会自动降级
  - content 为空但 reasoning_content 有内容（思维链模型）→ 自动回退取思维链

用法：
    from llm import Client
    c = Client(endpoint="http://127.0.0.1:1919/v1", model="...")
    text = c.chat([{"role": "user", "content": "..."}])
    obj = c.json(messages)          # 自动剥围栏 / 截配平括号 / 修尾逗号
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class JSONError(LLMError):
    def __init__(self, msg, raw=""):
        super().__init__(msg)
        self.raw = raw


def _strip_fence(t: str) -> str:
    t = t.strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", t, re.S)
    return m.group(1).strip() if m else t


def _brace_slice(t: str):
    """截出第一个配平的 {...}，字符串内的括号不计。"""
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    return None


def parse_json(text: str) -> dict:
    """把模型输出尽最大努力解析为 dict。"""
    if not text or not text.strip():
        raise JSONError("模型返回空内容")
    cand = _strip_fence(text)
    pieces = [cand]
    sliced = _brace_slice(cand)
    if sliced:
        pieces.append(sliced)
    for t in pieces:
        for attempt in (t,
                        re.sub(r",\s*([}\]])", r"\1", t),
                        re.sub(r",\s*([}\]])", r"\1", t).replace("“", '"').replace("”", '"')):
            try:
                obj = json.loads(attempt)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    raise JSONError("无法解析为 JSON", text[:1500])


class Client:
    def __init__(self, endpoint="http://127.0.0.1:1919/v1", model="local-model",
                 timeout=1200, temperature=0.2, max_tokens=16384, retries=2,
                 thinking=False, verbose=True, tag="", api_key=""):
        self.base = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retries = retries
        self.thinking = thinking
        self.verbose = verbose
        self.tag = tag
        self.api_key = api_key
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.seconds = 0.0
        self._no_think_ok = True

    def _say(self, msg):
        if self.verbose:
            print(("[%s] " % self.tag if self.tag else "") + msg, flush=True)

    def chat(self, messages, max_tokens=None, temperature=None, thinking=None):
        thinking = self.thinking if thinking is None else thinking
        mt = max_tokens or self.max_tokens
        temp = self.temperature if temperature is None else temperature
        last = None
        for attempt in range(self.retries + 1):
            payload = {"model": self.model, "messages": messages,
                       "temperature": temp, "max_tokens": mt}
            if not thinking and self._no_think_ok:
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = "Bearer " + self.api_key
            req = urllib.request.Request(
                self.base + "/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers)
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                dt = time.time() - t0
                ch = (data.get("choices") or [{}])[0]
                msg = ch.get("message") or {}
                text = (msg.get("content") or "").strip()
                if not text:
                    # 思维链模型可能只填 reasoning_content；没有正文时退回取它
                    text = (msg.get("reasoning_content") or "").strip()
                usage = data.get("usage") or {}
                self.calls += 1
                self.seconds += dt
                self.prompt_tokens += usage.get("prompt_tokens", 0) or 0
                self.completion_tokens += usage.get("completion_tokens", 0) or 0
                self._say("call#%d %.1fs 输出 %d tok（%s）" % (
                    self.calls, dt, usage.get("completion_tokens", 0) or 0,
                    ch.get("finish_reason")))
                if not text:
                    if ch.get("finish_reason") == "length":
                        raise LLMError("输出被 max_tokens 截断且内容为空")
                    raise LLMError("模型返回空 content")
                return text
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")
                if e.code == 400 and "chat_template_kwargs" in body and self._no_think_ok:
                    self._no_think_ok = False
                    self._say("端点不认 chat_template_kwargs，改用提示词抑制思维链")
                    continue
                last = LLMError("HTTP %s: %s" % (e.code, body[:300]))
            except Exception as e:  # 网络 / 超时 / 解析
                last = LLMError("%s: %s" % (type(e).__name__, str(e)[:300]))
            self._say("第 %d 次失败：%s" % (attempt + 1, str(last)[:160]))
            time.sleep(1.5 * (attempt + 1))
        raise last if last else LLMError("调用失败")

    def json(self, messages, max_tokens=None, temperature=None, thinking=None):
        text = self.chat(messages, max_tokens=max_tokens,
                         temperature=temperature, thinking=thinking)
        try:
            return parse_json(text)
        except JSONError:
            fix = messages + [
                {"role": "assistant", "content": text},
                {"role": "user",
                 "content": "上面的输出不是合法 JSON。请只输出一个合法 JSON 对象，"
                            "不要代码块围栏、不要解释、不要前后缀文字。"}]
            text2 = self.chat(fix, max_tokens=max_tokens,
                              temperature=0.0, thinking=False)
            return parse_json(text2)

    def stats(self):
        return {"calls": self.calls, "seconds": round(self.seconds, 1),
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens}
