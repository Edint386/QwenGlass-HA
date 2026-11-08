# QwenGlass-HA：用千问 AI 眼镜控制 Home Assistant 里的米家设备

对眼镜直接说“把空调打开”“制冷 24 度，风力调到最大”“屋里多少度”，不用先打开什么应用。平台的大模型听懂这句话，调用这个服务的 MCP 工具；服务通过 Home Assistant 控制设备，再回一句结果让眼镜念出来。

## 能做什么

| | 例子 |
|---|---|
| 开关 | “开空调”“把灯都关了”“关挂灯”，几台设备可以一起说 |
| 空调 | 温度（“调到 26 度”“高一点”）、模式（制冷、制热、除湿、送风、自动）、风速；一句话里的几件事一次做完 |
| 灯 | 亮度（“调到 50”“亮一点”“暗一点”） |
| 净化器、风扇 | 风速（“净化器开到最大”） |
| 扫地机 | “开始扫地”“回去充电” |
| 查状态 | “空调开着吗”“空调现在多少度”“屋里多少度”“湿度多少”“家里设备都什么状态” |
| 其他 | 上面做不到的，大模型可以直接调 HA 的服务，比如执行场景、调音箱音量 |

## 怎么工作

```text
眼镜 ── 千问开放平台（大模型）──HTTPS──► glasshome（MCP 工具）──WebSocket──► Home Assistant ── 米家云 ── 设备
```

- 平台上是一个 MCP 插件加一个技能。技能告诉大模型什么话该调哪个工具、参数怎么填；插件选「无状态协议」，每调一次工具就是一个 POST，服务校验 Bearer Token。
- 三个工具：`control`（开关、温度、模式、风速、亮度、扫地机）、`status`（状态、室温、湿度）、`call_service`（兜底，直接调 HA 的服务）。
- 设备名单写在 `control` 的工具描述里（从 `devices.toml` 生成），大模型不用先查一轮有哪些设备。大模型传来的名字不用一字不差，“卧室空调”“净化”“所有灯”都认得；认不出来就回“家里没有叫……的设备，能控制的有：……”。
- 服务和 HA 一直连着 WebSocket，所有设备的状态都订阅在内存里。查状态不用等设备；已经是那个状态就不发命令，直接回“已经开着了”。
- 平台等一个工具大约 3 秒。设备 2.5 秒还没回音，服务先回“……没有回应，可能离线了”，命令在后台照样做完。
- 返回扁平的 JSON，`message` 是一句能直接念的话。技能要求大模型原样念，不加别的话。

我们实测“把空调打开”：从平台召回技能到开始播报约 2.5 秒，其中工具这一轮 0.67 秒（服务端 0.41 秒）。服务和 HA 放在同一台国内服务器、米家账号在中国大陆区时最快，每条命令只走“服务器 → 米家云”这一段。

| 路径 | 内容 |
|---|---|
| `glasshome/` | Python 源码：`mcp_server.py`（MCP 和 Bearer 校验）、`tools.py`（认设备、合并参数、3 秒兜底）、`control.py`（合成最少的 HA 调用）、`ha.py`（HA 的 WebSocket 客户端）、`devices.py`（设备名录）、`cli.py`（命令行） |
| `skill/SKILL.md` | 平台上技能的内容 |
| `deploy/` | `Dockerfile`、`compose.yaml`（HA + 本服务）、`devices.example.toml`、反向代理配置示例 `site.conf` |

`agent.py` 和 `command.py` 是早先的「Agent 自定义接入」版：只认固定句式、不经过大模型，最快，但说法稍微一变就认不出来。代码留着，下面的部署不用它；`.env` 里不填 `GLASSHOME_AGENT_TOKEN` 就不开。测试没有放进这个仓库。

## 部署指南

要准备这些：

