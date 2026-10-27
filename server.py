# -*- coding: utf-8 -*-
"""
CodeArts OpenAI 兼容代理
=========================
把华为云 CodeArts 模型接口（openpangu-2.0-pro）包装成标准 OpenAI
Chat Completions API，供 Claude Code / OpenCode / Cline / Cherry Studio
等任何支持自定义 OpenAI Base URL 的工具直接使用。

启动方式:
    pip install -r requirements.txt
    python server.py

默认监听:  http://127.0.0.1:8787
OpenAI Base URL: http://127.0.0.1:8787/v1
"""
import collections
import datetime
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

load_dotenv()

AK = os.getenv("CODEARTS_AK", "").strip()
SK = os.getenv("CODEARTS_SK", "").strip()
if not AK or not SK:
    raise SystemExit("缺少 CODEARTS_AK / CODEARTS_SK，请在 .env 中配置后重试")

# CodeArts 目标端点（从抓包确认）
BASE_URL = "https://snap-access.cn-north-4.myhuaweicloud.com"
TARGET = BASE_URL + "/api/v2/chat/completions"
HOST = "snap-access.cn-north-4.myhuaweicloud.com"
AGENT_LIST_URL = BASE_URL + "/v1/agent-center/agents/useragents?offset=0&limit=100"
AGENT_DETAIL_PATH = "/v1/agent-center/agents/detail"

# 自动同步失败时的兜底列表（仅老套餐模型，走 AgentCenter 通道）；正常启动会用云端返回的列表覆盖它
FALLBACK_AGENT_MODELS = ["openpangu-2.0-pro", "openpangu-2.0-flash", "GLM-5.2"]
# 免费模型兜底列表（IDE 抓包确认）：走 maas_type=benefit 通道，签名时必须额外携带
# maas_type / model-id / model-name 三个头，否则报 "model is not registered"；
# 老套餐模型带上这些头反而会报 "unsupported model"，因此两条通道严格区分。
BENEFIT_MODELS = [
    "glm-5.3-flash",
    "deepseek-v4-pro-0813",
    "deepseek-v4-flash-0731",
]
FALLBACK_MODELS = FALLBACK_AGENT_MODELS + BENEFIT_MODELS
# opengw 免费额度网关（IDE 抓包确认）：永久 AK/SK 即可访问，gateway/config 返回免费模型列表
OPENGW_BASE = "https://opengw.developer.huaweicloud.com"
OPENGW_CONFIG_URL = OPENGW_BASE + "/api/v1/gateway/config"
# 模型缓存与代理程序放在同一目录，便于迁移、备份和排查
MODEL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models-cache.json"
)
# AgentCenter 通道模型（决定是否用 benefit 签名；不在其中的模型一律按 benefit 处理，
# 这样 IDE 以后新增免费模型无需改代码，直接传模型名即可）
AGENT_MODELS = list(FALLBACK_AGENT_MODELS)
SUPPORTED_MODELS = list(FALLBACK_MODELS)
MODEL_DETAILS = {}

