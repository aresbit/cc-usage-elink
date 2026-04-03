# kimi-code-usage-elink

Display your **Kimi Code usage** on a BLE tri-color e-ink screen — always visible, zero screen time.

将 Kimi Code 实时用量数据显示在蓝签 4.2寸三色墨水屏上，无需频繁查看浏览器。

## Preview

```
┌─────────────────────────────────────┐
│ KIMI CODE                  02:30    │  ← 黑色 header
├─────────────────────────────────────┤
│ WEEKLY                    39% used  │  ← 7天额度
│ Resets TUE 8PM                      │
│ [████████████░░░░░░░░░░░░░░░░░░░░]  │
├─────────────────────────────────────┤
│ RATE LIMIT                66% used  │  ← 5小时速率限制
│ Resets 0h 33m                       │
│ [████████████████████████░░░░░░░░]  │  ← ≥80% 变红
├─────────────────────────────────────┤
│ REMAINING                 61        │  ← 剩余额度
│ Limit 100                           │
│ [░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]  │
└─────────────────────────────────────┘
```

**颜色规则：**
- 使用率 ≥ 80% 时，进度条和百分比数字变**红色**
- 否则显示**黑色**

## Hardware

**Required: 蓝签 (LANCOS) 4.2-inch tri-color e-ink display, model EDP-42000DDF**

- Resolution: 400 × 300 px, tri-color (black / white / red)
- Interface: Bluetooth BLE
- Protocol: 蓝牙BLE通讯协议 V2.1 (LANCOS proprietary)

> This tool uses the LANCOS BLE V2.1 protocol. It is only compatible with the
> 蓝签 EDP-42000DDF (and same-protocol variants). Other e-ink displays will not work.

## Requirements

- Linux / macOS / Windows (跨平台支持)
- Python 3.13+
- [`uv`](https://docs.astral.sh/uv/) — dependencies install automatically on first run
- Chrome with remote debugging enabled (for automatic token extraction)
- Kimi Code account (已登录状态)

## Quick Start

`elink.py` is a single-file script — no installation needed, just [`uv`](https://docs.astral.sh/uv/).

```bash
# Clone the repository
git clone https://github.com/aresbit/cc-usage-elink && cd cc-usage-elink

# First-time setup: scan device + configure token
uv run elink.py setup

# After setup
uv run elink.py push          # push usage to screen once
uv run elink.py watch         # background mode, refresh every 5 min
uv run elink.py watch -i 15   # refresh every 15 min
```

## Commands

| Command | Description |
|---------|-------------|
| `setup [--force]` | Auto-configure: scan device + auto-extract token from Chrome |
| `push [--dry-run]` | Generate usage image → send to screen |
| `watch [-i MIN]` | Background mode, push every N minutes (default 5) |
| `scan [-t SEC]` | Scan for nearby BLE devices |
| `bind [ADDRESS]` | Bind default device |
| `clear` | Clear screen (all white) |
| `send IMAGE [ADDRESS]` | Send any image file to screen |
| `config show` | Show current config |
| `config set-token` | Set token manually (hidden input) |
| `config clear-token` | Remove saved token |

## Token Configuration

Kimi Code 需要有效的 access_token 才能获取用量数据。支持三种配置方式：

### Option 1 — Chrome CDP 自动提取（推荐）

```bash
# 1. 确保 Chrome 已开启远程调试
#    访问 chrome://inspect/#remote-debugging 并启用开关

# 2. 登录 https://www.kimi.com/code/console

# 3. 运行 setup，自动从 Chrome 提取 token
uv run elink.py setup
```

### Option 2 — 手动配置

```bash
# 浏览器访问 https://www.kimi.com/code/console
# DevTools → Application → Local Storage → kimi.com → access_token
# 复制 token 值

uv run elink.py config set-token
# 粘贴 token
```

### Option 3 — 系统密钥库（安全存储）

```bash
# macOS
security add-generic-password -s "elink-kimi-token" -a "elink" -w "<your-token>"

# Linux (需要 libsecret)
secret-tool store --label='elink-kimi' service elink-kimi
# 然后输入 token
```

Token lookup priority: `~/.config/elink/config.json` → Keychain / secret-tool → Chrome CDP auto-extract

## Data Source

用量数据通过 **Chrome CDP** 从 Kimi Code Console 页面实时提取：

| 数据项 | 来源 | 说明 |
|--------|------|------|
| Weekly usage | 页面文本 "Weekly usage XX%" | 7天周期额度使用率 |
| Rate limit | 页面文本 "Rate limit XX%" | 5小时速率限制使用率 |
| Reset time | 页面文本 "Resets in X hours/minutes" | 自动计算重置时间 |
| Remaining | 100 - used | 剩余可用额度 |

> 注意：直接从浏览器页面提取，无需调用 Kimi API，避免 401 认证问题。

## Config File

Stored at `~/.config/elink/config.json`:

```json
{
  "device_address": "FF:FF:42:00:XX:XX",
  "oauth_token": "eyJhbGciOiJIUzUxMiIs..."
}
```

## BLE Protocol Notes

The LANCOS BLE V2.1 protocol sends full-screen image data in two passes (black channel + red channel). Key timings that prevent display artifacts:

- 3 s delay after start packet (device init)
- 1 s per packet for first 10 packets
- 0.6 s per packet thereafter
- Write Without Response only (`response=False`)

Total transfer time: ~100 seconds per full refresh.

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| Token 获取失败 | Chrome 远程调试未开启 | 访问 `chrome://inspect/#remote-debugging` 启用开关 |
| Token 获取失败 | 未登录 Kimi Code | 浏览器访问 `https://www.kimi.com/code/console` 并登录 |
| API 401 Unauthorized | Token 过期 | 重新运行 `uv run elink.py setup` 获取新 token |
| `CancelledError` after connect | bleak 3.x CoreBluetooth bug | Pinned to bleak 0.22.x |
| Connect fails after scan | CoreBluetooth drops peripheral ref | Keep scanner running until connected |
| Artifacts at top of screen | Dropped BLE packets | Larger inter-packet delays |
| `Writing is not permitted` | Device only supports write-without-response | Use `response=False` |

## License

MIT
