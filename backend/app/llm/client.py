"""统一的多端点 LLM 客户端。

设计目标:
1. 一个 LLM 类屏蔽 deepseek / azure / zhipu / f3000 四个 OpenAI 兼容端点的差异,
   上层管线(bible/segment/generate/validate)只认 complete() 这一个方法。
2. 网络/限流类异常自动指数退避重试(tenacity),避免偶发抖动打断长管线。
3. 可选磁盘缓存:相同 prompt 直接复用历史结果,省钱省时,反复调试时尤其重要。
4. 记录 token 用量与耗时,累加到 self.usage,并用 logging 输出一行简报,
   方便事后估算成本与定位慢调用。

注意: 本文件绝不写入或打印任何密钥明文,密钥只从环境变量读取。
"""

# 标准库
import os
import json as _json  # 标准库 json,改名避免和方法参数 json 冲突
import time
import hashlib
import logging
from pathlib import Path

# 第三方库
# tenacity 提供声明式重试装饰器;这里用它实现指数退避
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
# openai SDK,1.59.6;OpenAI 走标准 OpenAI 兼容端点,AzureOpenAI 走 Azure 部署
from openai import OpenAI, AzureOpenAI
# 这些是 openai SDK 抛出的可重试异常类型(网络/限流/服务端 5xx/超时)
from openai import APIConnectionError, APITimeoutError, RateLimitError, InternalServerError


# 模块级 logger,名字带模块路径,方便上层统一配置日志级别
logger = logging.getLogger("screenwright.llm")


# 磁盘缓存根目录: backend/.llm_cache/
# __file__ = backend/app/llm/client.py,向上三级到 backend/
_CACHE_DIR = Path(__file__).resolve().parents[2] / ".llm_cache"


# 定义"哪些异常值得重试"。
# 这几类都是瞬时/外部故障:连不上、超时、被限流、服务端内部错误。
# 业务类错误(如鉴权失败 401、请求参数非法 400)不在其中,重试无意义,直接抛出。
_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)


def _provider_config(provider):
    """根据 provider 返回 (sdk_kind, client_kwargs, default_model)。

    sdk_kind 用来决定实例化 OpenAI 还是 AzureOpenAI。
    没配到对应 env 时抛出清晰的 RuntimeError(只提示缺哪个变量,不泄露任何值)。
    """
    # 缺失变量时统一的报错构造函数
    def _need(var_name):
        raise RuntimeError(
            "LLM 初始化失败: provider=%s 缺少环境变量 %s,请先 source 密钥文件" % (provider, var_name)
        )

    if provider == "deepseek":
        # deepseek 走标准 OpenAI 兼容端点
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            _need("DEEPSEEK_API_KEY")
        base_url = os.getenv("DEEPSEEK_BASE_URL")
        if not base_url:
            _need("DEEPSEEK_BASE_URL")
        # 默认模型可缺省;缺省时退回到 deepseek-chat
        default_model = os.getenv("DEEPSEEK_CHAT_MODEL")
        if not default_model:
            default_model = "deepseek-chat"
        return ("openai", {"api_key": api_key, "base_url": base_url}, default_model)

    if provider == "zhipu":
        # 智谱 GLM 免费端点,同样是 OpenAI 兼容
        api_key = os.getenv("ZHIPU_GLM_FREE_KEY")
        if not api_key:
            _need("ZHIPU_GLM_FREE_KEY")
        base_url = os.getenv("ZHIPU_BASE")
        if not base_url:
            _need("ZHIPU_BASE")
        default_model = os.getenv("ZHIPU_GLM_FREE_MODEL")
        if not default_model:
            _need("ZHIPU_GLM_FREE_MODEL")
        return ("openai", {"api_key": api_key, "base_url": base_url}, default_model)

    if provider == "f3000":
        # f3000 中转端点
        api_key = os.getenv("F3000_GRUNT_KEY")
        if not api_key:
            _need("F3000_GRUNT_KEY")
        base_url = os.getenv("F3000_BASE")
        if not base_url:
            _need("F3000_BASE")
        default_model = os.getenv("F3000_MODEL")
        if not default_model:
            _need("F3000_MODEL")
        return ("openai", {"api_key": api_key, "base_url": base_url}, default_model)

    if provider == "azure":
        # azure 用专门的 AzureOpenAI 客户端,参数名和标准端点不同
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        if not api_key:
            _need("AZURE_OPENAI_API_KEY")
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        if not endpoint:
            _need("AZURE_OPENAI_ENDPOINT")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION")
        if not api_version:
            _need("AZURE_OPENAI_API_VERSION")
        # azure 的"模型"其实是 deployment 名
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
        if not deployment:
            _need("AZURE_OPENAI_DEPLOYMENT")
        kwargs = {
            "api_key": api_key,
            "azure_endpoint": endpoint,
            "api_version": api_version,
        }
        return ("azure", kwargs, deployment)

    # 走到这里说明 provider 名字不在支持列表
    raise RuntimeError(
        "未知 provider: %s,支持的为 deepseek/azure/zhipu/f3000" % provider
    )


