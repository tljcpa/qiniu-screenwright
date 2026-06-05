"""LLM 客户端测试。

分两类:
1. 单元测试: 用 mock 顶掉底层 openai 客户端,不打网络。
   覆盖 json 解析、缓存命中(第二次不调用底层)、usage 累加。
2. 在线冒烟: 仅当 DEEPSEEK_API_KEY 在环境里时才跑,真调一次 deepseek。

运行前需 source 密钥文件让 env 就位:
  set -a; source /root/七牛云比赛/.secrets/shared.env; set +a
"""

import os
import sys
import types
import shutil

import pytest

# 把 backend/ 加进 sys.path,使 `from app.llm.client import ...` 可用。
# 本文件在 backend/tests/,parents[1] 即 backend/。
from pathlib import Path
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.llm import client as client_mod
from app.llm.client import LLM, get_llm


# ---------- 构造一个假的 openai 客户端 ----------

class _FakeUsage:
    """模拟 resp.usage,带 prompt/completion token 数。"""
    def __init__(self, prompt, completion):
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content, prompt=11, completion=7):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(prompt, completion)


class _FakeCompletions:
    """记录被调用次数,并返回预设内容。"""
    def __init__(self, content):
        self._content = content
        self.call_count = 0

    def create(self, **kwargs):
        self.call_count += 1
        # 记录最后一次调用参数,供断言 response_format 等
        self.last_kwargs = kwargs
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class _FakeClient:
    """整体替换 OpenAI() 返回的对象。"""
    def __init__(self, content):
        self.chat = _FakeChat(content)


@pytest.fixture
def fresh_cache(tmp_path, monkeypatch):
    """把缓存目录指到临时目录,保证测试间互不污染。"""
    monkeypatch.setattr(client_mod, "_CACHE_DIR", tmp_path / "llm_cache")
    yield
    # tmp_path 由 pytest 自动清理,无需手动删


@pytest.fixture
def patched_llm(monkeypatch, fresh_cache):
    """构造一个底层被 mock 的 LLM 实例。

    返回 (llm, fake_client),fake_client 用来断言调用次数。
    """
    fake = _FakeClient('{"ok": true, "n": 3}')

    # 顶掉 OpenAI 构造函数,让 LLM.__init__ 里 OpenAI(**kw) 返回我们的假客户端
    monkeypatch.setattr(client_mod, "OpenAI", lambda **kw: fake)
    # 顺手给 env 填上 deepseek 的最小配置,避免 _provider_config 报错
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://fake.local/v1")
    monkeypatch.setenv("DEEPSEEK_CHAT_MODEL", "deepseek-chat")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    llm = LLM(provider="deepseek", cache=True)
    return llm, fake


# ---------- 单元测试 ----------

def test_json_parse(patched_llm):
    """json=True 应返回已解析的 dict,且带 response_format。"""
    llm, fake = patched_llm
    out = llm.complete([{"role": "user", "content": "hi"}], json=True)
    assert isinstance(out, dict)
    assert out["ok"] is True
    assert out["n"] == 3
    # 确认确实传了 response_format
    assert fake.chat.completions.last_kwargs["response_format"] == {"type": "json_object"}


def test_string_return(patched_llm):
    """json=False 应返回原始字符串。"""
    llm, fake = patched_llm
    out = llm.complete([{"role": "user", "content": "hi"}], json=False)
    assert isinstance(out, str)
    assert out == '{"ok": true, "n": 3}'
    # json=False 不应带 response_format
    assert "response_format" not in fake.chat.completions.last_kwargs


def test_cache_hit_skips_backend(patched_llm):
    """同样的请求第二次应命中缓存,底层不再被调用。"""
    llm, fake = patched_llm
    msgs = [{"role": "user", "content": "cache me"}]

    first = llm.complete(msgs, json=True)
    assert fake.chat.completions.call_count == 1

    second = llm.complete(msgs, json=True)
    # 关键断言: 第二次没有再打底层
    assert fake.chat.completions.call_count == 1
    assert first == second


def test_usage_accumulates(patched_llm):
    """每次真调累加 usage;缓存命中不累加。"""
    llm, fake = patched_llm
    msgs_a = [{"role": "user", "content": "a"}]
    msgs_b = [{"role": "user", "content": "b"}]

    llm.complete(msgs_a, json=True)
    # 一次真调: prompt=11 completion=7 calls=1
    assert llm.usage["calls"] == 1
    assert llm.usage["prompt_tokens"] == 11
    assert llm.usage["completion_tokens"] == 7

    llm.complete(msgs_b, json=True)
    # 第二次不同内容,又一次真调
    assert llm.usage["calls"] == 2
    assert llm.usage["prompt_tokens"] == 22
    assert llm.usage["completion_tokens"] == 14

    # 重复 msgs_a -> 命中缓存,usage 不变
    llm.complete(msgs_a, json=True)
    assert llm.usage["calls"] == 2
    assert llm.usage["prompt_tokens"] == 22


def test_temperature_gt0_no_cache(patched_llm):
    """temperature>0 时不缓存,两次都真调。"""
    llm, fake = patched_llm
    msgs = [{"role": "user", "content": "hot"}]
    llm.complete(msgs, json=True, temperature=0.7)
    llm.complete(msgs, json=True, temperature=0.7)
    assert fake.chat.completions.call_count == 2


def test_missing_env_raises(monkeypatch, fresh_cache):
    """缺 env 时初始化应抛清晰错误。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(RuntimeError):
        LLM(provider="deepseek")


def test_unknown_provider_raises(fresh_cache):
    with pytest.raises(RuntimeError):
        LLM(provider="nope")


def test_get_llm_singleton(monkeypatch, fresh_cache):
    """get_llm 同 provider 返回同一实例。"""
    fake = _FakeClient("{}")
    monkeypatch.setattr(client_mod, "OpenAI", lambda **kw: fake)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://x/v1")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    # 清掉模块级单例缓存,避免被别的测试污染
    client_mod._INSTANCES.clear()
    a = get_llm("deepseek")
    b = get_llm("deepseek")
    assert a is b


# ---------- 在线冒烟(可选) ----------

@pytest.mark.skipif(
    not os.getenv("DEEPSEEK_API_KEY"),
    reason="无 DEEPSEEK_API_KEY,跳过在线冒烟(需先 source 密钥文件)",
)
def test_online_smoke_deepseek():
    """真调一次 deepseek,让它返回 {"ok": true} 形态,断言拿到 dict。"""
    llm = LLM(provider="deepseek", cache=False)
    messages = [
        {
            "role": "user",
            "content": "请只返回 JSON 对象 {\"ok\": true},不要任何额外文字。",
        }
    ]
    out = llm.complete(messages, json=True)
    assert isinstance(out, dict)
    assert out.get("ok") is True
    # usage 应被记录
    assert llm.usage["calls"] == 1
