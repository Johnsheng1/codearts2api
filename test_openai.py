# -*- coding: utf-8 -*-
"""本地测试脚本：通过代理调用 CodeArts 模型。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8787/v1"


def test_models():
    with urllib.request.urlopen(BASE + "/models") as r:
        print("== GET /models ==")
        print(json.dumps(json.loads(r.read()), ensure_ascii=False, indent=2))


def test_chat():
    payload = {
        "model": "openpangu-2.0-pro",
        "messages": [{"role": "user", "content": "只回复：成功"}],
        "stream": False,
        "max_tokens": 64,
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        print("\n== POST /chat/completions ==")
        data = json.loads(r.read())
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    test_models()
    test_chat()