class LLM:
    """统一 LLM 客户端。一个实例绑定一个 provider。"""

    def __init__(self, provider=None, cache=True):
        # provider 解析优先级: 显式入参 > 环境变量 LLM_PROVIDER > 默认 deepseek
        if provider is not None:
            self.provider = provider
        else:
            env_provider = os.getenv("LLM_PROVIDER")
            if env_provider:
                self.provider = env_provider
            else:
                self.provider = "deepseek"

        # 读取该 provider 的配置;缺 env 会在这里直接抛错(fail fast)
        sdk_kind, client_kwargs, default_model = _provider_config(self.provider)
        self.default_model = default_model

        # 按 sdk_kind 实例化对应的底层客户端
        if sdk_kind == "azure":
            self._client = AzureOpenAI(**client_kwargs)
        else:
            self._client = OpenAI(**client_kwargs)

        # 是否启用磁盘缓存
        self.cache = cache

        # 用量累加器: 跨多次 complete 调用累计,便于全管线成本统计
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def _cache_key(self, model, messages, want_json, temperature):
        """对 (provider, model, messages, json标志, temperature) 求 sha256。

        messages 是 list[dict],用 json.dumps 且 sort_keys 保证同样内容同样哈希,
        ensure_ascii=False 让中文也参与哈希(不影响正确性,只是更直观)。
        """
        payload = {
            "provider": self.provider,
            "model": model,
            "messages": messages,
            "json": want_json,
            "temperature": temperature,
        }
        raw = _json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, key):
        # 每条缓存一个文件,文件名即哈希
        return _CACHE_DIR / (key + ".json")

    def _cache_load(self, key):
        """命中返回缓存内容 dict {"raw": <字符串>},未命中返回 None。"""
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            # 缓存文件损坏时当作未命中,不让坏缓存拖垮主流程
            return None

    def _cache_store(self, key, raw_text):
        """把底层返回的原始字符串写入缓存。"""
        # 确保目录存在
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = self._cache_path(key)
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump({"raw": raw_text}, f, ensure_ascii=False)
        except Exception:
            # 写缓存失败不应影响调用结果,吞掉异常仅记日志
            logger.warning("写缓存失败: %s", path)

    def complete(self, messages, json=False, model=None, temperature=0.0, **kw):
        """发起一次补全。

        参数:
          messages: OpenAI 风格的消息列表 [{"role":..., "content":...}]
          json: True 时要求模型返回 JSON 对象,并把结果解析成 dict 返回;
                False 时返回纯字符串。
          model: 覆盖默认模型;None 用 provider 默认模型。
          temperature: 采样温度;>0 时不走缓存(因为结果本就要求随机)。
          **kw: 透传给底层 chat.completions.create 的其他参数(如 max_tokens)。

        返回: json=False -> str; json=True -> dict
        """
        # 解析最终使用的模型
        if model is None:
            use_model = self.default_model
        else:
            use_model = model

        # 决定本次是否可用缓存:
        # 只有 cache 开启且 temperature<=0(确定性输出)时才有意义。
        # temperature>0 时每次结果应不同,缓存会破坏随机性,故禁用。
        if self.cache and temperature <= 0:
            use_cache = True
        else:
            use_cache = False

        cache_key = None
        if use_cache:
            cache_key = self._cache_key(use_model, messages, json, temperature)
            cached = self._cache_load(cache_key)
            if cached is not None:
                # 命中: 直接用缓存的原始字符串,不计入 usage(因为没真调),记一行日志
                logger.info(
                    "[LLM cache hit] provider=%s model=%s json=%s",
                    self.provider, use_model, json,
                )
                raw_text = cached["raw"]
                if json:
                    return _json.loads(raw_text)
                else:
                    return raw_text

        # 未命中或不缓存: 真正调用底层(带重试)
        start = time.time()
        raw_text, usage_obj = self._call_with_retry(use_model, messages, json, temperature, kw)
        elapsed = time.time() - start

        # 累加 token 用量。某些端点可能不返回 usage,做兜底。
        prompt_tokens = 0
        completion_tokens = 0
        if usage_obj is not None:
            if getattr(usage_obj, "prompt_tokens", None) is not None:
                prompt_tokens = usage_obj.prompt_tokens
            if getattr(usage_obj, "completion_tokens", None) is not None:
                completion_tokens = usage_obj.completion_tokens
        self.usage["prompt_tokens"] += prompt_tokens
        self.usage["completion_tokens"] += completion_tokens
        self.usage["calls"] += 1

        # 一行简报: 看一眼就知道这次调用花了多少 token、多久
        logger.info(
            "[LLM call] provider=%s model=%s json=%s prompt=%d completion=%d %.2fs",
            self.provider, use_model, json, prompt_tokens, completion_tokens, elapsed,
        )

        # 写缓存(只缓存原始字符串,解析逻辑统一在外层)
        if use_cache and cache_key is not None:
            self._cache_store(cache_key, raw_text)

        # 按 json 标志决定返回类型
        if json:
            return _json.loads(raw_text)
        else:
            return raw_text

    @retry(
        # 最多尝试 4 次(首次 + 3 次重试)
        stop=stop_after_attempt(4),
        # 指数退避: 1s, 2s, 4s ... 上限 10s,缓解限流
        wait=wait_exponential(multiplier=1, min=1, max=10),
        # 只对网络/限流/服务端类异常重试,业务错误立即抛出
        retry=retry_if_exception_type(_RETRYABLE),
        # 重试耗尽后抛出最后一次的真实异常,而非 RetryError,方便上层定位
        reraise=True,
    )
    def _call_with_retry(self, model, messages, want_json, temperature, extra_kw):
        """真正打底层 API 的地方,被 tenacity 装饰以获得重试能力。

        返回 (原始文本, usage 对象)。
        """
        # 组装调用参数
        params = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        # json 模式: 用 OpenAI 兼容的 response_format 强制 JSON 对象输出
        if want_json:
            params["response_format"] = {"type": "json_object"}
        # 透传上层额外参数(如 max_tokens),放最后允许覆盖
        params.update(extra_kw)

        resp = self._client.chat.completions.create(**params)
        # 取第一条候选的文本内容
        content = resp.choices[0].message.content
        return (content, resp.usage)


# 模块级单例缓存: 同一 provider 复用同一个 LLM 实例,
# 避免到处 new 客户端导致重复建连。
_INSTANCES = {}


def get_llm(provider=None):
    """便捷工厂,按 provider 返回(并缓存)LLM 单例。

    provider=None 时解析逻辑与 LLM.__init__ 一致,这里先算出实际 key 再缓存,
    保证 get_llm() 和 get_llm("deepseek") 在默认就是 deepseek 时命中同一实例。
    """
    if provider is not None:
        key = provider
    else:
        env_provider = os.getenv("LLM_PROVIDER")
        if env_provider:
            key = env_provider
        else:
            key = "deepseek"

    if key not in _INSTANCES:
        _INSTANCES[key] = LLM(provider=key)
    return _INSTANCES[key]
