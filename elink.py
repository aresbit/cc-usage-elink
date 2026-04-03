#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "bleak>=0.22,<1.0",
#   "pillow>=12.1.1",
#   "click>=8.1",
#   "rich>=13.0",
# ]
# ///
"""
蓝签墨水屏 BLE 交互工具 + Kimi Code 用量看板
设备: 4.2寸(400x300) 三色墨水屏 (黑/白/红)
"""

import asyncio
import json
import math
import subprocess
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import click
from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

# ── Constants ─────────────────────────────────────────────────────────────────

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID    = "0000ffe2-0000-1000-8000-00805f9b34fb"

TYPE_BLACK = 0x13  # bit=1 → 白, bit=0 → 黑
TYPE_RED   = 0x12  # bit=1 → 红

SCREEN_W = 400
SCREEN_H = 300
CHUNK    = 240  # 每包最多 240 字节

CONFIG_PATH = Path.home() / ".config" / "elink" / "config.json"

console = Console()

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ── Protocol ──────────────────────────────────────────────────────────────────

def build_start(color_type: int) -> bytes:
    return bytes([color_type, 0x00, 0x00])


def build_end(color_type: int) -> bytes:
    return bytes([color_type, 0xFF, 0xFF])


def build_data_packets(color_type: int, data: bytes) -> list[bytes]:
    packets = []
    for i, offset in enumerate(range(0, len(data), CHUNK)):
        idx   = i + 1
        chunk = data[offset:offset + CHUNK]
        hi    = (idx >> 8) & 0xFF
        lo    = idx & 0xFF
        packets.append(bytes([color_type, hi, lo, len(chunk)]) + chunk)
    return packets


# ── Image conversion ──────────────────────────────────────────────────────────

def image_to_eink_bytes(image_path: str) -> tuple[bytes, bytes]:
    img = Image.open(image_path).convert("RGB").resize((SCREEN_W, SCREEN_H))
    black_data = bytearray()
    red_data   = bytearray()

    for y in range(SCREEN_H - 1, -1, -1):
        for byte_idx in range(math.ceil(SCREEN_W / 8)):
            b_black = 0
            b_red   = 0
            for bit in range(8):
                x = byte_idx * 8 + bit
                if x >= SCREEN_W:
                    b_black |= (1 << (7 - bit))
                    continue
                r, g, b = img.getpixel((x, y))
                is_red   = r > 150 and g < 100 and b < 100
                is_black = (r + g + b) < 200 and not is_red
                if not is_black:
                    b_black |= (1 << (7 - bit))
                if is_red:
                    b_red |= (1 << (7 - bit))
            black_data.append(b_black)
            red_data.append(b_red)

    return bytes(black_data), bytes(red_data)


# ── BLE ───────────────────────────────────────────────────────────────────────

def _is_eink_device(device, adv) -> bool:
    """判断是否为蓝签墨水屏设备（严格匹配）"""
    name  = (device.name or "").upper()
    uuids = " ".join(str(u).lower() for u in (adv.service_uuids or []))
    # 设备名含 EDP（蓝签固件特征）或广播完整 Service UUID
    return "EDP" in name or SERVICE_UUID.lower() in uuids


async def scan_devices(timeout: float = 8.0) -> list[tuple]:
    """扫描蓝签设备，返回 (device, rssi) 列表，按 RSSI 降序（信号最强排最前）"""
    seen: dict[str, tuple] = {}  # {address: (device, best_rssi)}

    def on_detect(device, adv):
        if not _is_eink_device(device, adv):
            return
        rssi = adv.rssi if adv.rssi is not None else -100
        prev = seen.get(device.address)
        if prev is None or rssi > prev[1]:
            seen[device.address] = (device, rssi)

    async with BleakScanner(detection_callback=on_detect):
        await asyncio.sleep(timeout)

    return sorted(seen.values(), key=lambda x: -x[1])


async def scan_until_found(timeout: float = 600.0) -> list:
    """扫描直到发现蓝签设备（或超时），找到后再等5s收集其他同类设备，按RSSI排序"""
    seen: dict[str, tuple] = {}  # {address: (device, best_rssi)}
    ev    = asyncio.Event()
    start = time.monotonic()

    def on_detect(device, adv):
        if not _is_eink_device(device, adv):
            return
        rssi = adv.rssi if adv.rssi is not None else -100
        prev = seen.get(device.address)
        if prev is None or rssi > prev[1]:
            seen[device.address] = (device, rssi)
        if not ev.is_set():
            ev.set()

    async with BleakScanner(detection_callback=on_detect):
        live_task = asyncio.create_task(_scan_with_live(ev, start, timeout, None))
        try:
            await asyncio.wait_for(asyncio.shield(ev.wait()), timeout=timeout)
            await asyncio.sleep(5.0)  # 多等5s，让同类设备都有机会广播
        except asyncio.TimeoutError:
            raise RuntimeError(f"扫描 {timeout:.0f}s 未发现设备，请确认设备已开机")
        finally:
            live_task.cancel()
            try:
                await live_task
            except asyncio.CancelledError:
                pass

    return sorted(seen.values(), key=lambda x: -x[1])


