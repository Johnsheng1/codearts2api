# CodeArts OpenAI 兼容代理

把华为云 CodeArts [https://codearts.huaweicloud.com/] 的模型接口（盘古开源大模型2.0、GLM-5.2、GLM-5.3、deepseek-flash-v4,deepseek-flash-v4.1,deepseek-v4-pro,Qwen）包装成**标准 OpenAI Chat Completions API**。

> [!IMPORTANT]
> 具体可用模型以程序自动同步的最新模型为准！！！

> [!TIP]
> 本工具无视华为云模型请求的高峰期排队策略，可以直接插队获得模型调用

底层走华为云 `SDK-HMAC-SHA256` 请求签名，AK/SK 存放在 `.env`。
## 新增免费层级模型，可使用benefit.py一键签到并通过server.py调用
> [!TIP]
> 建议使用opencode，pi，hermes，openclaw作为智能体接入。免费codearts具有5并发限制，其他代码智能体通常会超过这个限制，从而影响体验

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
> [!CAUTION]
> ​ 👇👇👇

> [!CAUTION]
> ​免责声明与使用条款
> 
> ​一、 权属与独立性
> 
> 本项目属于独立技术研究与开发作品。文中涉及的“华为云”及相关商标、服务名称均归其各自版权所有者所有。本项目与华为云及其关联公司无任何商业合作、授权或官方绑定关系。
> 
> ​二、 用途与风险自担
> 
> 本项目提供的调用机制未获得华为云官方的明示或暗示许可。所有功能仅供技术测试与学习探讨，请勿用于生产环境或商业用途。任何因使用本工具导致的账号被封禁、API 请求限流、数据泄露、业务受损或其他任何形式的损失，均由使用者自行承担全部责任。
> 
> 三、 无担保与责任限制
> 
> 本项目按“原样”（As-Is）提供，不提供任何明示或暗示的保证（包括但不限于适销性、特定用途适用性或不侵权保证）。作者不对因使用本代码引发的任何诉讼、索赔或纠纷承担责任。
> 
> ​四、 同意与生效
> 
> 任何使用、分发或修改本项目的行为，均被视为已无条件接受本声明的所有内容。如对本声明有异议，请立即销毁并停止使用本项目的所有资料。
>
> 条款变更：作者保留随时修改、更新本免责声明及使用条款的权利，且无需另行通知。修改后的条款一经发布即自动生效，在变更后继续使用本项目即视为您接受相关修改。

# 社区
感谢[LinuxDO](https://linux.do/)对本项目进行推广
