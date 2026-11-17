# -*- coding: utf-8 -*-
"""JJ象棋 棋盘识别：原始帧缓冲 -> 局面 FEN"""
import struct
import sys

import numpy as np

RAW = r"C:\pikafish\api\_raw.bin"


def load_raw(path=RAW):
    d = open(path, "rb").read()
    w, h, fmt = struct.unpack("<III", d[:12])
    body = d[16:] if len(d) == w * h * 4 + 16 else d[12:]
    arr = np.frombuffer(body, dtype=np.uint8)[: w * h * 4].reshape(h, w, 4)
    return arr[:, :, [0, 1, 2]].astype(np.int16)  # RGB


def explore(img):
    h, w, _ = img.shape
    r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    # 米色棋盘底
    beige = (r > 185) & (g > 168) & (b > 130) & (b < 215) & (r - b > 15)
    rows = beige.sum(axis=1)
    cols = beige.sum(axis=0)
    ys = np.where(rows > w * 0.25)[0]
    xs = np.where(cols > h * 0.25)[0]
    print("beige bbox: x", xs.min(), xs.max(), " y", ys.min(), ys.max())
    print("sample colors:")
    for (yy, xx, name) in [(700, 500, "board?"), (1200, 540, "center"), (300, 540, "topbar"),
                           (2000, 540, "bottombar"), (1240, 60, "left edge")]:
        print(f"  {name} ({yy},{xx}) = {img[yy, xx]}")
    # 棋盘内部暗线投影（只在米色区域内统计）
    y0, y1 = ys.min(), ys.max()
    x0, x1 = xs.min(), xs.max()
    sub = img[y0:y1 + 1, x0:x1 + 1]
    gray = sub.mean(axis=2)
    dark = gray < 120
    cprof = dark.sum(axis=0)
    rprof = dark.sum(axis=1)
    H, W = dark.shape
    cx = [i + x0 for i in range(W) if cprof[i] > H * 0.35]
    cy = [j + y0 for j in range(H) if rprof[j] > W * 0.35]
    print("vertical line candidate x:", group(cx))
    print("horizontal line candidate y:", group(cy))


def group(vals):
    if not vals:
        return []
    out, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= 3:
            cur.append(v)
        else:
            out.append(cur); cur = [v]
    out.append(cur)
    return [int(np.mean(c)) for c in out]


if __name__ == "__main__":
    img = load_raw(sys.argv[1] if len(sys.argv) > 1 else RAW)
    print("shape", img.shape)
    explore(img)
