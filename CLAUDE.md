# usage-elink

蓝签墨水屏 BLE 交互工具 + Kimi Code 用量看板

## 项目概述

通过蓝牙BLE向4.2寸三色墨水屏（黑/白/红，400×300px）发送图片。主要用途：在墨水屏上实时显示 Kimi Code 用量数据。

## 硬件

- **设备**: 蓝签 4.2寸墨水屏，型号 EDP-42000DDF
- **分辨率**: 400×300 像素，三色（黑/白/红）
- **BLE**: Service UUID `0000ffe0-...`，Characteristic UUID `0000ffe2-...`
- **设备地址**: 每台设备不同，运行 `elink setup` 或 `elink scan` 自动发现
- **广播间隔**: 约每3秒广播一次

## 协议（蓝牙BLE通讯V2.1）

### 数据格式
- **黑色通道** type=`0x13`：bit=1 → 白，bit=0 → 黑
- **红色通道** type=`0x12`：bit=1 → 红
- **图像编码**：行优先，从底部到顶部（y=299→0），每行50字节，共15000字节/通道

### 包结构
```
起始包: [type, 0x00, 0x00]
数据包: [type, idx_hi, idx_lo, length, data...]  最多240字节数据
结束包: [type, 0xFF, 0xFF]
```

### 发包时序（关键！）
- 起始包后等 **3秒**（设备初始化）
- 前10包每包等 **1秒**（设备完全就绪）
- 后续包每包等 **0.6秒**
- 使用 Write Without Response（`response=False`），设备不支持 Write With Response
- 单通道约 **50秒**，两通道合计约 **100秒**

> 注：时序是防止丢包的关键。间隔过短会导致屏幕上方出现噪点/乱码。

## 依赖

```toml
bleak>=0.22,<1.0   # BLE库，必须用0.22.x，3.x有CoreBluetooth连接bug
pillow>=12.1.1
click>=8.1
rich>=13.0
```

运行环境：Python 3.13，用 `uv` 管理。依赖声明在 `elink.py` 顶部 inline script header，`uv run elink.py` 自动安装。

## 主要文件

- `elink.py` — 唯一入口：BLE通讯 + 图像转换 + 用量数据拉取 + CLI
- `~/.config/elink/config.json` — 设备地址 + Token 持久化配置
- `/tmp/usage_display.png` — 生成的看板图片（临时）
- `get_kimi_token_cdp.sh` — CDP 自动获取 Token 脚本（可选）

## CLI 用法

```bash
# 首次使用：全自动配置（扫描绑定设备 + 配置 Token）
uv run elink.py setup

# 日常使用
uv run elink.py push              # 一条龙：拉数据 → 生成图 → 发送
uv run elink.py push --dry-run    # 只生成图片，不发送
uv run elink.py watch             # 后台模式，每5分钟自动推送
uv run elink.py watch -i 15       # 每15分钟推送

# 设备管理
uv run elink.py scan              # 扫描附近设备（Rich 表格）
uv run elink.py bind              # 扫描并交互式绑定默认设备
uv run elink.py bind <地址>       # 直接绑定指定地址
uv run elink.py clear             # 清屏（全白）

# 图片发送
uv run elink.py send <图片路径>   # 发送图片（使用绑定设备）
uv run elink.py send <图片> <地址> # 指定地址发送

# 配置
uv run elink.py config show
uv run elink.py config set-token  # 交互输入 Token（隐藏回显）
uv run elink.py config clear-token
```

## 用量看板

### 数据来源

| 数据 | 来源 |
|------|------|
| Quota Usage | `https://api.kimi.com/coding/v1/usages` |
| 剩余额度 | 同上 |

### Token 获取与配置

**优先级**: config 文件 > Keychain `elink-kimi-token` > Linux secret-tool

#### 获取方式

**方式1：CDP 自动获取（推荐，Chrome 需开启远程调试）**

```bash
# 1. Chrome 开启远程调试: 访问 chrome://inspect/#remote-debugging 并启用
# 2. 确保已登录 https://www.kimi.com/code/console
./get_kimi_token_cdp.sh
```

**方式2：手动获取**

1. 浏览器访问 https://www.kimi.com/code/console
2. 登录账号
3. DevTools → Application → Local Storage → `kimi.com` → `access_token`
4. 复制 token 值

**方式3：手动配置**

```bash
uv run elink.py config set-token   # 隐藏输入粘贴 Token
```

**方式4：存入系统密钥库（自动读取）**

```bash
# macOS
security add-generic-password -s "elink-kimi-token" -a "elink" -w "<token>"

# Linux (需要 libsecret 和 secret-tool)
secret-tool store --label='elink-kimi' service elink-kimi
# 然后输入 token
```

### 看板布局

```
┌─────────────────────────────────────┐
│ KIMI CODE                  14:32    │  ← 黑色 header
├─────────────────────────────────────┤
│ QUOTA USAGE               33% used  │
│ Resets in 3h 29m                    │
│ [████████████░░░░░░░░░░░░░░░░░░░░]  │  ← 使用率进度条（≥80% 变红）
├─────────────────────────────────────┤
│ REMAINING                 67        │
│ Limit 100                           │
│ [░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]  │
└─────────────────────────────────────┘
```

## 已知问题与解决方案

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 连接后 CancelledError | bleak 3.x CoreBluetooth bug | 降级到 bleak 0.22.x |
| 扫描停止后连接失败 | CoreBluetooth 丢失 peripheral 引用 | 扫描期间保持 scanner 运行，连接成功后再 stop |
| 屏幕上方噪点/乱码 | Write Without Response 丢包 | 增大包间隔（0.6s），起始延迟3s，前10包1s |
| Writing is not permitted | 设备仅支持 write-without-response | 使用 `response=False` |
| 进度条显示时间长 | 两通道合计约100s为正常 | 进度条含剩余时间列（TimeRemainingColumn）|