# 代理监听地址；改端口只需改这里（或设环境变量），启动日志会同步变化
LISTEN_HOST = os.getenv("CODEARTS_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("CODEARTS_PROXY_PORT", "8787"))

# ------------------------------------------------------------------ 并发闸门
# 实测结论（probe_concurrency.py，见 README「并发限制」一节）：
#   1. 上游超限是「硬拒绝」，不是排队：HTTP 400 + {"error_code":"TM.00001041",
#      "error_msg":"并发会话数已达上限(3个)..."}，该模型实测上限为 3 个并发会话；
#      另有一类按分钟计数的限流：InferHub.ModelArts.81101.429（300 次/分钟）。
#   2. 会话在请求结束后不会立刻归还额度，实测需再等约 45~60 秒才释放。
#      因此「同时在途请求数」并不能反映上游真实占用，必须把冷却期也算进去。
# 所以这里用「在途 + 冷却」的负载模型：请求结束后槽位继续占用 SESSION_COOLDOWN 秒。
MAX_CONCURRENCY = int(os.getenv("CODEARTS_MAX_CONCURRENCY", "3"))
# 排队等待上限；超过则返回 429 + Retry-After，交由客户端退避重试
QUEUE_TIMEOUT = float(os.getenv("CODEARTS_QUEUE_TIMEOUT", "120"))
# 会话冷却：模拟上游「会话额度延迟归还」。设 0 可退回纯在途限流（会重新出现
# TM.00001041，但吞吐不受冷却影响）
SESSION_COOLDOWN = float(os.getenv("CODEARTS_SESSION_COOLDOWN", "45"))

app = Flask(__name__)


class ConcurrencyGate:
    """限制同时打向上游的请求数，避免触发上游的并发会话上限。

    负载 = 在途请求数 + 冷却中的槽位数。冷却用于吸收上游「会话额度延迟归还」
    的行为（实测约 45~60 秒），否则一波请求刚结束就发下一波必定被拒。

    严格 FIFO 排队，保证不会有客户端被持续插队饿死。
    """

    def __init__(self, limit: int):
        self._cond = threading.Condition()
        self._limit = max(1, int(limit))
        self._active = 0
        self._waiters = collections.deque()
        self._ticket = 0
        self.peak = 0
        self.timeouts = 0
        self.rejected = 0

    def acquire(self, timeout: float) -> bool:
        """排队获取一个槽位，超时返回 False。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            self._ticket += 1
            ticket = self._ticket
            self._waiters.append(ticket)
            granted = False
            try:
                while True:
                    now = time.monotonic()
                    if self._waiters[0] == ticket and self._active < self._limit:
                        self._waiters.popleft()
                        self._active += 1
                        self.peak = max(self.peak, self._active)
                        granted = True
                        return True
                    remaining = deadline - now
                    if remaining <= 0:
                        self.timeouts += 1
                        return False
                    self._cond.wait(min(remaining, 0.5))
            finally:
                if not granted and ticket in self._waiters:
                    self._waiters.remove(ticket)
                self._cond.notify_all()

    def release(self, cooldown: float = 0.0) -> None:
        """归还槽位；cooldown > 0 时延迟归还，模拟上游会话的延迟释放。"""
        if cooldown and cooldown > 0:
            timer = threading.Timer(cooldown, self._release_now)
            timer.daemon = True
            timer.start()
        else:
            self._release_now()

    def _release_now(self) -> None:
        with self._cond:
            if self._active > 0:
                self._active -= 1
            self._cond.notify_all()

    def stats(self) -> dict:
        with self._cond:
            return {
                "limit": self._limit,
                "active": self._active,
                "waiting": len(self._waiters),
                "peak": self.peak,
                "queue_timeouts": self.timeouts,
            }


GATE = ConcurrencyGate(MAX_CONCURRENCY)


def _rate_limited_response(message: str, retry_after: int):
    """排队超时时返回标准 429，让客户端按 Retry-After 退避重试。"""
    resp = jsonify(
        {"error": {"message": message, "type": "rate_limit_error",
                   "code": "concurrency_limit"}}
    )
    resp.headers["Retry-After"] = str(max(1, int(retry_after)))
    return _cors(resp), 429


def _signed_request(method: str, url: str, body: bytes = b"", extra_headers=None, timeout=30):
    """发送带华为云 SDK-HMAC-SHA256 签名的请求。"""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    canonical_uri = path if path.endswith("/") else path + "/"
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    params.sort(key=lambda item: (item[0], item[1]))
    quote = lambda value: urllib.parse.quote(str(value), safe="-_.~")
    canonical_query = "&".join(
        f"{quote(key)}={quote(value)}" for key, value in params
    )

    headers = {}
    for key, value in (extra_headers or {}).items():
        headers[key.lower()] = str(value)
    headers["host"] = parsed.netloc
    headers.setdefault("content-type", "application/json")
    sdk_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers["x-sdk-date"] = sdk_date

    signed_names = sorted(headers)
    canonical_headers = "".join(
        f"{name}:{headers[name].strip()}\n" for name in signed_names
    )
    signed_headers = ";".join(signed_names)
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = (
        f"{method.upper()}\n{canonical_uri}\n{canonical_query}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        f"SDK-HMAC-SHA256\n{sdk_date}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    signature = hmac.new(
        SK.encode(), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    headers["authorization"] = (
        f"SDK-HMAC-SHA256 Access={AK}, SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    return requests.request(
        method.upper(), url, data=body or None, headers=headers, timeout=timeout
    )


def _hmac_headers(body: bytes, benefit: bool = False, model_id: str = "") -> dict:
    """构造华为云 SDK-HMAC-SHA256 签名请求头（仅适用于 POST /api/v2/chat/completions）。

    benefit=True 时按 IDE 免费模型通道附加 maas_type / model-id / model-name 三个头，
    这三个头必须参与签名，否则上游返回 "The model is not registered"。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    sdk_date = now.strftime("%Y%m%dT%H%M%SZ")

    # 注意：华为云签名规范要求消息头名称一律转小写再参与签名
    headers = {
        "host": HOST,
        "content-type": "application/json",
        "x-sdk-date": sdk_date,
    }
    if benefit and model_id:
        headers["maas_type"] = "benefit"
        headers["model-id"] = model_id
        headers["model-name"] = model_id

    signed_names = sorted(headers.keys())
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in signed_names)
    signed_headers = ";".join(signed_names)

    # CanonicalRequest: POST + URI(带尾斜杠) + 空查询串 + 规范头 + 签名头 + body哈希
    canonical_uri = "/api/v2/chat/completions/"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = (
        f"POST\n{canonical_uri}\n\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    # StringToSign: 算法 + 时间戳 + CanonicalRequest哈希
    hashed_canonical = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = f"SDK-HMAC-SHA256\n{sdk_date}\n{hashed_canonical}"

    signature = hmac.new(SK.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"SDK-HMAC-SHA256 Access={AK}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def _load_model_cache() -> bool:
    """加载本地模型缓存，返回是否成功。"""
    global SUPPORTED_MODELS, MODEL_DETAILS
    try:
        with open(MODEL_CACHE_PATH, "r", encoding="utf-8") as file:
            cached = json.load(file)
        models = cached.get("models", [])
        if not models:
            return False
        SUPPORTED_MODELS = [item["id"] for item in models if item.get("id")]
        MODEL_DETAILS = {item["id"]: item for item in models if item.get("id")}
        # 缓存里的 AgentCenter 模型参与通道判断；旧缓存无 channel 字段时默认
        # 视为 agent 通道；不在缓存里的新模型一律按 benefit 处理。
        AGENT_MODELS.clear()
        AGENT_MODELS.extend(
            item["id"]
            for item in models
            if item.get("channel", "agent") == "agent"
        )
        return bool(SUPPORTED_MODELS)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _save_model_cache(models) -> None:
    os.makedirs(os.path.dirname(MODEL_CACHE_PATH), exist_ok=True)
    payload = {
        "updated_at": int(time.time()),
        "models": models,
    }
    temp_path = MODEL_CACHE_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, MODEL_CACHE_PATH)


def _refresh_benefit_models() -> list:
    """从 opengw 免费额度网关动态获取免费模型列表（IDE 抓包确认的接口）。

    返回模型详情列表；失败时返回空列表，由调用方回退到 BENEFIT_MODELS 兜底。
    """
    try:
        response = _signed_request("GET", OPENGW_CONFIG_URL, timeout=30)
        response.raise_for_status()
        result = response.json().get("result") or {}
        models = result.get("models") or []
        discovered = []
        for model in models:
            model_id = model.get("model_id")
            if not model_id or any(item["id"] == model_id for item in discovered):
                continue
            discovered.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "codearts",
                    "channel": "benefit",
                    "name": model.get("model_name") or model_id,
                    "description": "免费额度模型（签到领取）",
                    "context_window": model.get("context_window"),
                    "max_tokens": model.get("max_tokens"),
                    "supports_images": False,
                }
            )
        return discovered
    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        print("免费模型列表同步失败，将使用兜底列表:", error)
        return []


