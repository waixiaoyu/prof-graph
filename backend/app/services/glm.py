"""GLM 客户端（Anthropic 协议，T7）。

- 结构化 JSON 输出（代码围栏容错解析，整体解析失败抛 GLMParseError）
- 每次调用写 token_usage（breaker.record_usage）
- 调用前过熔断检查（breaker.ensure_allowed）
- 超时/限流/连接类异常归为 GLMTransientError（可重试），其余 API 错误为 GLMError

transport 可注入：单测用假 transport 替代真实 HTTP。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import breaker
from app.services.breaker import BreakerOpenError, JobClass
from app.settings import settings


class GLMError(Exception):
    pass


class GLMTransientError(GLMError):
    """超时 / 限流 / 连接失败——调用方可按退避策略重试。"""


class GLMParseError(GLMError):
    """响应整体无法解析为 JSON——调用方决定重试或进死信。"""


@dataclass(frozen=True)
class TransportResult:
    text: str
    input_tokens: int
    output_tokens: int


class Transport(Protocol):
    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        ...


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_response(text: str) -> dict:
    """解析模型返回的 JSON：容忍 ```json 围栏与前后杂讯，整体失败抛 GLMParseError。"""
    cleaned = _CODE_FENCE_RE.sub("", text.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise GLMParseError(f"GLM 返回无法解析为 JSON：{text[:200]!r}")


def _default_transport() -> Transport:
    import anthropic

    client = anthropic.AsyncAnthropic(
        base_url=settings.glm_base_url,
        api_key=settings.glm_api_key,
        timeout=settings.glm_request_timeout_ms / 1000,
    )

    async def call(system: str, user: str, max_tokens: int) -> TransportResult:
        try:
            resp = await client.messages.create(
                model=settings.glm_model,
                system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=max_tokens,
            )
        except (anthropic.APITimeoutError, anthropic.RateLimitError, anthropic.APIConnectionError) as e:
            raise GLMTransientError(f"{type(e).__name__}: {e}") from e
        except anthropic.APIError as e:
            raise GLMError(f"{type(e).__name__}: {e}") from e
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        return TransportResult(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    return call


class GLMClient:
    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport or _default_transport()

    async def complete_json(
        self,
        session: AsyncSession,
        system: str,
        user: str,
        job_type: str,
        job_class: JobClass,
        max_tokens: int = 2000,
    ) -> dict:
        """熔断检查 → 调用 → 记账 → 解析。被熔断时抛 BreakerOpenError。"""
        await breaker.ensure_allowed(session, job_class)
        result = await self._transport(system, user, max_tokens)
        await breaker.record_usage(session, job_type, result.input_tokens, result.output_tokens)
        return parse_json_response(result.text)
