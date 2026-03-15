# GrsaiProxyManager

一个基于 FastAPI 的 API Key 代理管理服务，透明代理所有请求至 [grsai.com](https://grsai.com)，通过 Round-robin 策略管理多个 API Key 池，自动跳过积分不足的 Key。

## 功能特性

- **透明代理**：完整转发 method / path / query / headers / body，原样返回上游响应
- **Round-robin 轮询**：公平分发请求至所有可用 Key
- **积分感知**：根据模型自动扣除对应积分，失败/违规自动返还
- **持久化缓存**：积分和状态写入 `keys_cache.json`，重启后无需重新检测
- **定时刷新**：后台每 5 分钟并发检测所有 Key 余额
- **Web 管理界面**：Key 增删查、积分筛选、分页、明暗主题
- **登录保护**：管理界面需要密码验证，支持修改密码
- **画图界面**：内置 Nano Banana 绘图客户端，支持图片保存、下载

## 快速开始

### 方式一：直接运行

#### 1. 克隆仓库

```bash
git clone https://github.com/lqhxxihx/GrsaiProxyManager.git
cd GrsaiProxyManager
```

#### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> **默认密码**：`admin123456`，首次部署后请立即修改

#### 3. 启动服务

```bash
uvicorn main:app --port 1515
```

---

### 方式二：Docker 部署

```bash
git clone https://github.com/lqhxxihx/GrsaiProxyManager.git
cd GrsaiProxyManager
docker compose up -d
```

或手动构建：

```bash
docker build -t grsai-proxy .
docker run -d -p 1515:1515 --env-file .env --name grsai-proxy grsai-proxy
```

## 更新

### 本地运行更新

```bash
# 拉取远端全部更新，并删除本地多余文件（保持与仓库完全一致）
git fetch --all
git reset --hard origin/master
git clean -fdx
uvicorn main:app --port 1515
```

### Docker 更新

```bash
# 拉取远端全部更新，并删除本地多余文件（保持与仓库完全一致）
git fetch --all
git reset --hard origin/master
git clean -fdx
docker compose up -d --build
```

## 访问地址

| 地址 | 说明 |
|------|------|
| `http://localhost:1515/ui/` | 画图界面 |
| `http://localhost:1515/ui/admin/` | Key 管理界面（需登录） |
| `http://localhost:1515/ui/admin/login` | 管理员登录页 |

## 代理使用方式

将原来请求 `https://grsaiapi.com` 的地址改为本服务地址，API Key 填写管理员密码：

```bash
curl -X POST http://localhost:1515/v1/draw/nano-banana \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer your_admin_password' \
  -d '{"model": "nano-banana", "prompt": "a cute cat"}'
```

## 管理 API

> 所有管理 API 需要登录 session cookie

| Method | Path | 说明 |
|--------|------|------|
| `GET` | `/admin/keys` | 查看所有 Key 状态 |
| `POST` | `/admin/keys` | 批量添加 Key |
| `DELETE` | `/admin/keys/{hint}` | 删除 Key |
| `POST` | `/admin/keys/refresh` | 刷新全部 Key 积分 |
| `POST` | `/admin/keys/refresh-subset` | 刷新指定 Key 积分 |
| `POST` | `/admin/login` | 登录 |
| `POST` | `/admin/logout` | 退出 |
| `POST` | `/admin/change-password` | 修改密码 |
| `POST` | `/admin/credits-summary` | 获取积分汇总（需密码） |

## 项目结构

```
GrsaiProxyManager/
├── main.py              # FastAPI 入口，路由，认证
├── key_manager.py       # API Key 管理：轮询、余额检测、持久化
├── proxy.py             # 透明代理逻辑：转发请求至上游
├── model_credits.py     # 模型积分消耗映射表
├── config.py            # 配置读取
├── static/              # 前端静态文件
│   ├── index.html       # 画图界面
│   ├── script.js        # 画图逻辑
│   └── admin/           # 管理界面
│       ├── index.html
│       └── login.html
├── .env                 # 环境变量配置
├── requirements.txt
└── README.md
```

## 支持的模型及积分消耗

| 模型 | 积分/次 |
|------|--------|
| nano-banana-pro | 1800 |
| nano-banana-2 | 1300 |
| nano-banana-pro-vt | 1800 |
| nano-banana-fast | 440 |
| nano-banana | 1400 |

## License

MIT