def _refresh_models() -> bool:
    """从 CodeArts AgentCenter 获取当前账号可用模型（仅老套餐通道）。"""
    global SUPPORTED_MODELS, MODEL_DETAILS
    try:
        # 与 CodeArts CLI 抓包一致：AgentCenter 接口需要这个路由头。
        response = _signed_request(
            "GET",
            AGENT_LIST_URL,
            extra_headers={
                "agent-type": "AgentCenter",
                "x-language": "zh-cn",
                "accept": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        agents = data.get("agents", [])
        if not agents:
            return False

        # 优先主 Agent；若某个详情无模型，再尝试其他主 Agent。
        candidates = sorted(
            agents,
            key=lambda item: (
                not bool(item.get("is_primary_agent")),
                item.get("agent_order") is None,
                item.get("agent_order") or 999999,
            ),
        )
        discovered = []
        for agent in candidates:
            agent_id = agent.get("agent_id")
            if not agent_id:
                continue
            detail_url = (
                f"{BASE_URL}{AGENT_DETAIL_PATH}?agent_id="
                f"{urllib.parse.quote(str(agent_id), safe='')}"
            )
            detail_response = _signed_request(
                "GET",
                detail_url,
                extra_headers={
                    "agent-type": "AgentCenter",
                    "x-language": "zh-cn",
                    "accept": "application/json",
                },
                timeout=30,
            )
            if detail_response.status_code != 200:
                continue
            detail = detail_response.json()
            for model in (detail.get("gpts", {}).get("models", []) or []):
                model_id = model.get("model_id") or model.get("model_alias")
                if not model_id or any(item["id"] == model_id for item in discovered):
                    continue
                params = model.get("model_parameters") or {}
                discovered.append(
                    {
                        "id": model_id,
                        "object": "model",
                        "owned_by": "codearts",
                        "channel": "agent",
                        "name": model.get("model_alias") or model.get("model_name") or model_id,
                        "description": model.get("model_desc") or "",
                        "context_window": params.get("context_window"),
                        "max_tokens": params.get("max_tokens"),
                        "supports_images": params.get("supports_images", False),
                        "enable_queue": params.get("enable_queue", False),
                    }
                )
            if discovered:
                # 主 Agent 已拿到完整模型列表，避免不必要请求。
                break

        if not discovered:
            return False
        # benefit 免费模型不在 AgentCenter 列表里，从 opengw 网关动态同步；
        # 同步失败时回退到 BENEFIT_MODELS 兜底，保证 /v1/models 可见。
        benefit_models = _refresh_benefit_models()
        if not benefit_models:
            benefit_models = [
                {"id": model_id, "channel": "benefit", "object": "model",
                 "owned_by": "codearts", "name": model_id}
                for model_id in BENEFIT_MODELS
            ]
        known = {item["id"] for item in discovered}
        for item in benefit_models:
            if item["id"] not in known:
                discovered.append(item)
        SUPPORTED_MODELS = [item["id"] for item in discovered]
        MODEL_DETAILS = {item["id"]: item for item in discovered}
        AGENT_MODELS.clear()
        AGENT_MODELS.extend(
            item["id"] for item in discovered if item.get("channel") == "agent"
        )
        _save_model_cache(discovered)
        print("已自动同步模型:", ", ".join(SUPPORTED_MODELS))
        return True
    except (requests.RequestException, ValueError, TypeError, KeyError, OSError) as error:
        print("自动同步模型失败，将使用缓存或兜底列表:", error)
        return False


def _refresh_models_if_needed() -> None:
    """模型列表短时缓存，避免 Cherry Studio 频繁轮询时重复请求。"""
    # 进程刚启动时先把缓存详情装载到内存；不能只依赖兜底模型名。
    if not MODEL_DETAILS:
        _load_model_cache()
    try:
        age = time.time() - os.path.getmtime(MODEL_CACHE_PATH)
    except OSError:
        age = float("inf")
    if age > 300:
        if not _refresh_models():
            _load_model_cache()


def _normalize_finish_reason(value):
    """把 CodeArts 的结束原因转换为 OpenAI 客户端能识别的值。"""
    if value in (None, "other"):
        return "stop"
    return value


def _parse_sse_bytes(raw: bytes):
    """把上游 SSE 聚合为一个 OpenAI chat.completion JSON。"""
    text_parts = []
    reasoning_parts = []
    tool_calls = {}
    result = {
        "id": None,
        "object": "chat.completion",
        "created": None,
        "model": None,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": ""},
            "finish_reason": "stop",
        }],
    }
    usage = None
    last_finish_reason = None

    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith(b"data:"):
            continue
        payload = line[5:].lstrip()
        if payload == b"[DONE]":
            continue
        try:
            chunk = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

        # CodeArts 可能以 HTTP 200 返回业务错误，需要保留错误信息。
        if chunk.get("error_code") or chunk.get("error_msg"):
            return {
                "error": {
                    "message": chunk.get("error_msg") or "CodeArts upstream error",
                    "type": "upstream_error",
                    "code": chunk.get("error_code"),
                }
            }

        for key in ("id", "created", "model"):
            if chunk.get(key) is not None:
                result[key] = chunk[key]
        if chunk.get("usage"):
            usage = chunk["usage"]

        for choice in chunk.get("choices") or []:
            reason = choice.get("finish_reason")
            if reason is not None:
                last_finish_reason = _normalize_finish_reason(reason)
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                current = tool_calls.setdefault(index, {
                    "id": call.get("id"),
                    "type": call.get("type", "function"),
                    "function": {"name": "", "arguments": ""},
                })
                if call.get("id"):
                    current["id"] = call["id"]
                if call.get("type"):
                    current["type"] = call["type"]
                function = call.get("function") or {}
                if function.get("name"):
                    current["function"]["name"] += function["name"]
                if function.get("arguments"):
                    current["function"]["arguments"] += function["arguments"]

    message = result["choices"][0]["message"]
    message["content"] = "".join(text_parts)
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    result["choices"][0]["finish_reason"] = last_finish_reason or ("tool_calls" if tool_calls else "stop")
    if usage:
        result["usage"] = usage
    return result


