# -*- coding: utf-8 -*-
"""CodeArts 并发上限探测脚本（只读探测，不修改 server.py）
============================================================

目的：在改 server.py 之前，先用真实流量回答三个问题：

    1. 超出上游并发阈值时，表现是「报错」还是「静默排队」？
    2. 阈值到底是 5 吗？计数口径是「账号级 / 通道级 / 模型级」？
    3. 流式响应在途期间是否真的占用并发名额？

做法：用屏障让 N 个请求在同一瞬间打向上游 /api/v2/chat/completions，
然后按时间轴还原「实际重叠的并发数」，与每个请求的报错/延迟对照。

用法（先确认 .env 里已配置 CODEARTS_AK / CODEARTS_SK）：

    python probe_concurrency.py --n 1                      # 冒烟，验证签名可用
    python probe_concurrency.py --models deepseek-v4-flash-0731 --n 6
    python probe_concurrency.py --models openpangu-2.0-pro --n 8
    python probe_concurrency.py --models A,B --n 8         # 混合通道，测是否共用同一池

注意：会真实消耗模型额度，N 越大消耗越多，建议从 6 起步。
"""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.parse

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

AK = os.getenv("CODEARTS_AK", "").strip()
SK = os.getenv("CODEARTS_SK", "").strip()
if not AK or not SK:
    raise SystemExit("缺少 CODEARTS_AK / CODEARTS_SK，请在 .env 中配置后重试")

HOST = "snap-access.cn-north-4.myhuaweicloud.com"
TARGET = f"https://{HOST}/api/v2/chat/completions"
CACHE_PATH = os.path.join(HERE, "models-cache.json")

DEFAULT_PROMPT = "请从 1 数到 400，只输出数字并用逗号分隔，不要任何解释。"


