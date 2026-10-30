---
name: clash-tailscale-coexist
description: >
  彻底解决 Clash (Clash Verge / Mihomo / Clash Meta) TUN 虚拟网卡模式与 Tailscale 
  在 macOS / Linux 上的路由与 DNS 打架冲突。当遇到开启 Tailscale 后 Google/ChatGPT 
  报错 ERR_CONNECTION_CLOSED、关闭 Tailscale 后内网域名 (*.ts.net) 打不开、
  以及 MagicDNS 劫持导致 GFW 投毒时使用。提供一键诊断、彻底解耦配置模板与排查 SOP。
---

# Clash 与 Tailscale 终极共存与冲突根治指南

本 Skill 用于彻底根治 **Clash (Clash Verge Rev / Mihomo / Clash Meta) TUN 虚拟网卡模式** 与 **Tailscale** 同时运行时的各类冲突与打架问题。

---

## 核心冲突模型与机理

两者的打架本质上是 **三层不同机制互相缠绕** 导致的：

```mermaid
flowchart TD
    subgraph Conflict1["1. DNS 抢占与投毒"]
        A1["Tailscale 默认开启 MagicDNS"] -->|注入全局 resolver #1| A2["系统所有 DNS 查询发往 100.100.100.100"]
        A2 -->|明文转发国内 DNS 114.114.114.114| A3["GFW 嗅探到 chatgpt.com / google"]
        A3 -->|注入假 IP 192.133.77.133| A4["Chrome 连接假 IP ➔ 超时 / 失败"]
    end

    subgraph Conflict2["2. IPv6 Happy Eyeballs 黑洞"]
        B1["家庭/移动宽带默认分配公网 IPv6"] -->|Tailscale DNS 返回真实 AAAA| B2["Chrome 快乐眼球优先连 IPv6"]
        B2 -->|境外代理 VPS 无 IPv6 出口| B3["IPv6 直连出境被 GFW 跨境丢包"]
        B3 -->|握手挂起| B4["浏览器转圈 ➔ ERR_CONNECTION_CLOSED"]
    end

    subgraph Conflict3["3. Clash 内核物理绑定陷阱"]
        C1["Mihomo 启用 auto-detect-interface"] -->|强制 socket 绑定物理网卡 en0| C2["发往 100.100.100.100 的查询被强送 en0"]
        C2 -->|未走 utun18 虚拟网卡| C3["路由器丢弃 ➔ i/o timeout"]
        C3 -->|Clash 内部 nameserver 挂起| C4["*.ts.net 内部域名彻底失联"]
    end
```

---

## 终极解耦架构

不采用任何互相妥协的“半开/半关”方案，实行**彻底的职责解耦**：

| 组件 / 维度 | 责任分工 | 关键策略 |
| :--- | :--- | :--- |
| **Tailscale** | 仅负责虚拟内网数据传输，退出全局 DNS | 执行 `tailscale set --accept-dns=false`，绝不注入全局 `resolver #1` |
| **Clash (Mihomo)** | 负责全部公网流量的分流与境外加密解析 | 100% 走 Fake-IP 机制，彻底免疫 GFW DNS 投毒 |
| **Tailscale 内网解析** | 由 Clash 本地 `hosts:` 表直接映射 | 将 `*.ts.net` 节点静态绑定为 `100.x.x.x`，0ms 直连且不触发任何外网 DNS |
| **本地网络** | 彻底关闭物理 Wi-Fi 的公网 IPv6 | 执行 `networksetup -setv6off Wi-Fi`，根除 Happy Eyeballs 撞墙黑洞 |
| **TUN 路由** | 排除 Tailscale 与代理服务端网段 | 排除 `100.64.0.0/10`、`fd7a:115c:a1e0::/48` 及代理 VPS 物理 IP |

---

## 五步根治 SOP

### 步骤 1：禁止 Tailscale 劫持系统全局 DNS
在终端执行：
```bash
tailscale set --accept-dns=false
```
* **效果**：Tailscale 不再向 macOS 注册全局 `resolver #1`。
* **内网状态**：虚拟网卡 `utunX` 与内网 IP 通信依然 100% 保持畅通。在菜单栏 Tailscale 图标中，等同于取消勾选 **"Use Tailscale DNS Settings"**。

---

### 步骤 2：关闭物理网卡的公网 IPv6（消除 Happy Eyeballs 黑洞）
在 macOS 终端执行（无需 root 密码）：
```bash
networksetup -setv6off Wi-Fi
```
* **效果**：物理 Wi-Fi 只保留 IPv4，Chrome 和系统组件不会再向 GFW 的 IPv6 跨境黑洞发起 TCP 握手。
* **注意**：Tailscale 内部的 IPv6（`fd7a:...` ULA 地址）由虚拟网卡独立维护，不受此设置影响。