def _parse_upstream_json(upstream):
    """兼容上游普通 JSON 和错误地返回 SSE 的情况。"""
    raw = upstream.content
    try:
        result = json.loads(raw.decode("utf-8"))
        if isinstance(result, dict) and result.get("choices"):
            for choice in result["choices"]:
                choice["finish_reason"] = _normalize_finish_reason(choice.get("finish_reason"))
        return result
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _parse_sse_bytes(raw)


def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.after_request
def after_request(resp: Response) -> Response:
    return _cors(resp)


def _model_payload(model_id: str) -> dict:
    """生成 OpenAI 模型对象，并附带常见能力元数据。"""
    detail = MODEL_DETAILS.get(model_id, {})
    context_window = detail.get("context_window")
    max_tokens = detail.get("max_tokens")
    supports_images = bool(detail.get("supports_images", False))
    payload = {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "codearts",
        "name": detail.get("name") or model_id,
        "description": detail.get("description") or "",
        # 常见 OpenAI 兼容客户端会读取其中一部分；未知字段会被安全忽略。
        "context_window": context_window,
        "context_length": context_window,
        "max_tokens": max_tokens,
        "max_output_tokens": max_tokens,
        "supports_images": supports_images,
        "vision": supports_images,
        "supports_vision": supports_images,
        "supports_function_calling": True,
        "supports_tool_calling": True,
        "supports_reasoning": True,
        "input_modalities": ["text", "image"] if supports_images else ["text"],
        "output_modalities": ["text"],
        "supported_parameters": [
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "tools",
            "tool_choice",
            "response_format",
        ],
    }
    return {key: value for key, value in payload.items() if value is not None}


