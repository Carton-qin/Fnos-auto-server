# 飞牛私有云 (fnOS) 自动化中枢控制台 (fnos-server)

专为**飞牛私有云 (fnOS / 飞牛 NAS)** 打造的全能型自动化任务调度、网络数据智能抓取与提炼、服务存活哨兵与即时消息推送平台。

本项目由原 ESP32-S3 单片机自动化中枢架构全面重构升级而来，彻底解放硬件算力瓶颈，搭载 **Playwright 无头 Chromium 智能双模爬虫**、**APScheduler 工业级调度引擎** 与 **SQLite 结构化数据库**，完美解决单页面动态渲染 (SPA)、Cloudflare 反爬风控拦截、内存限制与移动端长篇分卷推送痛点。

---

## 🌟 核心升级亮点

| 维度 | 原 ESP32-S3 单片机旧版 | 飞牛 fnOS 专属新版 (`fnos-server`) |
| :--- | :--- | :--- |
| **硬件算力** | 8MB 内存，单片机 CPU 受限 | **NAS 级 CPU + 海量内存**，支持无头浏览器深度渲染与长文本大模型智能提炼 |
| **网页抓取** | 裸 Socket HTTP (遇 302/Cloudflare 报错) | **智能双模：轻量异步 Httpx + Playwright 无头 Chromium 深度渲染** |
| **动态网页** | 无法执行 JavaScript，SPA 页面拿到空骨架 | **完整执行 React/Vue/Angular 客户端 JS 渲染**，轻松驾驭各类现代复杂 Web 页面 |
| **定时调度** | 自写循环轮询，时区易受 NTP 漂移 | **APScheduler (AsyncIOScheduler 工业级调度)**，原生 UTC+8 北京时间 |
| **数据持久化** | 易损 Flash，单个 JSON 文件并发覆写 | **SQLite + SQLAlchemy 2.0 ORM**，事务安全，持久化挂载至 `/app/data` |
| **网络端口** | 80 端口 (易与 NAS 冲突) | **专属端口 `8836:8836`** (可自由定义) |
| **历史兼容** | 无法平滑导入 | **内置一键导入旧版 `tasks.json` 工具**，零成本无缝迁移 |

---

## 🛠️ 核心架构与功能

1. **智能双模爬虫引擎 (`crawler.py`)**：
   - **第一级（轻量极速）**：针对普通 API 接口和静态页面，采用异步 `httpx` 发起轻量高并发请求；
   - **第二级（智能升阶）**：一旦检测到目标为动态渲染 SPA 单页面、或者返回 302/403/Cloudflare WAF 挑战，系统自动升阶调度 **Playwright Headless Chromium** 沙盒执行完整 JavaScript 渲染，获取真实完整的页面数据。
2. **Playwright 账号密码自动托管与会话持久化 (`StorageState`)**：
   - **告别手动提取 Cookie**：支持直接配置站点账号与密码，由无头浏览器自动模拟真实人类登录交互；
   - **持久化 StorageState**：登录成功后自动导出 Cookies 与 LocalStorage 至 `/app/data/sessions/{task_id}_state.json`，后续每日任务直接复用持久化会话，毫秒级快速通过；
   - **自愈式智能重登 (Self-Healing)**：若会话因服务端超时失效（触发 401/403 或重定向至登录页），调度器自动触发静默重登并刷新会话，任务永不掉线；
   - **凭据安全保护**：用户登录密码采用 AES-128/256 Fernet 强对称加密固化在 NAS 本地，API 接口与控制台前端自动进行掩码脱敏（`******`）。
3. **分级验证码破解与远程扫码交互 (`captcha.py`)**：
   - **第 1 级（本地极速识别）**：内置 **ddddocr 本地离线深度学习神经网络**，0 延迟、免外部 API 调用，秒级破解数字/英文字符/简单运算验证码；
   - **第 2 级（视觉大模型兜底）**：遇到扭曲变形或复杂图形，自动调用多模态 Vision LLM 进行图像认知回退破解；
   - **第 3 级（远程扫码与实时快照）**：针对微信公众号扫码关注登录或复杂 2FA，提供 Web 实时页面快照与扫码窗口，手机扫码后一键激活登录并持久化保存凭据。
4. **Cookie 与 JWT 智能续期与预警**：
   - 自动解析 Cookie 中的 JWT `exp` 过期时间戳；
   - 距离到期仅剩 $\le 3$ 天时，主动通过微信/飞书/钉钉推送预警通知；
   - 自动截获并续期服务端下发的 `Set-Cookie`。