def _load_channel_map() -> dict:
    """从 models-cache.json 读模型所属通道；读不到就全部按 benefit 处理（与 server.py 一致）。"""
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as file:
            cached = json.load(file)
        return {
            item["id"]: item.get("channel", "agent")
            for item in cached.get("models", [])
            if item.get("id")
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {}


CHANNEL_MAP = _load_channel_map()


def sign_headers(body: bytes, benefit: bool, model_id: str) -> dict:
    """与 server.py 完全一致的 SDK-HMAC-SHA256 签名，避免探测结果被签名差异污染。"""
    sdk_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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

    canonical_uri = "/api/v2/chat/completions/"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = (
        f"POST\n{canonical_uri}\n\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        f"SDK-HMAC-SHA256\n{sdk_date}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    signature = hmac.new(SK.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"SDK-HMAC-SHA256 Access={AK}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def probe_one(index, model, barrier, prompt, max_tokens, timeout, results, t_origin):
    """发一条流式请求，记录关键时间点；异常也写入 results 便于统一对比。"""
    benefit = CHANNEL_MAP.get(model, "benefit") != "agent"
    record = {
        "index": index,
        "model": model,
        "channel": "benefit" if benefit else "agent",
        "status": None,
        "t_headers": None,
        "t_first": None,
        "t_last": None,
        "chunks": 0,
        "chars": 0,
        "error": None,
    }
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "max_tokens": max_tokens,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    response = None
    try:
        barrier.wait(timeout=30)
        headers = sign_headers(body, benefit=benefit, model_id=model)
        # 诊断用：强制关闭 TCP 连接。若不加这一行，读到 EOF 后连接会被放回连接池
        # 继续复用，上游可能据此判定会话仍存活，从而把"并发会话数"算在旧请求头上。
        # 该头不参与签名（未列入 SignedHeaders），不影响签名校验。
        headers["connection"] = "close"
        response = requests.post(
            TARGET, data=body, headers=headers, stream=True, timeout=timeout
        )
        record["status"] = response.status_code
        record["t_headers"] = time.monotonic() - t_origin
        record["t_last"] = record["t_headers"]

        if response.status_code != 200:
            record["error"] = (response.text or "")[:300].replace("\n", " ")
            results[index] = record
            return

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/event-stream" not in content_type:
            # 上游偶尔降级为普通 JSON，直接当作一个整体响应
            text = response.text
            record["t_first"] = time.monotonic() - t_origin
            record["t_last"] = record["t_first"]
            record["chunks"] = 1
            record["chars"] = len(text)
            try:
                obj = json.loads(text)
                if obj.get("error_code") or obj.get("error_msg"):
                    record["error"] = f"{obj.get('error_code')} {obj.get('error_msg')}"
            except ValueError:
                record["error"] = f"非 SSE 且非 JSON: {text[:200]}"
            results[index] = record
            return

        for line in response.iter_lines(decode_unicode=False):
            if not line or not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                # 不 break：继续读到 EOF。提前断开属于「异常终止」，
                # 上游可能因此把会话判定为仍存活，从而污染并发测量。
                # server.py 的生成器同样是读到上游 EOF 才结束。
                continue
            try:
                chunk = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if chunk.get("error_code") or chunk.get("error_msg"):
                record["error"] = f"{chunk.get('error_code')} {chunk.get('error_msg')}"
                break
            now = time.monotonic() - t_origin
            if record["t_first"] is None:
                record["t_first"] = now
            record["t_last"] = now
            record["chunks"] += 1
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                for key in ("content", "reasoning_content"):
                    value = delta.get(key)
                    if isinstance(value, str):
                        record["chars"] += len(value)
    except Exception as error:  # noqa: BLE001 - 探测脚本，任何异常都要记录而非中断
        record["error"] = f"{type(error).__name__}: {error}"[:300]
    finally:
        # 关键：显式关闭响应，及时释放底层 TCP 连接。
        # 若只 break 出 iter_lines 而不关闭，连接会留在池中，
        # 上游可能据此认为会话仍存活，从而污染并发测量结果。
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        record["t_closed"] = time.monotonic() - t_origin
    results[index] = record
    return


def timeline_overlap(records) -> list:
    """还原时间轴上的并发数曲线，返回 [(相对时间, 并发数), ...]。"""
    events = []
    for item in records:
        if item.get("t_headers") is None or item.get("t_last") is None:
            continue
        events.append((item["t_headers"], 1))
        events.append((item["t_last"], -1))
    events.sort(key=lambda pair: (pair[0], pair[1]))
    curve = []
    current = 0
    for moment, delta in events:
        current += delta
        curve.append((moment, current))
    return curve


def max_overlap(curve) -> int:
    return max((value for _, value in curve), default=0)


def run(models, n, prompt, max_tokens, timeout):
    results = {}
    t_origin = time.monotonic()
    barrier = threading.Barrier(n)
    threads = [
        threading.Thread(
            target=probe_one,
            args=(i, models[i % len(models)], barrier, prompt, max_tokens, timeout,
                  results, t_origin),
            daemon=True,
        )
        for i in range(n)
    ]
    print(f"发起 {n} 个并发请求，模型: {', '.join(models)}，max_tokens={max_tokens}")
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout + 60)

    wall = time.monotonic() - t_origin
    ordered = [results[i] for i in sorted(results)]

    print("\n{:<4}{:<26}{:<9}{:>10}{:>10}{:>10}{:>8}".format(
        "#", "模型", "状态", "响应头(s)", "首字节(s)", "结束(s)", "块数"))
    print("-" * 80)
    for item in ordered:
        fmt = lambda value: f"{value:.2f}" if isinstance(value, (int, float)) else "-"
        print("{:<4}{:<26}{:<9}{:>10}{:>10}{:>10}{:>8}".format(
            item["index"],
            item["model"][:25],
            item["status"] if item["status"] else "ERR",
            fmt(item["t_headers"]),
            fmt(item["t_first"]),
            fmt(item["t_last"]),
            item["chunks"],
        ))

    errors = [item for item in ordered if item["error"]]
    curve = timeline_overlap(ordered)
    peak = max_overlap(curve)

    print(f"\n总墙钟耗时: {wall:.2f}s")
    print(f"时间轴上的峰值并发: {peak}（成功建立连接并持续传输的请求数）")

    # 队列时长：响应头到手直到首个数据块，用来区分「上游排队」与「模型慢」
    waits = [
        item["t_first"] - item["t_headers"]
        for item in ordered
        if item.get("t_first") is not None and item.get("t_headers") is not None
    ]
    if waits:
        print(f"响应头→首字节: 最小 {min(waits):.2f}s / 最大 {max(waits):.2f}s")

    if errors:
        print(f"\n异常请求 {len(errors)} 个：")
        for item in errors:
            print(f"  #{item['index']} [{item['channel']}/{item['model']}] {item['error']}")
    else:
        print("\n无请求报错。")

    print("\n判读建议：")
    if errors and peak <= n - len(errors):
        print("  - 有请求直接报错且峰值并发 == 阈值 → 硬性并发上限，超限立即失败。")
        print("    改造方向：代理侧槽位 + 立即 429 + Retry-After。")
    elif peak >= 5 and max(waits or [0]) > 1.0:
        print("  - 无报错但部分请求首字节被显著推迟 → 上游存在排队闸门。")
        print("    改造方向：代理侧槽位 + keepalive 保活，避免被客户端判超时。")
    elif peak > 5:
        print(f"  - 峰值并发达到 {peak} 且无报错 → 该通道的限制弱于预期，")
        print("    需要再抬高 N（如 10、12）继续摸索真实阈值。")
    else:
        print("  - 数据不足，建议提高 N 或加长 max_tokens 重测。")
    return 0 if not errors else 1


def main(argv) -> int:
    parser = argparse.ArgumentParser(description="CodeArts 上游并发上限探测")
    parser.add_argument("--models", default="deepseek-v4-flash-0731",
                        help="逗号分隔的模型名；多个模型会轮流分配，用于测混合通道")
    parser.add_argument("--n", type=int, default=6, help="并发请求数（默认 6）")
    parser.add_argument("--max-tokens", type=int, default=1024, help="单请求输出上限")
    parser.add_argument("--timeout", type=int, default=300, help="单请求超时秒数")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="提示词")
    args = parser.parse_args(argv[1:])

    models = [item.strip() for item in args.models.split(",") if item.strip()]
    if not models:
        raise SystemExit("--models 不能为空")
    if args.n < 1:
        raise SystemExit("--n 至少为 1")
    for model in models:
        channel = CHANNEL_MAP.get(model, "benefit")
        print(f"模型 {model} → 通道 {channel}")
    print()
    return run(models, args.n, args.prompt, args.max_tokens, args.timeout)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