@app.route("/v1/models", methods=["GET", "OPTIONS"])
def list_models():
    if request.method == "OPTIONS":
        return _cors(Response(""))
    _refresh_models_if_needed()
    return jsonify({
        "object": "list",
        "data": [_model_payload(model_id) for model_id in SUPPORTED_MODELS],
    })


@app.route("/v1/models/<path:model_id>", methods=["GET", "OPTIONS"])
def get_model(model_id):
    """提供标准 OpenAI 单模型详情接口，方便客户端二次读取能力。"""
    if request.method == "OPTIONS":
        return _cors(Response(""))
    _refresh_models_if_needed()
    if model_id not in SUPPORTED_MODELS:
        return _cors(jsonify({
            "error": {
                "message": f"model '{model_id}' not found",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }
        })), 404
    return jsonify(_model_payload(model_id))


@app.route("/healthz", methods=["GET"])
def healthz():
    """并发闸门的运行时状态，用于排查「请求为什么在排队」。"""
    return jsonify({
        "status": "ok",
        "models": len(SUPPORTED_MODELS),
        "concurrency": GATE.stats(),
    })


@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
def chat_completions():
    if request.method == "OPTIONS":
        return _cors(Response(""))

    body = request.get_data()
    if not body:
        return _cors(jsonify({"error": {"message": "empty body"}})), 400
    try:
        req_json = json.loads(body)
    except Exception:
        return _cors(jsonify({"error": {"message": "invalid JSON body"}})), 400

    # 模型名归一化；请求聊天时也按短缓存周期同步一次模型列表
    _refresh_models_if_needed()
    model = req_json.get("model", SUPPORTED_MODELS[0])
    if model not in SUPPORTED_MODELS:
        return (
            _cors(
                jsonify(
                    {
                        "error": {
                            "message": f"model '{model}' 不在当前账号可用模型列表中",
                            "type": "invalid_request_error",
                            "param": "model",
                            "code": "model_not_found",
                        }
                    }
                )
            ),
            404,
        )
    req_json["model"] = model

    # 通道判断：AgentCenter 老套餐模型用基础签名；其余（免费 benefit 模型）
    # 必须带 maas_type/model-id/model-name 三个签名头，否则上游报未注册。
    benefit = model not in AGENT_MODELS

    # 客户端是否要流式
    want_stream = bool(req_json.get("stream", False))

    # 重新序列化 body（确保与签名时一致）
    body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
    headers = _hmac_headers(body, benefit=benefit, model_id=model)

    # 并发闸门：上游超限是硬拒绝而非排队，必须由代理先削峰。
    # 排不到槽位就返回 429 + Retry-After，让客户端退避重试，
    # 而不是把注定被上游拒绝的请求发出去。
    if not GATE.acquire(QUEUE_TIMEOUT):
        return _rate_limited_response(
            f"代理侧并发排队超时（上限 {MAX_CONCURRENCY}，已等待 {QUEUE_TIMEOUT:g}s）",
            retry_after=SESSION_COOLDOWN or 5,
        )

    try:
        upstream = requests.post(
            TARGET, data=body, headers=headers, stream=True, timeout=600
        )
    except Exception as e:
        # 请求没建立起来，上游不会为此占用会话，直接归还槽位、不进入冷却
        GATE.release()
        return _cors(jsonify({"error": {"message": f"upstream error: {e}"}})), 502

    if upstream.status_code != 200:
        try:
            detail = upstream.text[:2000]
        except Exception:
            detail = ""
        # 上游直接拒绝（含并发会话已达上限），不会占用会话额度，立即归还槽位
        GATE.release()
        return (
            _cors(
                jsonify(
                    {
                        "error": {
                            "message": f"upstream {upstream.status_code}: {detail}"
                        }
                    }
                )
            ),
            502,
        )

    if want_stream:
        def generate():
            """确保槽位覆盖整个流式响应，结束后按 SESSION_COOLDOWN 进入冷却。"""
            try:
                for chunk in _stream_body():
                    yield chunk
            finally:
                GATE.release(SESSION_COOLDOWN)

        def _stream_body():
            """严格转发为 OpenAI SSE；仅对 benefit 流补齐缺失的结束原因。"""
            content_type = (upstream.headers.get("Content-Type") or "").lower()
            is_benefit = model not in AGENT_MODELS
            saw_done = False
            saw_finish_reason = False

            # 上游偶尔会在请求 stream=true 时返回普通 JSON，包装成单个 SSE 帧。
            if "text/event-stream" not in content_type:
                result = _parse_upstream_json(upstream)
                for choice in result.get("choices") or []:
                    if choice.get("finish_reason") in (None, "other"):
                        choice["finish_reason"] = "stop"
                payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                yield b"data: " + payload + b"\n\n"
                yield b"data: [DONE]\n\n"
                return

            for raw_line in upstream.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                if not raw_line.startswith(b"data:"):
                    if raw_line.startswith(b":"):
                        yield raw_line + b"\n\n"
                    continue

                payload = raw_line[5:].strip()
                if payload == b"[DONE]":
                    # benefit 流需要先确认是否缺少 finish_reason；agent 流则保持
                    # 原来的即时转发行为，避免改变 agent 客户端状态机。
                    if not is_benefit:
                        if not saw_done:
                            yield b"data: [DONE]\n\n"
                            saw_done = True
                    else:
                        saw_done = True
                    continue
                try:
                    chunk = json.loads(payload.decode("utf-8"))
                    for choice in chunk.get("choices") or []:
                        reason = choice.get("finish_reason")
                        if reason is not None:
                            saw_finish_reason = True
                            if reason == "other":
                                choice["finish_reason"] = "stop"
                        # DeepSeek benefit 流会发送 content:null、reasoning_content 有值。
                        # 部分 Agent Runtime 只接受字符串 content，会将 null chunk
                        # 误判为无响应；OpenAI 兼容格式中空字符串表达同样语义。
                        delta = choice.get("delta")
                        if isinstance(delta, dict) and delta.get("content") is None:
                            delta["content"] = ""
                    payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    yield b"data: " + payload + b"\n\n"
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue

            # 只对免费 benefit 模型补齐完全缺失的 finish_reason；不重发任何上游 chunk。
            if is_benefit and not saw_finish_reason:
                yield b"data: " + json.dumps(
                    {
                        "id": "proxy-finish",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8") + b"\n\n"
            if is_benefit and not saw_done:
                yield b"data: [DONE]\n\n"
            elif is_benefit:
                # 上游 [DONE] 被延迟到这里，确保补块在 [DONE] 之前。
                yield b"data: [DONE]\n\n"
            # agent 分支保持原有行为：如果上游异常断流、没有发送 [DONE]，
            # 代理仍补一个 [DONE]，与修改前的实现一致。
            if not is_benefit and not saw_done:
                yield b"data: [DONE]\n\n"

        resp = Response(generate(), status=200, mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        resp.headers["X-Accel-Buffering"] = "no"
        return _cors(resp)

    # 非流式：无论上游返回 JSON 还是 SSE，都统一聚合成单个 JSON。
    # CodeArts 在排队、重试或特定模型场景下可能即使请求 stream=false
    # 仍返回 text/event-stream；直接转发会导致客户端把 data: 当作 JSON 解析。
    try:
        result = _parse_upstream_json(upstream)
    finally:
        # 上游已 200，会话已建立，必须进入冷却
        GATE.release(SESSION_COOLDOWN)
    if isinstance(result, dict) and result.get("error"):
        resp = Response(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            status=502,
            content_type="application/json; charset=utf-8",
        )
        return _cors(resp)
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    resp = Response(output, status=200, content_type="application/json; charset=utf-8")
    return _cors(resp)


if __name__ == "__main__":
    # 启动时先同步一次真实模型列表（失败则回退到缓存/兜底列表），再打印。
    if not _refresh_models():
        _load_model_cache()
    print(f"CodeArts OpenAI 兼容代理已启动: http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"OpenAI Base URL: http://{LISTEN_HOST}:{LISTEN_PORT}/v1")
    print("可用模型:", SUPPORTED_MODELS)
    print(
        f"并发闸门: 上限 {MAX_CONCURRENCY}，排队超时 {QUEUE_TIMEOUT:g}s，"
        f"会话冷却 {SESSION_COOLDOWN:g}s（查看 /healthz）"
    )
    print("按 Ctrl+C 停止")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True)