- 一台公网服务器，装好 Docker；一个指向它的域名和正规证书（Let's Encrypt 就行）。平台只收域名，不收 IP。
- 米家账号。HA 用小米官方的 Xiaomi Home 集成接米家。已经有 HA 的话可以接你自己的，没有的话 compose 里带了一个。
- 一个[千问开放平台](https://device.qianwen.com)账号，一副千问 AI 眼镜 S1。

### 第一步：启动服务

1. 在仓库根目录复制 `.env.example` 为 `.env`，权限设成 600。先填 `GLASSHOME_MCP_TOKEN`，一串随机字符，比如：

   ```bash
   openssl rand -hex 24
   ```

2. 复制设备说法的示例，第三步再改：

   ```bash
   cp deploy/devices.example.toml deploy/devices.toml
   ```

3. 建服务的数据目录（容器里用 10001 号用户跑）：

   ```bash
   sudo install -d -o 10001 -g 10001 deploy/data
   ```

4. 启动：

   ```bash
   cd deploy && docker compose up -d --build
   ```

   起两个容器：`glasshome-ha`（HA，`127.0.0.1:8123`）和 `glasshome-voice`（本服务，MCP 在 `127.0.0.1:19531`），都只监听本机。

   - 已经有 HA：删掉 `compose.yaml` 里的 `homeassistant` 这一段和 `voice` 的 `depends_on`，把 `.env` 里的 `GLASSHOME_HA_URL` 改成 `ws://你的HA地址:8123/api/websocket`。
   - 服务器在国外：Dockerfile 和 compose 默认用腾讯云的 PyPI 镜像，改法见两个文件开头的注释。

### 第二步：接上 Home Assistant

1. HA 的网页只开在服务器本机，用 SSH 隧道打开：

   ```bash
   ssh -N -L 8123:127.0.0.1:8123 你的服务器
   ```

   浏览器打开 `http://localhost:8123`，完成初始化。
2. 按 [XiaoMi/ha_xiaomi_home](https://github.com/XiaoMi/ha_xiaomi_home) 的说明装上 Xiaomi Home 集成，登录米家账号，设备就会出现在 HA 里。
3. 在 HA 左下角点你的用户名 → 安全 → 长期访问令牌 → 创建令牌，填进 `.env` 的 `GLASSHOME_HA_TOKEN`，再重建本服务：

   ```bash
   docker compose up -d voice
   ```

### 第三步：写设备说法

1. 列出 HA 里的设备：

   ```bash
   docker exec glasshome-voice python -m glasshome.cli devices
   ```

2. 把常用设备的实体 ID 和你想叫的短名写进 `deploy/devices.toml`，写法看文件里的注释。温湿度计也写进去，就能问“屋里多少度”。
3. 重启本服务，让工具描述里的设备名单更新：

   ```bash
   docker compose restart voice
   ```

4. 不用眼镜先试一下（会真的开空调）：

   ```bash
   docker exec glasshome-voice python -m glasshome.cli tool control '{"device": "空调", "power": "on"}'
   ```

### 第四步：反向代理

把 `deploy/site.conf` 里的两个 `location` 加进你已有的 HTTPS 站点（nginx）的 `server` 块，重载 nginx。

- 不要开 HTTP/3，也不要发 `Alt-Svc: h3`。平台的执行端见到会改走 QUIC，UDP 不通就超时。
- 打开 `https://你的域名/home/healthz`，返回 `{"ok":true,"ha":true}` 就通了。
- 再用 Token 试一次 MCP，应该列出三个工具：

  ```bash
  curl -s https://你的域名/home/mcp -H "Authorization: Bearer 你的GLASSHOME_MCP_TOKEN" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
  ```

### 第五步：在千问开放平台上配置

| 仓库里的文件 | 放到平台上的哪里 | 怎么放 |
|---|---|---|
| 无 | MCP 插件 | 在网页上填表，见下面第 1 步 |
| `skill/SKILL.md` | 技能 | frontmatter 的 `name`、`description` 填进对应的框，正文粘进去 |

**1. 新建 MCP 插件**

打开「资源库 → MCP 插件 → 创建 MCP 插件」，填：

| 字段 | 填什么 |
|---|---|
| 插件名称 | `米家 MCP`（随意） |
| 描述 | 控制家里接进 Home Assistant 的米家设备：开关、温度、模式、风速、亮度、扫地机，查状态和室温 |
| 基础 URL | `https://你的域名`，末尾不要 `/` |
| endpoint | `/home/mcp` |
| 认证方式 | Bearer Token，只填 `.env` 里 `GLASSHOME_MCP_TOKEN` 的值本身 |
| 传输协议 | 无状态协议 |

保存时平台会连一次服务，拉到 `control`、`status`、`call_service` 三个工具，插件会有一个 6 位的编码（形如 `AbC123`）。

- 平台存的是保存那一刻的工具描述。以后改了 `devices.toml` 并重启服务，要在插件里点「刷新 MCP 工具」，大模型才知道新的设备名。
- 平台会丢掉参数里的 `enum`。取值也写在了描述里，不影响使用。

**2. 新建技能**

打开「资源库 → 技能 → 新建」：

1. 把 `SKILL.md` 开头的 `name`（家居控制）和 `description` 填进对应的框。`description` 决定平台什么话召回这个技能，里面 Use when 的例句最好换成你家的设备名。
2. 正文粘进去，把里面的 `HOME` 全部换成插件编码，保存。`^^编码.control^^` 这样的写法会变成工具卡片。
3. 不需要页面。

**3. 真机测试**

1. 在「设备管理」里添加眼镜，打开开发者模式。
2. 在技能编辑器顶部点「真机测试」，选你的眼镜。
3. 直接对眼镜说“把空调打开”。

自己用不需要发布：插件和技能都是草稿时，绑定了测试的眼镜就能用。

- 删掉插件或技能，不会把技能从眼镜上卸掉。删了要到「设备管理 → 已安装 N 个测试 Skill」里移除，不然旧技能还会抢召回，大模型没有工具可调，就会自己编一句回答。
- 说了没反应，看平台控制台的「调试日志」：召回到哪个技能、每轮大模型、工具的输入输出和用时都在里面。

## 安全

- 拿到 `GLASSHOME_MCP_TOKEN` 的人就能控制你家的设备，不要外泄；泄了就换一个，平台插件里一起改。
- `GLASSHOME_HA_TOKEN` 是 HA 的长期令牌，能管整个 HA，只放在服务器的 `.env` 里。
- `call_service` 能调 HA 里任何设备自己那一类的服务（灯只能调 `light.*` 和通用的 `homeassistant.*`，不能拿灯去调 `lock.unlock`）。家里有门锁、燃气阀这类设备的话，想好要不要让眼镜碰到。
- 服务的日志里有设备名和调用参数，不要贴到公开的地方。