async def _scan_with_live(ev: asyncio.Event, start: float, scan_timeout: float, address: str | None):
    """扫描期间显示进度条（elapsed / timeout）"""
    addr_str = f" [cyan]{address}[/cyan]" if address else ""
    with Progress(
        SpinnerColumn(),
        TextColumn(f"[bold]扫描{addr_str}[/bold]"),
        BarColumn(bar_width=30),
        TextColumn("[dim]{task.completed:.0f}s / {task.total:.0f}s[/dim]"),
        console=console,
    ) as progress:
        task = progress.add_task("", total=scan_timeout)
        while not ev.is_set():
            elapsed = time.monotonic() - start
            progress.update(task, completed=min(elapsed, scan_timeout))
            await asyncio.sleep(0.25)


async def _try_direct_connect(address: str, attempts: int = 8) -> BleakClient | None:
    """跳过扫描，直接按地址连接（CoreBluetooth 缓存过的设备有效）"""
    for i in range(attempts):
        client = BleakClient(address)
        try:
            await client.connect(timeout=30.0)
            return client
        except Exception as e:
            console.print(f"  [dim]直连尝试 {i+1}/{attempts}: {e}[/dim]")
            if i < attempts - 1:
                await asyncio.sleep(0.5)
    return None


async def find_and_connect(address: str | None, scan_timeout: float = 60.0) -> BleakClient:
    # ── 快速路径：有地址先直连，无需等广播 ──────────────────────────
    if address:
        with console.status(f"[bold]直连 [cyan]{address}[/cyan]...[/bold]"):
            client = await _try_direct_connect(address)
        if client:
            console.print(f"[green]✓[/green] 直连成功")
            return client
        console.print("[yellow]直连失败，回退到扫描模式...[/yellow]")

    # ── 扫描模式（无地址 或 直连失败） ───────────────────────────────
    # {address: (device, best_rssi)} — 持续更新，取最强信号
    candidates: dict[str, tuple] = {}
    ev    = asyncio.Event()

    def on_detect(device, adv):
        # 严格匹配：有绑定地址时只接受精确匹配
        if address is not None and device.address != address:
            return
        if not _is_eink_device(device, adv):
            return
        rssi = adv.rssi if adv.rssi is not None else -100
        prev = candidates.get(device.address)
        if prev is None or rssi > prev[1]:
            candidates[device.address] = (device, rssi)
        if not ev.is_set():
            ev.set()

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()

    start     = time.monotonic()
    live_task = asyncio.create_task(_scan_with_live(ev, start, scan_timeout, address))
    try:
        await asyncio.wait_for(asyncio.shield(ev.wait()), timeout=scan_timeout)
        # 多等3s让信号更新（取最强的）
        await asyncio.sleep(3.0)
    except asyncio.TimeoutError:
        live_task.cancel()
        await scanner.stop()
        raise RuntimeError(f"扫描 {scan_timeout:.0f}s 未找到设备，请确认设备已开机并在附近")
    finally:
        live_task.cancel()
        try:
            await live_task
        except asyncio.CancelledError:
            pass

    # 选信号最强的设备
    found, best_rssi = max(candidates.values(), key=lambda x: x[1])
    elapsed = time.monotonic() - start
    console.print(
        f"[green]✓[/green] 找到: [cyan]{found.name or '?'}[/cyan]  "
        f"[dim]RSSI={best_rssi}dBm  {elapsed:.1f}s[/dim]"
    )

    # 保持 scanner 运行 — CoreBluetooth 需要扫描器存活才能保住 peripheral 引用
    for attempt in range(8):
        await asyncio.sleep(0.3)
        client = BleakClient(found)
        try:
            await client.connect(timeout=30.0)
            await scanner.stop()
            return client
        except Exception as e:
            console.print(f"  [yellow]连接失败 (第{attempt+1}次): {e}，等1s重试...[/yellow]")
            await asyncio.sleep(1.0)

    await scanner.stop()
    raise RuntimeError("连接失败，已重试8次")


async def send_channel(
    client: BleakClient,
    color_type: int,
    data: bytes,
    progress: Progress,
    task_id,
):
    packets = build_data_packets(color_type, data)

    await client.write_gatt_char(CHAR_UUID, build_start(color_type), response=False)
    await asyncio.sleep(3.0)

    for i, pkt in enumerate(packets):
        await client.write_gatt_char(CHAR_UUID, pkt, response=False)
        await asyncio.sleep(1.0 if i < 10 else 0.6)
        progress.update(task_id, advance=1)

    await client.write_gatt_char(CHAR_UUID, build_end(color_type), response=False)
    await asyncio.sleep(0.1)


