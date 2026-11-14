#!/usr/bin/env bash
#
# diagnose_and_fix.sh - Clash & Tailscale 共存状态诊断与交互式修复
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

confirm_system_change() {
    local change="$1"
    if [[ ! -t 0 ]]; then
        log_warn "未检测到交互终端；跳过系统修改：$change"
        return 1
    fi
    printf '%s [y/N] ' "$change"
    local answer
    read -r answer || return 1
    [[ "$answer" == "y" || "$answer" == "Y" || "$answer" == "yes" || "$answer" == "YES" ]]
}

echo -e "${BOLD}====================================================${RESET}"
echo -e "${BOLD}  Clash & Tailscale 共存状态诊断与修复工具 (macOS)   ${RESET}"
echo -e "${BOLD}====================================================${RESET}\n"

# 1. 检查 Tailscale 运行与系统 DNS resolver
log_info "1. 检查 Tailscale 运行与系统 DNS resolver..."
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
        log_warn "系统首选 resolver 是 Tailscale DNS (100.100.100.100)。"
        log_warn "这只证明当前首选 resolver 是 Tailscale DNS；它本身不能证明公网解析被投毒或是连接故障原因。"
        if [ "$AUTO_FIX" = true ]; then
            log_warn "关闭后 MagicDNS 名称可能无法解析。恢复命令：tailscale set --accept-dns=true"
            if confirm_system_change "要关闭 Tailscale DNS 接管吗？仅在确认 DNS 路径是故障原因后继续。"; then
                "$TAILSCALE_BIN" set --accept-dns=false
                log_ok "已执行 tailscale set --accept-dns=false"
            else
                log_info "未修改 Tailscale DNS 设置。"
            fi
        else
            log_warn "对照故障域名和 DNS 上游的解析结果后，再评估是否需要调整 Tailscale DNS。"
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
    log_ok "物理 Wi-Fi IPv6 已关闭。"
else
    log_warn "物理 Wi-Fi IPv6 未关闭；这不能单独证明 IPv6 是连接故障原因。"
    if [[ "$WIFI_V6" =~ "Automatic" ]]; then
        if [ "$AUTO_FIX" = true ]; then
            log_warn "关闭后恢复自动配置的命令：networksetup -setv6automatic Wi-Fi"
            if confirm_system_change "要临时关闭 Wi-Fi IPv6 吗？仅在 IPv6 路径故障已验证后继续。"; then
                networksetup -setv6off Wi-Fi
                log_ok "已执行 networksetup -setv6off Wi-Fi"
            else
                log_info "未修改 Wi-Fi IPv6 设置。"
            fi
        else
            log_warn "先用 IPv4/IPv6 分别测试目标站点，确认 IPv6 路径异常后再考虑修改。"
        fi
    else
        log_warn "当前 IPv6 模式不是 Automatic；脚本不会覆盖自定义设置。请先记录现有配置并手动评估。"
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
log_info "4. 固定站点示例探测（仅显示观测结果，不判定用户网络正常或异常）..."

test_url() {
    local name="$1"
    local url="$2"

    printf "   %-18s " "测试 $name:"
    local code error_file curl_status curl_error
    error_file=$(mktemp)
    if code=$(curl -sS -o /dev/null -w "%{http_code}" --connect-timeout 4 "$url" 2>"$error_file"); then
        curl_status=0
    else
        curl_status=$?
    fi
    curl_error=$(cat "$error_file")
    rm -f "$error_file"

    if [[ "$curl_status" -eq 0 ]]; then
        echo -e "HTTP ${code:-000}（示例观测；请按目标服务的预期自行判断）"
    else
        echo -e "HTTP ${code:-000}；curl exit ${curl_status}；${curl_error:-未提供错误详情}"
    fi
}

# These public URLs are examples only. Their availability and response codes
# vary by region and policy; probe the user's actual target when diagnosing.
test_url "Google" "https://www.google.com.hk"
test_url "ChatGPT" "https://chatgpt.com/"
test_url "YouTube" "https://www.youtube.com"

echo ""
echo -e "${BOLD}诊断完成！${RESET}"
