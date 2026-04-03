#!/bin/bash
# 通过 Chrome CDP 自动获取 Kimi Code Token
# 需要先开启 Chrome 远程调试: chrome://inspect/#remote-debugging

set -e

CDP_SCRIPT="$HOME/.claude/skills/chrome-cdp/scripts/cdp.mjs"

if [ ! -f "$CDP_SCRIPT" ]; then
    echo "错误: Chrome CDP 技能未安装"
    echo "请先安装 chrome-cdp 技能"
    exit 1
fi

echo "=== Kimi Code Token 自动获取 ==="
echo ""
echo "请确保:"
echo "1. Chrome 已开启远程调试 (chrome://inspect/#remote-debugging)"
echo "2. 已在 Chrome 中登录 https://www.kimi.com/code/console"
echo ""

# 列出可用页面
echo "正在查找 Kimi Code 页面..."
PAGES=$(node "$CDP_SCRIPT" list 2>/dev/null | grep -E "kimi.com/code/console|kimi.com" || true)

if [ -z "$PAGES" ]; then
    echo "未找到 Kimi Code 页面，尝试导航..."
    # 获取第一个可用的 target
    FIRST_TARGET=$(node "$CDP_SCRIPT" list 2>/dev/null | head -1 | awk '{print $1}')
    if [ -z "$FIRST_TARGET" ]; then
        echo "错误: 没有可用的 Chrome 页面"
        echo "请先开启 Chrome 远程调试"
        exit 1
    fi
    echo "导航到 Kimi Code Console..."
    node "$CDP_SCRIPT" nav "$FIRST_TARGET" "https://www.kimi.com/code/console" >/dev/null 2>&1
    sleep 3
    TARGET="$FIRST_TARGET"
else
    # 使用已存在的 Kimi 页面
    TARGET=$(echo "$PAGES" | head -1 | awk '{print $1}')
    echo "找到现有页面: $TARGET"
fi

echo ""
echo "正在提取 Token..."

# 从 localStorage 获取 token
TOKEN=$(node "$CDP_SCRIPT" eval "$TARGET" "localStorage.getItem('access_token') || localStorage.getItem('token')" 2>/dev/null | tr -d '"' || true)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    # 尝试从 cookie 获取
    TOKEN=$(node "$CDP_SCRIPT" eval "$TARGET" "document.cookie.split(';').find(c => c.trim().startsWith('access_token='))?.split('=')[1]" 2>/dev/null | tr -d '"' || true)
fi

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ] || [ "$TOKEN" = "undefined" ]; then
    echo "错误: 无法获取 Token"
    echo "请确保已登录 https://www.kimi.com/code/console"
    exit 1
fi

echo ""
echo "✓ Token 获取成功!"
echo "Token: ${TOKEN:0:20}..."
echo ""

# 保存到配置
echo "是否保存到 elink 配置? (y/n)"
read -r CONFIRM
if [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ]; then
    CONFIG_PATH="$HOME/.config/elink/config.json"
    mkdir -p "$(dirname "$CONFIG_PATH")"

    if [ -f "$CONFIG_PATH" ]; then
        # 更新现有配置
        python3 -c "
import json
import sys
with open('$CONFIG_PATH', 'r') as f:
    cfg = json.load(f)
cfg['oauth_token'] = '$TOKEN'
with open('$CONFIG_PATH', 'w') as f:
    json.dump(cfg, f, indent=2)
print('配置已更新')
"
    else
        # 创建新配置
        echo "{\"oauth_token\": \"$TOKEN\"}" > "$CONFIG_PATH"
        echo "配置已创建"
    fi

    echo ""
    echo "✓ Token 已保存到 $CONFIG_PATH"
    echo ""
    echo "现在可以运行: uv run elink.py push"
fi

echo ""
echo "完整 Token (用于手动配置):"
echo "$TOKEN"
