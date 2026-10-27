# -*- coding: utf-8 -*-
"""通过代理验证并发闸门是否生效。

对比口径：
    - 改动前：N 个并发直打上游 → 超限的请求收到 TM.00001041，直接失败。
    - 改动后：N 个并发打代理 → 代理排队削峰，全部成功，只是耗时被拉长。

用法：
    python test_gate.py            # 默认 6 并发
    python test_gate.py 8          # 8 并发
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"


def gate_stats():
    with urllib.request.urlopen(BASE + "/healthz", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))["concurrency"]


def one(index, model, barrier, results):
    payload = json.dumps({
        "model": model,
        "stream": False,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "只回复：ok"}],
    }).encode("utf-8")
    request = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    barrier.wait(timeout=30)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=320) as response:
            status = response.status
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        status = "ERR"
        raw = f"{type(error).__name__}: {error}"
    results[index] = {
        "status": status,
        "elapsed": time.monotonic() - started,
        "body": raw[:300],
    }


def main(argv):
    n = int(argv[1]) if len(argv) > 1 else 6
    model = argv[2] if len(argv) > 2 else "deepseek-v4.1-flash"

    print("发起前闸门状态:", gate_stats())
    barrier = threading.Barrier(n)
    results = {}
    threads = [
        threading.Thread(target=one, args=(i, model, barrier, results))
        for i in range(n)
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=340)
    wall = time.monotonic() - started

    print(f"\n{n} 个并发请求，总墙钟 {wall:.1f}s")
    print("{:<4}{:<8}{:>10}  {}".format("#", "状态", "耗时(s)", "响应片段"))
    print("-" * 78)
    upstream_rejects = 0
    for index in sorted(results):
        item = results[index]
        if "TM.00001041" in item["body"] or "81101" in item["body"]:
            upstream_rejects += 1
        print("{:<4}{:<8}{:>10.1f}  {}".format(
            index, item["status"], item["elapsed"], item["body"][:60]))

    print("\n发起后闸门状态:", gate_stats())
    print(f"被上游并发上限拒绝的请求数: {upstream_rejects}")

    if upstream_rejects == 0 and all(isinstance(r["status"], int) and r["status"] == 200
                                     for r in results.values()):
        print("结论: 全部成功，闸门生效（超限请求被排队而不是被上游拒绝）。")
        return 0
    print("结论: 仍有请求失败，需要检查闸门上限或冷却时间设置。")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
