# -*- coding: utf-8 -*-
"""
把 FOA-AL00（scrcpy 投屏）窗口的当前画面截图保存到桌面。

用法：
    python shot.py                 # 保存到 桌面/FOA-AL00_时间戳.png
    python shot.py 棋盘.png         # 指定文件名（仍在桌面）
    python shot.py --scale 1084    # 放大到手机分辨率宽度后保存
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime
import os
import time

from PIL import Image, ImageGrab

TITLE = "FOA-AL00"
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # 高 DPI 下坐标不错位
except Exception:
    user32.SetProcessDPIAware()


def find_window(title=TITLE):
    """返回标题为 title 的可见窗口句柄（自动排除 scrcpy 控制台窗口）。"""
    hits = []
    buf, rect = ctypes.create_unicode_buffer(300), wt.RECT()

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            user32.GetWindowTextW(hwnd, buf, 300)
            if buf.value.strip() == title:
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                hits.append((hwnd, rect.right - rect.left, rect.bottom - rect.top))
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb)
    user32.EnumWindows(CB, 0)
    if not hits:
        raise RuntimeError(f"未找到标题为 {title} 的可见窗口")
    # 多个同名窗口时取最高（竖屏）的那个
    return max(hits, key=lambda t: t[2])[0], hits


def capture(hwnd, scale_width=None):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                       # 还原最小化窗口
        time.sleep(0.6)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.45)

    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    if r.right <= 0 or r.bottom <= 0:
        raise RuntimeError("窗口客户区尺寸为 0，可能仍被最小化")

    img = ImageGrab.grab(bbox=(pt.x, pt.y, pt.x + r.right, pt.y + r.bottom),
                         all_screens=True)
    if scale_width:
        h = round(img.height * scale_width / img.width)
        img = img.resize((scale_width, h), Image.LANCZOS)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", default=None, help="输出文件名（默认带时间戳）")
    ap.add_argument("--scale", type=int, default=None, help="放大到指定宽度")
    ap.add_argument("--dir", default=DESKTOP, help="输出目录")
    args = ap.parse_args()

    hwnd, hits = find_window()
    img = capture(hwnd, args.scale)

    os.makedirs(args.dir, exist_ok=True)
    name = args.name or f"FOA-AL00_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
        name += ".png"
    out = os.path.join(args.dir, name)
    img.save(out)
    print(f"[ok] {out}  {img.size[0]}x{img.size[1]}")


if __name__ == "__main__":
    main()
