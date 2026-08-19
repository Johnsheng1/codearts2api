# CodeArts OpenAI 兼容代理

把华为云 CodeArts 的模型接口（`openpangu-2.0-pro`）包装成**标准 OpenAI Chat Completions API**。

底层走华为云 `SDK-HMAC-SHA256` 请求签名，AK/SK 存放在 `.env`。

## 使用方法

### 1. 安装依赖

```cmd
cd /d C:\Users\johnsheng\Desktop\codearts-openai-proxy
pip install -r requirements.txt
```

### 2. 配置 AK/SK

编辑 `.env`：

```ini
CODEARTS_AK=你的AccessKey
CODEARTS_SK=你的SecretKey
```

> ⚠️ 当前 `.env` 中的密钥已在公开对话中出现过，**请立即到华为云 IAM 控制台删除并重新创建**，然后替换到 `.env`。

### 3. 启动代理

```cmd
python server.py
```

默认监听：

```text
http://127.0.0.1:8787
```

### 4. 在各类工具中接入

把 OpenAI 兼容 Base URL 指向：

```text
http://127.0.0.1:8787/v1
```

- API Key：任意非空字符串（例如 `codearts`）
- 模型：`openpangu-2.0-pro`

支持的工具示例：OpenCode、Cline、Continue、LobeChat、Cherry Studio 等所有支持自定义 OpenAI 兼容端点的工具。

## 本地测试

```cmd
python test_openai.py
```

## 接口说明

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 格式聊天补全，支持 `stream` 流式 |
| `GET /v1/models` | 模型列表 |

## 代码结构

```text
codearts-openai-proxy/
├── .env               # AK/SK（勿提交到公开仓库）
├── server.py          # 主服务：签名 + 转发
├── requirements.txt   # Python 依赖
├── test_openai.py     # 本地测试脚本
└── README.md
```

## 说明

- 代理只做签名和转发，不修改模型行为。
- 每次调用会消耗华为云 CodeArts 套餐额度（token）。
- 仅绑定 `127.0.0.1`，不会对外网开放。
