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
import datetime
import hashlib
import hmac
import json
import os

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

load_dotenv()

AK = os.getenv("CODEARTS_AK", "").strip()
SK = os.getenv("CODEARTS_SK", "").strip()
if not AK or not SK:
    raise SystemExit("缺少 CODEARTS_AK / CODEARTS_SK，请在 .env 中配置后重试")

# CodeArts 目标端点（从抓包确认）
TARGET = "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions"
HOST = "snap-access.cn-north-4.myhuaweicloud.com"
SUPPORTED_MODELS = ["openpangu-2.0-pro", "openpangu-2.0-flash", "GLM-5.2"]

app = Flask(__name__)


def _hmac_headers(body: bytes) -> dict:
    """构造华为云 SDK-HMAC-SHA256 签名请求头（仅适用于 POST /api/v2/chat/completions）。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    sdk_date = now.strftime("%Y%m%dT%H%M%SZ")

    # 注意：华为云签名规范要求消息头名称一律转小写再参与签名
    headers = {
        "host": HOST,
        "content-type": "application/json",
        "x-sdk-date": sdk_date,
    }

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


def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.after_request
def after_request(resp: Response) -> Response:
    return _cors(resp)


@app.route("/v1/models", methods=["GET", "OPTIONS"])
def list_models():
    if request.method == "OPTIONS":
        return _cors(Response(""))
    data = {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 0, "owned_by": "codearts"}
            for m in SUPPORTED_MODELS
        ],
    }
    return jsonify(data)


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

    # 模型名归一化
    model = req_json.get("model", SUPPORTED_MODELS[0])
    if model not in SUPPORTED_MODELS:
        model = SUPPORTED_MODELS[0]
    req_json["model"] = model

    # 客户端是否要流式
    want_stream = bool(req_json.get("stream", False))

    # 重新序列化 body（确保与签名时一致）
    body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
    headers = _hmac_headers(body)

    try:
        upstream = requests.post(
            TARGET, data=body, headers=headers, stream=True, timeout=600
        )
    except Exception as e:
        return _cors(jsonify({"error": {"message": f"upstream error: {e}"}})), 502

    if upstream.status_code != 200:
        try:
            detail = upstream.text[:2000]
        except Exception:
            detail = ""
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
            """转发 SSE，并把 CodeArts 的结束原因规范成 OpenAI 常见值。"""
            saw_terminal = False
            for raw_line in upstream.iter_lines(decode_unicode=False):
                if not raw_line:
                    yield b"\n"
                    continue
                # 保留 SSE 注释/非 data 行
                if not raw_line.startswith(b"data:"):
                    yield raw_line + b"\n"
                    continue
                prefix, payload = raw_line[:5], raw_line[5:].lstrip()
                if payload == b"[DONE]":
                    saw_terminal = True
                    yield b"data: [DONE]\n\n"
                    continue
                try:
                    chunk = json.loads(payload.decode("utf-8"))
                    choices = chunk.get("choices") or []
                    for choice in choices:
                        reason = choice.get("finish_reason")
                        # CodeArts/部分模型可能返回 other 或空值；Cherry Studio
                        # 只接受 stop、length、tool_calls、content_filter 等标准值。
                        if reason == "other":
                            choice["finish_reason"] = "stop"
                            saw_terminal = True
                    payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    yield b"data:" + payload + b"\n\n"
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # 非 JSON 数据原样传递，避免破坏错误事件
                    yield raw_line + b"\n"
            if not saw_terminal:
                yield b"data: [DONE]\n\n"

        resp = Response(generate(), status=200, mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        resp.headers["X-Accel-Buffering"] = "no"
        return _cors(resp)

    # 非流式：规范化结束原因并显式使用 UTF-8，避免客户端报 other
    try:
        result = upstream.json()
        for choice in result.get("choices") or []:
            if choice.get("finish_reason") == "other":
                choice["finish_reason"] = "stop"
        output = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, json.JSONDecodeError):
        output = upstream.content
    resp = Response(output, status=200, content_type="application/json; charset=utf-8")
    return _cors(resp)


if __name__ == "__main__":
    print("CodeArts OpenAI 兼容代理已启动: http://127.0.0.1:8787")
    print("OpenAI Base URL: http://127.0.0.1:8787/v1")
    print("可用模型:", SUPPORTED_MODELS)
    print("按 Ctrl+C 停止")
    app.run(host="127.0.0.1", port=8787, threaded=True)