---

### 步骤 3：配置 Clash 规则与排除路由
确保在 Clash Verge 的配置文件或配置增强模板（`Merge.yaml` / `Script.js`）中加入以下关键配置：

#### 1. TUN 模式排除 Tailscale 流量
```yaml
tun:
  enable: true
  stack: gvisor
  auto-route: true
  strict-route: false
  route-exclude-address:
    - 100.64.0.0/10          # Tailscale IPv4 全网段
    - fd7a:115c:a1e0::/48     # Tailscale IPv6 ULA 全网段
    - <VPS_IP_1>/32          # 你的代理节点公网物理 IP
```

#### 2. 全局关闭 IPv6 虚拟路由残留
```yaml
ipv6: false

dns:
  enable: true
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  # 切勿开启 fake-ip-range6，否则会注入 2000::/3 导致伪造 IPv6 握手失败
  fake-ip-filter:
    - "+.ts.net"
    - "*.ts.net"
    - "+.tailscale.com"
    - "+.tailscale.io"
```

#### 3. 直连规则前置 (Prepend Rules)
```yaml
prepend-rules:
  - AND,((NETWORK,udp),(DST-PORT,443)),REJECT  # 拦截 QUIC 避免 UDP 丢包
  - PROCESS-NAME,Tailscale,DIRECT
  - PROCESS-NAME,tailscaled,DIRECT
  - PROCESS-NAME,IPNExtension,DIRECT
  - PROCESS-NAME,io.tailscale.ipn.macsys.network-extension,DIRECT
  - DOMAIN-SUFFIX,ts.net,DIRECT
  - DOMAIN-SUFFIX,tailscale.com,DIRECT
  - DOMAIN-SUFFIX,tailscale.io,DIRECT
  - IP-CIDR,100.64.0.0/10,DIRECT,no-resolve
  - IP-CIDR6,fd7a:115c:a1e0::/48,DIRECT,no-resolve
```

---

### 步骤 4：通过 Clash Hosts 静态映射 Tailscale 节点
查看你的 Tailscale 设备列表：
```bash
tailscale status
```
在 Clash 配置文件中注入 `hosts:`（Clash Verge 可在配置增强 `Merge.yaml` 或当前激活 Profile 中添加）：
```yaml
hosts:
  "dobby.gleanvo.com": "<VPS_PUBLIC_IP>"
  "momomac-mini.tail4df241.ts.net": "100.116.36.36"
  "momomac-mini": "100.116.36.36"
  "dobby.tail4df241.ts.net": "100.105.207.63"
  "dobby": "100.105.207.63"
```
* **效果**：当在浏览器打开 `https://momomac-mini.tail4df241.ts.net/` 时，Clash **0毫秒**直接命中 Hosts 返回内部固定 IP，流量直接送入 Tailscale 虚拟隧道，完全不向外网发 DNS 请求。

---

### 步骤 5：彻底清理特权服务并重启 Clash Verge
在 macOS 上，Clash Verge 内核通常作为 root 特权服务运行。单纯保存文本文件不一定能热重载核心。
执行完整重启命令：
```bash
osascript -e 'quit app "Clash Verge"'
sleep 2
open -a "Clash Verge"
```

---

## 验证与验收清单

| 检查项 | 命令 / 方式 | 预期正常输出 |
| :--- | :--- | :--- |
| **DNS 劫持状态** | `scutil --dns \| head -n 12` | `resolver #1` 为物理网关或路由器 DNS（如 114.114.114.114），**不存在** `100.100.100.100` |
| **物理 IPv6 状态** | `networksetup -getinfo Wi-Fi` | 显示 `IPv6: Off` |
| **Google 访问** | `curl -I https://www.google.com.hk` | `HTTP/2 200 OK` (秒开) |
| **ChatGPT 访问** | `curl -I https://chatgpt.com/` | `HTTP/2 403 / 200` (Cloudflare 毫秒级建立 TLS，无超时) |
| **Tailscale 内部域名** | `curl -I https://<NODE>.<TAILNET>.ts.net/` | 正常连通，返回对应内部 HTTP 服务响应 |
| **Tailscale SSH 连通** | `ssh <NODE_NAME>` | 秒连登录无阻 |

---

## 自动化体检与修复脚本

本 Skill 配套提供了诊断脚本：
```bash
# 运行环境体检
./scripts/diagnose_and_fix.sh

# 一键自动修复
./scripts/diagnose_and_fix.sh --fix
```
