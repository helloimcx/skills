#!/usr/bin/env bash
#
# diagnose_and_fix.sh - Clash & Tailscale 共存状态体检与一键修复工具
#

set -e

RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[33m"
CYAN="\033[36m"
BOLD="\033[1m"
RESET="\033[0m"

log_info() { echo -e "${CYAN}[INFO]${RESET} $1"; }
log_ok() { echo -e "${GREEN}[OK]${RESET} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${RESET} $1"; }
log_err() { echo -e "${RED}[ERROR]${RESET} $1"; }

AUTO_FIX=false
if [[ "$1" == "--fix" || "$1" == "-f" ]]; then
    AUTO_FIX=true
fi

echo -e "${BOLD}====================================================${RESET}"
echo -e "${BOLD}  Clash & Tailscale 共存状态诊断与修复工具 (macOS)   ${RESET}"
echo -e "${BOLD}====================================================${RESET}\n"

# 1. 检查 Tailscale 状态与 DNS 接管
log_info "1. 检查 Tailscale 运行与 DNS 劫持状态..."
if ! command -v tailscale &>/dev/null; then
    log_warn "未在 PATH 中找到 tailscale 命令，尝试常见路径..."
    TAILSCALE_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
else
    TAILSCALE_BIN="tailscale"
fi

if ! "$TAILSCALE_BIN" status &>/dev/null; then
    log_warn "Tailscale 当前未运行或未登录。"
else
    TS_STATUS=$("$TAILSCALE_BIN" status | head -n 3)
    echo "$TS_STATUS" | sed 's/^/   /'
    
    # 检查 scutil --dns 是否被 Tailscale 抢占为 resolver #1
    FIRST_NS=$(scutil --dns | awk '/resolver #1/{flag=1; next} /resolver #2/{flag=0} flag && /nameserver\[0\]/{print $3; exit}')
    if [[ "$FIRST_NS" == "100.100.100.100" ]]; then
        log_err "Tailscale 正在接管系统全局 DNS (resolver #1 为 100.100.100.100)！"
        log_err "➔ 这会导致 GFW 对外网域名明文投毒，触发 ChatGPT/Google 无法访问！"
        if [ "$AUTO_FIX" = true ]; then
            log_info "正在执行修复：禁止 Tailscale 接管系统全局 DNS..."
            "$TAILSCALE_BIN" set --accept-dns=false
            log_ok "已执行 tailscale set --accept-dns=false"
        else
            log_warn "建议修复：运行 $0 --fix 或执行 'tailscale set --accept-dns=false'"
        fi
    else
        log_ok "Tailscale 未接管全局 DNS (resolver #1 为: $FIRST_NS)"
    fi
fi

echo ""

# 2. 检查物理 Wi-Fi 的 IPv6 状态
log_info "2. 检查物理网络公网 IPv6 状态..."
WIFI_V6=$(networksetup -getinfo Wi-Fi 2>/dev/null | grep -i "IPv6:" || echo "IPv6: Unknown")
echo "   Wi-Fi $WIFI_V6"

if [[ "$WIFI_V6" =~ "Off" ]]; then
    log_ok "物理 Wi-Fi IPv6 已关闭（已免疫 Happy Eyeballs GFW 丢包黑洞）。"
else
    log_warn "物理 Wi-Fi 当前开启了 IPv6！"
    log_warn "➔ 浏览器（Happy Eyeballs）会优先尝试 IPv6，导致境外网站被 GFW 拦截挂起。"
    if [ "$AUTO_FIX" = true ]; then
        log_info "正在执行修复：关闭物理 Wi-Fi 的 IPv6..."
        networksetup -setv6off Wi-Fi
        log_ok "已执行 networksetup -setv6off Wi-Fi"
    else
        log_warn "建议修复：运行 $0 --fix 或执行 'networksetup -setv6off Wi-Fi'"
    fi
fi

echo ""

# 3. 检查 Clash TUN 接口与虚假 IPv6 路由
log_info "3. 检查 Clash TUN 接口与路由表..."
TUN_DEV=$(ifconfig | grep -E "utun[0-9]+" | awk -F':' '{print $1}' | tr '\n' ' ')
echo "   活跃 TUN 接口: $TUN_DEV"

V6_ROGUE_ROUTES=$(netstat -nr -f inet6 2>/dev/null | grep -E "utun[0-9]+" | grep -E "2000::/3|100::/8" || true)
if [ -n "$V6_ROGUE_ROUTES" ]; then
    log_warn "检测到虚拟网卡残留有全局 IPv6 路由："
    echo "$V6_ROGUE_ROUTES" | sed 's/^/   /'
    log_warn "➔ 建议检查 Clash 配置中的 fake-ip-range6 并彻底重启 Clash Verge。"
else
    log_ok "虚拟网卡未发现伪造的全局 IPv6 路由。"
fi

echo ""

# 4. 关键连通性实测
log_info "4. 真实网络连通性测试..."

test_url() {
    local name="$1"
    local url="$2"
    local expected_code="$3"
    
    printf "   %-18s " "测试 $name:"
    local code
    code=$(curl -s -k -o /dev/null -w "%{http_code}" --connect-timeout 4 "$url" 2>/dev/null || echo "FAILED")
    
    if [[ "$code" == "$expected_code" || ( "$name" == "ChatGPT" && ( "$code" == "403" || "$code" == "200" ) ) ]]; then
        echo -e "${GREEN}${code} 正常${RESET}"
    else
        echo -e "${RED}${code} 异常 (超时或未连通)${RESET}"
    fi
}

test_url "Google" "https://www.google.com.hk" "200"
test_url "ChatGPT" "https://chatgpt.com/" "403"
test_url "YouTube" "https://www.youtube.com" "200"

echo ""
echo -e "${BOLD}诊断完成！${RESET}"
