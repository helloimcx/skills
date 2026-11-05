# clash-tailscale-coexist

排查 Clash (Clash Verge / Mihomo / Clash Meta) TUN 模式与 Tailscale 在 macOS / Linux 上可能出现的路由、DNS 和连接问题。根因取决于系统、网络和具体配置，应先收集证据。

## 为什么冲突？
1. DNS resolver 选择或上游路径可能与预期不同，需要比较系统解析与指定上游的结果。
2. 某些网络的 IPv6 路由或代理路径可能异常，需要对同一目标分别测试 IPv4 和 IPv6。
3. Clash 内核的自动或显式接口选择可能影响出站路径，需结合本机配置、路由和连接证据确认。

## 解决方案
先按证据逐项调整。优先保留 MagicDNS 和网络所需的 IPv6；关闭 Tailscale DNS 接管可能影响 MagicDNS，静态 hosts 地址可能过期，关闭 IPv6 可能影响其他应用。

## 快速体检与修复
```bash
# 检查当前系统冲突状态
./scripts/diagnose_and_fix.sh

# 交互式修复：逐项确认并显示恢复命令
./scripts/diagnose_and_fix.sh --fix
```

非交互运行会跳过系统设置修改。执行前请阅读脚本显示的影响和恢复命令。

详细操作 SOP 请阅读 [SKILL.md](./SKILL.md)。