5. **连续失败智能熔断保护**：
   - 单个任务连续失败 3 次自动触发熔断，挂起 6 小时定时调度，防止宽带 IP 被目标网站防火墙拉黑。
6. **大模型自动提炼与故障诊断 (`llm.py`)**：
   - 支持 DeepSeek、通义千问、Kimi、SiliconFlow、OpenAI 等标准 OpenAI 兼容协议；
   - 支持对抓取到的长文本进行智能提纯、多条目卡片化美化排版；
   - 任务失败时自动调用大模型生成专业的排查与修复建议。
7. **全渠道多终端消息即时推送 (`notifier.py`)**：
   - 支持 PushPlus (微信)、企业微信群机器人 (支持超长文本智能分卷切块，杜绝 4096 字节截断)、飞书机器人、钉钉机器人 (含加签)、Bark (iOS)、自定义 Webhook；
   - 内置夜间免打扰 (DND) 策略与紧急消息穿透。

---

## 🚀 飞牛 NAS (fnOS) 快速部署指南

### 方式一：飞牛应用中心 / Docker 容器管理部署（推荐）

1. 打开飞牛 NAS 管理面板，进入 **「Docker」** 或 **「应用中心」**；
2. 在 **Compose 编排** 中点击 **「新增项目」**；
3. 项目名称填写：`fnos-server`；
4. 将本工程目录下的 `docker-compose.yml` 内容粘贴进去：

```yaml
services:
  fnos-automation:
    build: .
    image: fnos-server:latest
    container_name: fnos-server
    restart: unless-stopped
    shm_size: '1gb'
    ports:
      - "8836:8836"
    volumes:
      - ./data:/app/data
      - ./app:/app/app
      - ./main.py:/app/main.py
    environment:
      - TZ=Asia/Shanghai
      - PYTHONUNBUFFERED=1
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

5. 点击 **「立即启动 / 构建」**，系统将自动拉取环境并构建运行；
6. 启动成功后，在浏览器中打开：
   ```text
   http://<你的飞牛NAS局域网IP>:8836
   ```

---

### 方式二：SSH 终端命令行一键运行

如果你的飞牛 NAS 开启了 SSH 访问：

```bash
# 1. 拷贝 / 克隆 fnos-server 文件夹到飞牛 NAS 存储卷 (如 /vol1/1000/dockerfiles/fnos-server)
cd /vol1/1000/dockerfiles/fnos-server

# 2. 一键构建并后台启动
sudo docker compose up -d --build

# 3. 查看实时日志
sudo docker compose logs -f
```

> 💡 **热更新支持**：服务原生支持代码目录热重载挂载。后续更新只需在终端执行 `git pull` 即可在秒级自动重载最新代码，前端刷新即生效，无需反复重新构建镜像。

---

### 方式三：本地开发环境直接运行 (Python 3.11+)

```bash
cd fnos-server

# 1. 安装依赖
pip install -r requirements.txt

# 2. 安装 Playwright Chromium 浏览器内核
playwright install chromium

# 3. 启动 FastAPI 本地服务
python main.py
```

---

## 🔐 首次登录与配置

1. **默认管理密码**：`admin`；
2. 首次登录后，推荐前往 **「🖥️ 飞牛与存储」** 选项卡修改管理员密码；
3. **API 在线调试文档 (Swagger UI)**：
   ```text
   http://<你的飞牛NAS局域网IP>:8836/api/docs
   ```

---

## 📥 从原 ESP32 单片机一键平滑迁移历史任务

如果您在原 ESP32 开发板上已有运行的定时任务和打卡配置：
1. 从原 ESP32 开发板导出的 `tasks.json`（或直接复制文件内容）；
2. 进入飞牛控制台，切换至 **「⏱️ 任务中心」** 标签；
3. 点击顶部的 **「📥 导入旧版 tasks.json」** 按钮；
4. 将 JSON 文本直接粘贴到输入框中，点击 **「确认导入并入库」**；
5. 系统将自动完成数据格式解析转换并写入 SQLite 数据库，同时自动在后台注册 APScheduler 定时调度！

---

## 📁 数据持久化与备份说明

所有持久化资产均存放在挂载卷 `./data` 中：
- `data/app.db`：SQLite 结构化数据库（包含全部任务定义、30天打卡战绩、运行审计日志）；
- `data/backups/`：全量快照备份；
- 您可在控制台点击 **「⬇️ 导出全量备份 JSON」** 随时将整个平台的数据打包下载保存。
