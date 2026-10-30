# clash-tailscale-coexist

彻底根治 Clash (Clash Verge / Mihomo / Clash Meta) TUN 模式与 Tailscale 在 macOS / Linux 上的路由与 DNS 打架冲突。

## 为什么冲突？
1. **Tailscale 劫持全局 DNS**：MagicDNS 将外网域名明文转发至本地 DNS，触发 GFW 抢答投毒（如 `chatgpt.com -> 192.133.77.133`），导致超时断连。
2. **IPv6 Happy Eyeballs 黑洞**：本地宽带分配公网 IPv6，Chrome 优先尝试 IPv6，因代理节点无 IPv6 出口直接在本地撞墙丢包。
3. **Mihomo TUN 网卡绑定陷阱**：Mihomo 将 DNS 查询强制绑定到物理网卡 `en0`，发往 Tailscale 的虚拟 DNS 请求被路由器直接丢弃。

## 解决方案
* **彻底解耦**：Tailscale 退出全局 DNS 接管（`tailscale set --accept-dns=false`）。
* **内置 Hosts 映射**：Tailscale 内部节点由 Clash `hosts:` 表直接解析，0 毫秒直通。
* **规避 IPv6 黑洞**：关闭物理 Wi-Fi 的公网 IPv6（`networksetup -setv6off Wi-Fi`）。

## 快速体检与修复
```bash
# 检查当前系统冲突状态
./scripts/diagnose_and_fix.sh

# 一键自动修复
./scripts/diagnose_and_fix.sh --fix
```

详细操作 SOP 请阅读 [SKILL.md](./SKILL.md)。
