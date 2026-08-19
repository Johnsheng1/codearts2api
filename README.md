# CodeArts OpenAI 兼容代理

把华为云 CodeArts [https://codearts.huaweicloud.com/] 的模型接口（盘古开源大模型2.0、GLM-5.2）包装成**标准 OpenAI Chat Completions API**。

底层走华为云 `SDK-HMAC-SHA256` 请求签名，AK/SK 存放在 `.env`。

## 使用方法

### 1. 安装依赖

```cmd
pip install -r requirements.txt
```

### 2. 配置 AK/SK

编辑 `.env`：

```ini
CODEARTS_AK=你的AccessKey
CODEARTS_SK=你的SecretKey
```
#### 如何获取AK/SK(Access Key/Secret Key)
进入 https://console.huaweicloud.com/iam/#/mine/accessKey ，登录后-->新增访问密钥 即可获取

**请勿泄露你的AK/SK，这具有华为云账号的最高访问权限**

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
codearts2api/
├── .env               # AK/SK
├── server.py          # 主服务：签名 + 转发
├── requirements.txt   # Python 依赖
├── test_openai.py     # 本地测试脚本
└── README.md
```

## 说明

- 代理只做签名和转发，不修改模型行为。
- 每次调用会消耗华为云 CodeArts 套餐额度（token）。
- 仅绑定 `127.0.0.1`，不会对外网开放。
- 本项目及本项目作者与华为云平台及其附属公司无任何关系，且华为云平台中并未对本项目中调用方式明确提出允许，若造成封号/限流等任何因使用本工具造成的情况与项目作者无关
