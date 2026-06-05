# -*- coding: utf-8 -*-
"""
校验 + 自动修复(Pass4 的一半)。

核心思路：
- LLM 产出的剧本可能是不太干净的 YAML/JSON 文本(或已 parse 的 dict)。
- 先尝试解析 + pydantic 校验。成功直接返回 Screenplay。
- 失败时，把 pydantic 的 ValidationError 文本 + 原始内容回喂给 LLM，
  让它输出修复后的版本，最多重试 max_retries 次。

设计约束：
- llm 是鸭子类型：任何带 .complete(messages: list[dict], json: bool=False) -> str|dict
  方法的对象都行。这里不 import 任何具体 client，只依赖这个接口。
- 本模块不主动调外部 API：是否真的发请求由传进来的 llm 决定。
"""

from __future__ import annotations

import json
from typing import Union

import yaml
from pydantic import ValidationError

from .models import Screenplay


def _coerce_to_dict(raw: Union[str, dict]) -> dict:
    """
    把原始输入统一成 dict。

    - 已经是 dict：直接返回。
    - 是字符串：先试 JSON(更严格、报错更利于判断)，失败再试 YAML。
      因为 YAML 是 JSON 的超集，safe_load 也能吃 JSON，
      但优先 json.loads 能在纯 JSON 场景给出更精确的语法错误。
    解析失败抛 ValueError，交给上层进入修复重试。
    """
    if isinstance(raw, dict):
        return raw

    # 走到这里 raw 是字符串。
    if isinstance(raw, str):
        # 先尝试 JSON。
        try:
            parsed = json.loads(raw)
        except Exception:
            # JSON 失败，退回 YAML。
            try:
                parsed = yaml.safe_load(raw)
            except Exception as exc:
                raise ValueError("无法解析为 JSON 或 YAML: " + str(exc))

        # 解析结果必须是映射，否则不是合法剧本结构。
        if not isinstance(parsed, dict):
            raise ValueError("解析结果不是对象(dict)，得到: " + type(parsed).__name__)
        return parsed

    # 既不是 dict 也不是 str，类型非法。
    raise TypeError("raw 必须是 str 或 dict，得到: " + type(raw).__name__)


def _try_build(raw: Union[str, dict]) -> Screenplay:
    """
    尝试把原始输入构造成 Screenplay。

    两步都可能失败：
    - 解析阶段(_coerce_to_dict) 抛 ValueError/TypeError。
    - 校验阶段(model_validate) 抛 ValidationError。
    这里不吞异常，原样上抛，让 validate_and_repair 决定是否修复重试。
    """
    data = _coerce_to_dict(raw)
    # model_validate 触发判别联合按 type 选子模型 + 全字段校验。
    return Screenplay.model_validate(data)


def _build_repair_messages(original: Union[str, dict], error_text: str) -> list:
    """
    构造回喂给 LLM 的修复对话。

    把"出了什么错"和"原始内容"都给 LLM，要求它只输出修复后的完整 YAML。
    用 YAML 作为修复目标格式，与 to_yaml/from_yaml 的主格式一致。
    """
    # 原始内容若是 dict，转成 YAML 文本再喂(便于 LLM 阅读和对照行)。
    if isinstance(original, dict):
        original_text = yaml.safe_dump(original, allow_unicode=True, sort_keys=False)
    else:
        original_text = original

    # system 设定角色与硬性输出约束。
    system_msg = {
        "role": "system",
        "content": (
            "你是剧本 schema 修复器。下面给你一份未通过 pydantic 校验的剧本，"
            "以及校验错误。请输出修复后的完整剧本 YAML，"
            "不要解释、不要 markdown 代码围栏，只输出 YAML 本身。"
        ),
    }
    # user 给出错误文本 + 原始内容。
    user_msg = {
        "role": "user",
        "content": (
            "校验错误如下:\n"
            + error_text
            + "\n\n原始内容如下:\n"
            + original_text
            + "\n\n请输出修复后的完整剧本 YAML。"
        ),
    }
    return [system_msg, user_msg]


def validate_and_repair(
    raw: Union[str, dict],
    llm,
    max_retries: int = 2,
) -> Screenplay:
    """
    解析 + 校验剧本，失败则回喂 LLM 修复并重试。

    参数：
    - raw: LLM 吐的原始字符串(YAML 或 JSON)或已 parse 的 dict。
    - llm: 鸭子类型，需有 .complete(messages, json=False) -> str|dict。
    - max_retries: 修复重试次数(不含首次尝试)。

    返回：校验通过的 Screenplay。
    全部重试用尽仍失败：抛出最后一次的异常。
    """
    # current 是当前待校验的内容，每轮修复后被替换为 LLM 的新产出。
    current: Union[str, dict] = raw
    # 记录最后一次异常，重试耗尽时抛它。
    last_error: Exception

    # 总尝试次数 = 首次 1 次 + max_retries 次修复。
    total_attempts = max_retries + 1
    attempt = 0
    while attempt < total_attempts:
        try:
            # 尝试构造；成功直接返回，结束整个流程。
            return _try_build(current)
        except (ValidationError, ValueError, TypeError) as exc:
            # 记下异常。
            last_error = exc

            # 如果这已经是最后一次尝试，不再修复，跳出循环后抛错。
            if attempt == total_attempts - 1:
                break

            # 还有重试机会：把错误文本 + 原始内容回喂 LLM 求修复。
            error_text = str(exc)
            messages = _build_repair_messages(current, error_text)
            # 调用 llm。json=False 表示我们期望文本(YAML)，不强制 JSON 模式。
            fixed = llm.complete(messages, json=False)
            # 用 LLM 的修复产出作为下一轮待校验内容。
            current = fixed

        # 进入下一轮。
        attempt = attempt + 1

    # 循环结束仍未成功，抛出最后一次异常。
    raise last_error