def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    )


async def _do_send(address: str | None, black_bytes: bytes, red_bytes: bytes):
    n_black = math.ceil(len(black_bytes) / CHUNK)
    n_red   = math.ceil(len(red_bytes)   / CHUNK)

    client = await find_and_connect(address)
    console.print(f"[green]✓[/green] 已连接  预计约 {_est_seconds(n_black + n_red):.0f}s")

    try:
        with _make_progress() as progress:
            t1 = progress.add_task("[bold]黑色通道[/bold]", total=n_black)
            await send_channel(client, TYPE_BLACK, black_bytes, progress, t1)

            t2 = progress.add_task("[red]红色通道[/red]", total=n_red)
            await send_channel(client, TYPE_RED, red_bytes, progress, t2)

        console.print(Panel("[bold green]发送完毕，墨水屏正在刷新！[/bold green]", expand=False))
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            # 断开时的 EOFError 等设备异常不影响成功结果
            console.print(f"[dim]断开连接: {e}[/dim]")


def _est_seconds(total_packets: int) -> float:
    """估算发送耗时（秒）：起始3s + 前10包×1s + 剩余×0.6s，两通道各一次"""
    per_channel = 3.0 + min(total_packets // 2, 10) * 1.0 + max(total_packets // 2 - 10, 0) * 0.6
    return per_channel * 2


# ── Usage image ───────────────────────────────────────────────────────────────

def _detect_token_from_keychain() -> str | None:
    """尝试从系统密钥库自动读取 Kimi Token（macOS Keychain / Linux secret-tool）"""
    import sys

    # macOS: security 命令
    if sys.platform == "darwin":
        try:
            # 优先查找 Kimi token
            raw = subprocess.run(
                ["security", "find-generic-password", "-s", "elink-kimi-token", "-w"],
                capture_output=True, text=True,
            ).stdout.strip()
            if raw:
                return raw
        except FileNotFoundError:
            pass

    # Linux: secret-tool (libsecret)
    elif sys.platform.startswith("linux"):
        try:
            raw = subprocess.run(
                ["secret-tool", "lookup", "service", "elink-kimi"],
                capture_output=True, text=True,
            ).stdout.strip()
            if raw:
                return raw
        except FileNotFoundError:
            pass

    return None


def _get_token_from_cdp() -> str | None:
    """通过 Chrome CDP 从已登录的 Kimi 页面获取 token"""
    cdp_script = Path.home() / ".claude" / "skills" / "chrome-cdp" / "scripts" / "cdp.mjs"
    if not cdp_script.exists():
        return None

    try:
        # 1. 列出所有页面，找 kimi.com/code/console
        result = subprocess.run(
            ["node", str(cdp_script), "list"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None

        # 解析找到 kimi 页面
        target = None
        for line in result.stdout.splitlines():
            if "kimi.com/code/console" in line.lower():
                # 提取 target ID (第一列)
                parts = line.split()
                if parts:
                    target = parts[0]
                    break

        if not target:
            return None

        # 2. 从页面 localStorage 获取 access_token
        result = subprocess.run(
            ["node", str(cdp_script), "eval", target, "localStorage.getItem('access_token')"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            token = result.stdout.strip().strip('"')
            if token and token.startswith("eyJ"):
                return token

    except Exception:
        pass

    return None


def get_oauth_token() -> str | None:
    # 优先级: config 文件 > Keychain
    return load_config().get("oauth_token") or _detect_token_from_keychain()


def _prompt_manual_token(cfg: dict):
    """提示用户手动输入 Token"""
    import sys
    if sys.platform == "darwin":
        store_hint = "[dim]安全存储: security add-generic-password -s elink-kimi-token -a elink -w <token>[/dim]"
    elif sys.platform.startswith("linux"):
        store_hint = "[dim]安全存储: secret-tool store --label='elink-kimi' service elink-kimi[/dim]"
    else:
        store_hint = ""
    console.print(
        "[dim]获取方式: 浏览器访问 https://www.kimi.com/code/console[/dim]\n"
        "[dim]DevTools → Application → Local Storage → kimi.com → access_token[/dim]\n"
        + store_hint
    )
    token = click.prompt("粘贴 Kimi Token", hide_input=True)
    cfg["oauth_token"] = token
    save_config(cfg)
    console.print("[green]✓[/green] Token 已保存")


def fetch_usage(token: str) -> dict | None:
    """获取 Kimi Code 用量数据（通过 CDP 从页面文本解析）"""
    # 优先使用 CDP 从浏览器页面获取
    cdp_data = _get_usage_from_page()
    if cdp_data:
        return cdp_data

    # 回退：直接 API 请求（可能会 401）
    try:
        req = urllib.request.Request(
            "https://api.kimi.com/coding/v1/usages",
            headers={
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("usage", {})
    except Exception as e:
        console.print(f"[yellow]API 请求失败: {e}[/yellow]")
        return None





def _get_usage_from_page() -> dict | None:
    """通过 Chrome CDP 从页面文本解析用量数据"""
    cdp_script = Path.home() / ".claude" / "skills" / "chrome-cdp" / "scripts" / "cdp.mjs"
    if not cdp_script.exists():
        return None

    try:
        # 1. 找到 kimi 页面
        result = subprocess.run(
            ["node", str(cdp_script), "list"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None

        target = None
        for line in result.stdout.splitlines():
            if "kimi.com/code/console" in line.lower():
                parts = line.split()
                if parts:
                    target = parts[0]
                    break

        if not target:
            return None


        # 2. 获取页面文本
        result = subprocess.run(
            ["node", str(cdp_script), "eval", target, "document.body.innerText"],
            capture_output=True, text=True, timeout=15
        )

        if result.returncode != 0:
            return None

        text = result.stdout

        # 3. 解析用量数据
        import re
        from datetime import datetime, timedelta, timezone

        usage_data = {}

        # 清理文本：将 Unicode 空白替换为普通空格
        clean_text = text.replace('\xa0', ' ').replace('\u2002', ' ')
        lines = clean_text.split('\n')


        for i, line in enumerate(lines):
            # Weekly usage 行 - 查找附近6行内的百分比
            if 'Weekly usage' in line or '每周用量' in line:
                for j in range(i, min(i + 6, len(lines))):
                    pct_match = re.search(r'(\d+)%', lines[j])
                    if pct_match and 'weekly_pct' not in usage_data:
                        usage_data['weekly_pct'] = int(pct_match.group(1))
                    reset_match = re.search(r'Resets in (\d+) hours', lines[j], re.IGNORECASE)
                    if reset_match:
                        hours = int(reset_match.group(1))
                        reset_time = datetime.now(timezone.utc) + timedelta(hours=hours)
                        usage_data['weekly_reset'] = reset_time.isoformat()

            # Rate limit 行
            if 'Rate limit' in line or '利率限制' in line:
                for j in range(i, min(i + 6, len(lines))):
                    pct_match = re.search(r'(\d+)%', lines[j])
                    if pct_match and 'rate_pct' not in usage_data:
                        usage_data['rate_pct'] = int(pct_match.group(1))
                    reset_match = re.search(r'Resets in (\d+) minutes', lines[j], re.IGNORECASE)
                    if reset_match:
                        mins = int(reset_match.group(1))
                        reset_time = datetime.now(timezone.utc) + timedelta(minutes=mins)
                        usage_data['rate_reset'] = reset_time.isoformat()

        # 构建返回数据
        if 'weekly_pct' in usage_data:
            result_data = {
                'limit': '100',
                'used': str(usage_data['weekly_pct']),
                'remaining': str(100 - usage_data['weekly_pct']),
                'resetTime': usage_data.get('weekly_reset', ''),
                'rate_used': str(usage_data.get('rate_pct', 0)),
                'rate_remaining': str(100 - usage_data.get('rate_pct', 0)),
                'rate_reset': usage_data.get('rate_reset', ''),
            }
            return result_data
        else:
            console.print("[yellow]未找到 weekly_pct 数据[/yellow]")

    except Exception as e:
        import traceback
        console.print(f"[yellow]CDP获取失败: {e}[/yellow]")
        console.print(f"[dim]{traceback.format_exc()}[/dim]")

    return None

def _fmt_resets(iso: str | None) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso).astimezone()
    s  = (dt - datetime.now(timezone.utc).astimezone()).total_seconds()
    if s <= 0:
        return "Now"
    h, rem = divmod(int(s), 3600)
    m = rem // 60
    return f"{h}h {m}m" if h < 24 else dt.strftime("%a %-I%p").upper()


def render_usage_image(out: str = "/tmp/usage_display.png") -> str:
    # ── Colors ────────────────────────────────────────────────────────
    BG    = (252, 252, 250)
    BLACK = (8,   8,   8)
    WHITE = (255, 255, 255)
    GRAY  = (115, 115, 115)
    LGRAY = (215, 215, 215)
    RED   = (190, 30,  30)

    # ── Data ──────────────────────────────────────────────────────────
    token = get_oauth_token()
    api   = fetch_usage(token) if token else None
    if not token:
        console.print("[yellow]未找到 Token（用 elink setup 配置）[/yellow]")

    # Kimi Code API: { "limit": "100", "used": "33", "remaining": "67", "resetTime": "..." }
    usage = api or {}

    # 计算使用率百分比
    def _calc_pct(u: dict) -> float:
        limit = float(u.get("limit", 0) or 0)
        used = float(u.get("used", 0) or 0)
        return (used / limit) if limit > 0 else 0

    def _get_pct_str(u: dict) -> str:
        pct = _calc_pct(u) * 100
        return f"{pct:.0f}%" if u else "—"

    def _get_reset_str(u: dict) -> str:
        rt = u.get("resetTime")
        return _fmt_resets(rt) if rt else "—"

    # ── Canvas ────────────────────────────────────────────────────────
    W, H  = 400, 300
    P     = 14

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    # 跨平台字体选择：优先使用系统默认粗体无衬线字体
    def _get_font_paths() -> list[str]:
        import sys
        paths = []
        if sys.platform == "darwin":
            paths = [
                "/System/Library/Fonts/SFCompact.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/SFNS.ttf",
                "/System/Library/Fonts/Arial.ttf",
            ]
        elif sys.platform == "win32":
            paths = [
                "C:/Windows/Fonts/segoeuib.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/calibrib.ttf",
                "C:/Windows/Fonts/arial.ttf",
            ]
        else:  # Linux / Unix
            # 通过 fc-list 动态获取已安装字体（如果可用）
            try:
                result = subprocess.run(
                    ["fc-list", ":family", "-f", "%{file}\n"],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0:
                    candidates = [l.strip() for l in result.stdout.splitlines() if l.strip()]
                    # 优先常见的粗体/清晰字体
                    priority = ["DejaVuSans-Bold", "LiberationSans-Bold", "FreeSans-Bold",
                                "NotoSans-Bold", "Ubuntu-Bold", "DejaVuSans",
                                "LiberationSans", "FreeSans", "NotoSans"]
                    for p in priority:
                        for c in candidates:
                            if p.lower().replace("-bold", "") in c.lower() and p.replace("-Bold", "").lower() in c.lower():
                                if "bold" in c.lower() or "Bold" in c:
                                    paths.append(c)
                                    break
                        if not paths:
                            for c in candidates:
                                if p.replace("-Bold", "").lower() in c.lower():
                                    paths.append(c)
                                    break
            except Exception:
                pass
            # 回退到常见路径
            paths += [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSans-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            ]
        return paths

    def F(size: int) -> ImageFont.FreeTypeFont:
        for path in _get_font_paths():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    def vcenter(font, row_y: int, row_h: int) -> int:
        """返回使文字在 row_y..row_y+row_h 内垂直居中的绘制 y"""
        bb = font.getbbox("Ag")
        return row_y + (row_h - (bb[3] - bb[1])) // 2 - bb[1]

    # ── Header ────────────────────────────────────────────────────────
    HDR_H = 38
    d.rectangle([0, 0, W, HDR_H], fill=BLACK)
    tf = F(22)
    d.text((P, vcenter(tf, 0, HDR_H)), "KIMI CODE", font=tf, fill=WHITE)
    sf = F(14)
    right_str = datetime.now().strftime("%H:%M") + "  " + date.today().strftime("%-d %b").upper()
    d.text((W - P, vcenter(sf, 0, HDR_H)), right_str, font=sf, fill=(190, 190, 190), anchor="rt")

    # ── 3 sections 无缝铺满剩余高度 ──────────────────────────────────
    # 1px 分割线 × 2 + 3 section 等高 = 300 - 38 = 262px
    SEP_H   = 1
    SEC_H   = (H - HDR_H - 2 * SEP_H) // 3   # 86px
    LABEL_H = 28                               # 标签行高
    BAR_H   = SEC_H - LABEL_H                 # 58px 进度条

    # 右侧百分比最宽为 "100%"，留足够空间
    pf     = F(20)
    PCW    = int(pf.getlength("100%")) + 12  # 增加右边距
    # 左侧标签宽度：适应最长的标签 "TOTAL"
    BLW    = int(F(20).getlength("TOTAL")) + 4  # 减小左边距

    def section(y0: int, label: str, reset_str: str,
                bar_label: str, pct: float, pct_str: str,
                fill_color=BLACK):
        # 标签行
        lf  = F(20)
        rf  = F(14)
        d.text((P, vcenter(lf, y0, LABEL_H)),          label,     font=lf, fill=BLACK)
        d.text((W - P, vcenter(rf, y0, LABEL_H)), reset_str, font=rf, fill=GRAY, anchor="rt")

        # 进度条行
        by  = y0 + LABEL_H
        bx  = P + BLW + 4
        bw  = W - P - BLW - PCW - 8

        blf = F(20)
        d.text((P, vcenter(blf, by, BAR_H)), bar_label, font=blf, fill=BLACK)

        d.rectangle([bx, by + 2, bx + bw, by + BAR_H - 2], outline=BLACK, width=1, fill=LGRAY)
        if pct > 0.001:
            fw = max(int(pct * (bw - 2)), 1)
            d.rectangle([bx + 1, by + 3, bx + fw, by + BAR_H - 3], fill=fill_color)

        ppf = F(20)
        d.text((W - P, vcenter(ppf, by, BAR_H)), pct_str, font=ppf, fill=fill_color, anchor="rt")

    # ── 渲染 section + 分割线 ────────────────────────────────────
    # Kimi Code: 显示 Weekly usage 和 Rate limit
    usage_pct = _calc_pct(usage)
    usage_col = RED if usage_pct >= 0.8 else BLACK
    usage_str = _get_pct_str(usage)
    usage_rst = _get_reset_str(usage)

    # Rate limit 数据
    rate_used = usage.get("rate_used", "0")
    rate_pct = int(rate_used) / 100 if rate_used.isdigit() else 0
    rate_col = RED if rate_pct >= 0.8 else BLACK
    rate_str = f"{rate_used}%" if rate_used.isdigit() else "—"
    rate_rst = _fmt_resets(usage.get("rate_reset"))

    # 获取剩余额度显示
    remaining = usage.get("remaining", "—")
    limit = usage.get("limit", "—")

    sections = [
        ("WEEKLY", f"Resets {usage_rst}", "7D", usage_pct, usage_str, usage_col),
        ("RATE LIMIT", f"Resets {rate_rst}", "5H", rate_pct, rate_str, rate_col),
        ("REMAINING", f"Limit {limit}", "LEFT", usage_pct, str(remaining), usage_col),
    ]
    for i, args in enumerate(sections):
        y0 = HDR_H + i * (SEC_H + SEP_H)
        section(y0, *args)
        if i < 2:
            sep_y = y0 + SEC_H
            d.line([P, sep_y, W - P, sep_y], fill=LGRAY, width=SEP_H)

    img.save(out)
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """蓝签墨水屏 CLI · Kimi Code 用量看板"""


# ─ setup ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--force", is_flag=True, help="强制重新配置（覆盖现有设置）")
def setup(force: bool):
    """全自动初始配置（首次使用）"""
    cfg = load_config()

    console.print(Panel("[bold cyan]elink 初始化配置[/bold cyan]", expand=False))

    # ── 步骤 1: 绑定设备 ──────────────────────────────────────────────
    if cfg.get("device_address") and not force:
        console.print(f"[dim]✓ 设备已绑定: {cfg['device_address']}（用 --force 重新配置）[/dim]")
    else:
        console.print("\n[bold]步骤 1/2[/bold]  绑定墨水屏设备  [dim](最多等待10分钟，Ctrl+C 跳过)[/dim]")
        try:
            devices = asyncio.run(scan_until_found(600.0))
        except (RuntimeError, KeyboardInterrupt) as e:
            if isinstance(e, RuntimeError):
                console.print(f"[red]{e}[/red]")
            else:
                console.print("[dim]跳过设备绑定[/dim]")
            devices = []

        if devices:
            table = Table(header_style="bold cyan")
            table.add_column("#", style="dim", width=3)
            table.add_column("设备名", min_width=20)
            table.add_column("地址", style="cyan")
            table.add_column("RSSI", style="yellow", justify="right")
            for i, (dev, rssi) in enumerate(devices, 1):
                table.add_row(str(i), dev.name or "—", dev.address, f"{rssi} dBm")
            console.print(table)

            if len(devices) == 1:
                selected, _ = devices[0]
                console.print(f"自动选择唯一设备: [cyan]{selected.name or selected.address}[/cyan]")
            else:
                idx         = click.prompt("选择设备编号", type=click.IntRange(1, len(devices)))
                selected, _ = devices[idx - 1]

            cfg["device_address"] = selected.address
            save_config(cfg)
            console.print(f"[green]✓[/green] 设备已绑定: [cyan]{selected.address}[/cyan]")

    # ── 步骤 2: OAuth Token ───────────────────────────────────────────
    if cfg.get("oauth_token") and not force:
        console.print(f"[dim]✓ Token 已配置: {cfg['oauth_token'][:16]}…（用 --force 重新配置）[/dim]")
    else:
        console.print("\n[bold]步骤 2/2[/bold]  配置 Kimi Token")

        token_source = None
        auto_token = None

        # 优先级1: 尝试 CDP 自动获取（Chrome 已登录）
        with console.status("[bold]尝试从 Chrome 获取 Token...[/bold]"):
            cdp_token = _get_token_from_cdp()
        if cdp_token:
            auto_token = cdp_token
            token_source = "Chrome"

        # 优先级2: Keychain
        if not auto_token:
            with console.status("[bold]检测 Keychain...[/bold]"):
                keychain_token = _detect_token_from_keychain()
            if keychain_token:
                auto_token = keychain_token
                token_source = "Keychain"

        # 处理自动获取到的 token
        if auto_token:
            console.print(f"[green]✓[/green] 从 {token_source} 读取到 Token: [dim]{auto_token[:16]}…[/dim]")
            if click.confirm("使用此 Token?", default=True):
                cfg["oauth_token"] = auto_token
                save_config(cfg)
                console.print(f"[green]✓[/green] Token 已从 {token_source} 保存到配置文件")
            else:
                _prompt_manual_token(cfg)
        else:
            console.print("[yellow]未从 Chrome 或 Keychain 找到 Token[/yellow]")
            console.print("[dim]提示: 确保 Chrome 已开启远程调试并登录 kimi.com/code/console[/dim]")
            _prompt_manual_token(cfg)

    # ── 完成 ─────────────────────────────────────────────────────────
    console.print(Panel(
        f"[bold green]配置完成！[/bold green]\n\n"
        f"设备地址: [cyan]{cfg.get('device_address', '[red]未配置[/red]')}[/cyan]\n"
        f"OAuth Token: [dim]{'已配置' if cfg.get('oauth_token') else '[yellow]未配置[/yellow]'}[/dim]\n\n"
        "[dim]立即推送:    [bold]uv run elink.py push[/bold][/dim]\n"
        "[dim]后台定时推送: [bold]uv run elink.py watch[/bold][/dim]",
        expand=False,
    ))


# ─ scan ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--timeout", "-t", default=8.0, show_default=True, help="扫描超时（秒）")
def scan(timeout: float):
    """扫描附近的蓝签设备"""
    with console.status(f"[bold]扫描中 ({timeout:.0f}s)...[/bold]"):
        devices = asyncio.run(scan_devices(timeout))

    if not devices:
        console.print("[red]未发现蓝签设备[/red]")
        return

    table = Table(title="发现的蓝签设备", header_style="bold cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("设备名", min_width=20)
    table.add_column("地址", style="cyan")
    table.add_column("RSSI", style="yellow", justify="right")
    for i, (dev, rssi) in enumerate(devices, 1):
        table.add_row(str(i), dev.name or "—", dev.address, f"{rssi} dBm")
    console.print(table)


# ─ bind ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("address", required=False)
@click.option("--timeout", "-t", default=8.0, show_default=True, help="扫描超时（秒）")
def bind(address: str | None, timeout: float):
    """绑定默认设备（扫描后选择，或直接指定地址）"""
    if address:
        cfg = load_config()
        cfg["device_address"] = address
        save_config(cfg)
        console.print(f"[green]✓[/green] 已绑定: [cyan]{address}[/cyan]")
        return

    with console.status(f"[bold]扫描中 ({timeout:.0f}s)...[/bold]"):
        devices = asyncio.run(scan_devices(timeout))

    if not devices:
        console.print("[red]未发现蓝签设备[/red]")
        return

    table = Table(title="发现的蓝签设备", header_style="bold cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("设备名", min_width=20)
    table.add_column("地址", style="cyan")
    table.add_column("RSSI", style="yellow", justify="right")
    for i, (dev, rssi) in enumerate(devices, 1):
        table.add_row(str(i), dev.name or "—", dev.address, f"{rssi} dBm")
    console.print(table)

    if len(devices) == 1:
        dev0, _ = devices[0]
        if not click.confirm(f"绑定 [{dev0.name or dev0.address}]?", default=True):
            return
        selected = dev0
    else:
        idx         = click.prompt("选择设备编号", type=click.IntRange(1, len(devices)))
        selected, _ = devices[idx - 1]

    cfg = load_config()
    cfg["device_address"] = selected.address
    save_config(cfg)
    console.print(f"[green]✓[/green] 已绑定: [cyan]{selected.name or '?'}[/cyan]  {selected.address}")


# ─ send ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("image_path", type=click.Path(exists=True))
@click.argument("address", required=False)
def send(image_path: str, address: str | None):
    """发送图片到墨水屏"""
    if not address:
        address = load_config().get("device_address")

    with console.status("[bold]转换图片...[/bold]"):
        black_bytes, red_bytes = image_to_eink_bytes(image_path)
    console.print(
        f"[green]✓[/green] 图片转换完成  "
        f"黑色: {len(black_bytes)}B  红色: {len(red_bytes)}B"
    )

    asyncio.run(_do_send(address, black_bytes, red_bytes))


# ─ clear ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("address", required=False)
def clear(address: str | None):
    """清屏（全白）"""
    if not address:
        address = load_config().get("device_address")

    total       = SCREEN_H * math.ceil(SCREEN_W / 8)
    black_bytes = bytes([0xFF] * total)
    red_bytes   = bytes([0x00] * total)

    asyncio.run(_do_send(address, black_bytes, red_bytes))


# ─ push ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("address", required=False)
@click.option("--out", default="/tmp/usage_display.png", show_default=True, help="图片输出路径")
@click.option("--dry-run", is_flag=True, help="只生成图片，不发送")
def push(address: str | None, out: str, dry_run: bool):
    """一条龙：生成 CC 用量图 → 发送到墨水屏"""
    if not address:
        address = load_config().get("device_address")

    with console.status("[bold]获取 Kimi Code 用量数据...[/bold]"):
        image_path = render_usage_image(out)
    console.print(f"[green]✓[/green] 用量图已生成: {image_path}")

    if dry_run:
        console.print("[dim]--dry-run: 跳过发送[/dim]")
        return

    with console.status("[bold]转换图片...[/bold]"):
        black_bytes, red_bytes = image_to_eink_bytes(image_path)

    asyncio.run(_do_send(address, black_bytes, red_bytes))


# ─ watch ─────────────────────────────────────────────────────────────────────

async def _watch_loop(address: str, interval: int, out: str):
    """watch 的异步循环实现"""
    run_count = 0
    while True:
        run_count += 1
        console.rule(f"[bold]第 {run_count} 次  {datetime.now().strftime('%H:%M:%S')}[/bold]")

        try:
            with console.status("[bold]获取用量数据...[/bold]"):
                image_path = render_usage_image(out)
            console.print(f"[green]✓[/green] 用量图已生成")

            with console.status("[bold]转换图片...[/bold]"):
                black_bytes, red_bytes = image_to_eink_bytes(image_path)

            await _do_send(address, black_bytes, red_bytes)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            import traceback
            console.print(f"[red]推送失败: {e}[/red]")
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
            console.print(f"[yellow]将在 {interval} 分钟后重试[/yellow]")

        # ── 倒计时 ────────────────────────────────────────────────────
        seconds = interval * 60
        try:
            with Live(console=console, refresh_per_second=2) as live:
                for remaining in range(seconds, 0, -1):
                    m, s = divmod(remaining, 60)
                    live.update(Text(f"  下次推送: {m:02d}:{s:02d}", style="dim"))
                    await asyncio.sleep(1)
        except KeyboardInterrupt:
            console.print("\n[yellow]已停止[/yellow]")
            break


@cli.command()
@click.argument("address", required=False)
@click.option("--interval", "-i", default=5, show_default=True, help="刷新间隔（分钟）")
@click.option("--out", default="/tmp/usage_display.png", show_default=True, help="图片缓存路径")
def watch(address: str | None, interval: int, out: str):
    """后台模式：定时推送用量到墨水屏（Ctrl+C 退出）"""
    if not address:
        address = load_config().get("device_address")

    if not address:
        console.print(
            "[red]未绑定设备[/red]  先运行 [bold]elink setup[/bold] 或 [bold]elink bind[/bold]"
        )
        raise SystemExit(1)

    console.print(Panel(
        f"[bold]后台模式[/bold]  每 [cyan]{interval}[/cyan] 分钟刷新一次\n"
        f"设备: [cyan]{address}[/cyan]\n"
        "[dim]Ctrl+C 退出[/dim]",
        title="[bold cyan]elink watch[/bold cyan]",
        expand=False,
    ))

    asyncio.run(_watch_loop(address, interval, out))


# ─ config ────────────────────────────────────────────────────────────────────

@cli.group()
def config():
    """配置管理"""


@config.command("set-token")
@click.option("--token", prompt="Kimi Token", hide_input=True, help="Kimi Code Token")
def config_set_token(token: str):
    """设置 OAuth Token"""
    cfg = load_config()
    cfg["oauth_token"] = token
    save_config(cfg)
    console.print("[green]✓[/green] OAuth Token 已保存")


@config.command("clear-token")
def config_clear_token():
    """删除已保存的 OAuth Token（回退到 Keychain 自动检测）"""
    cfg = load_config()
    cfg.pop("oauth_token", None)
    save_config(cfg)
    console.print("[green]✓[/green] Token 已清除，将回退到 Keychain")


@config.command("show")
def config_show():
    """显示当前配置"""
    cfg = load_config()

    table = Table(title="当前配置", header_style="bold cyan")
    table.add_column("Key",   style="cyan", min_width=18)
    table.add_column("Value")

    addr  = cfg.get("device_address") or "[dim]未绑定[/dim]"
    token = cfg.get("oauth_token")
    token_display = f"{token[:16]}…" if token else "[dim]未设置（自动检测 Keychain）[/dim]"

    table.add_row("device_address", addr)
    table.add_row("oauth_token",    token_display)
    table.add_row("config_path",    str(CONFIG_PATH))
    console.print(table)


if __name__ == "__main__":
    cli()
