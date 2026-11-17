# -*- coding: utf-8 -*-
"""从桌面截图检测棋子中心并拟合 9x10 网格（圆盘核卷积法），输出标注图/拼版图/坐标。"""
import numpy as np, cv2, json
from PIL import Image, ImageDraw

SRC = r"C:\Users\17601\Desktop\FOA-AL00_20261104_173429.png"

img = Image.open(SRC).convert("RGB")
W0, H0 = img.size                      # 343 x 763
up = img.resize((1084, 2412), Image.LANCZOS)
arr = np.asarray(up).astype(int)
r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

# 棋子面：暖色盘面（笔画会被扣除，不影响卷积峰值）
face = ((r > 175) & (r - b > 45) & (g > 105) & (r < 248)).astype(np.float32)

# 圆盘核卷积：盘面处得分高
R = 50
yy, xx = np.mgrid[-R:R + 1, -R:R + 1]
kern = ((xx * xx + yy * yy) <= R * R).astype(np.float32)
kern /= kern.sum()
score = cv2.filter2D(face, -1, kern)

# 局部极大值
peak = (score == cv2.dilate(score, np.ones((61, 61), np.uint8))) & (score > 0.22)
pys, pxs = np.where(peak)
cents = [(float(x), float(y), float(score[y, x])) for x, y in zip(pxs, pys)]
cents.sort(key=lambda c: -c[2])
kept = []
for x, y, s in cents:                                   # 非极大值抑制
    if all((x - kx) ** 2 + (y - ky) ** 2 > 80 ** 2 for kx, ky, _ in kept):
        kept.append((x, y, s))
print("检测到棋子:", len(kept))

xs = sorted(c[0] for c in kept)
ys = sorted(c[1] for c in kept)

# ---- 等距网格 RANSAC：从检测中心里找出 9 列/10 行的最优等距网格 ---- #
def best_grid(vals, n, lo, hi, plo, phi):
    best = (0, None, None)
    vals = np.array(vals)
    for pitch in np.arange(plo, phi, 0.5):
        for off in np.arange(lo, hi, 1.0):
            ks = np.round((vals - off) / pitch)
            d = np.abs(vals - (off + ks * pitch))
            hit = ((d < 25) & (ks >= 0) & (ks <= n - 1)).sum()
            if hit > best[0]:
                best = (hit, off, pitch)
    _, off, pitch = best
    # 用命中点做一次最小二乘精修
    ks = np.round((vals - off) / pitch)
    m = (np.abs(vals - (off + ks * pitch)) < 25) & (ks >= 0) & (ks <= n - 1)
    A = np.vstack([ks[m], np.ones(m.sum())]).T
    (a, bv), *_ = np.linalg.lstsq(A, vals[m], rcond=None)
    return [a * k + bv for k in range(n)], best[0]

GX, hx = best_grid(xs, 9, 30, 130, 100, 130)
GY, hy = best_grid(ys, 10, 600, 780, 100, 130)
print(f"网格命中: 列 {hx}/{len(xs)}  行 {hy}/{len(ys)}")
# 剔除不在网格交点附近的检测（HUD 图标等）
def near_grid(x, y):
    i = int(round((x - GX[0]) / (GX[1] - GX[0])))
    j = int(round((y - GY[0]) / (GY[1] - GY[0])))
    return 0 <= i <= 8 and 0 <= j <= 9 and abs(x - GX[i]) < 30 and abs(y - GY[j]) < 30
kept = [c for c in kept if near_grid(c[0], c[1])]
print("棋盘上棋子:", len(kept))

def cluster(vals, gap=60):
    vals = sorted(vals)
    groups, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= gap:
            cur.append(v)
        else:
            groups.append(cur); cur = [v]
    groups.append(cur)
    return [float(np.mean(g)) for g in groups]

cx, cy = cluster(xs), cluster(ys)
print("列聚类:", [round(v) for v in cx])
print("行聚类:", [round(v) for v in cy])

occ = {}
for (x, y, s) in kept:
    i = int(np.argmin([abs(x - gx) for gx in GX]))
    j = int(np.argmin([abs(y - gy) for gy in GY]))
    occ.setdefault((i, j), []).append((round(x), round(y)))
print("占用交点数:", len(occ))
empty = [(i, j) for j in range(10) for i in range(9) if (i, j) not in occ]
print("空交点:", empty)

# 标注图
im = up.copy()
dr = ImageDraw.Draw(im)
for gx in GX:
    dr.line([(gx, GY[0]), (gx, GY[9])], fill=(0, 160, 255), width=3)
for gy in GY:
    dr.line([(GX[0], gy), (GX[8], gy)], fill=(0, 160, 255), width=3)
for (i, j), lst in occ.items():
    x, y = lst[0]
    dr.ellipse([x - 52, y - 52, x + 52, y + 52], outline=(0, 255, 0), width=4)
    dr.text((x - 14, y - 78), f"{i},{j}", fill=(255, 0, 255))
im.save(r"C:\pikafish\api\_scan_full.png")

# 棋子拼版（供字形判读）
order = sorted(occ.items(), key=lambda kv: (kv[0][1], kv[0][0]))
tiles = []
for (i, j), lst in order:
    x, y = lst[0]
    crop = up.crop((int(x) - 56, int(y) - 56, int(x) + 57, int(y) + 57))
    tiles.append(((i, j), crop))
cols = 8
rows_n = (len(tiles) + cols - 1) // cols
sheet = Image.new("RGB", (cols * 116, rows_n * 116), (255, 255, 255))
dr2 = ImageDraw.Draw(sheet)
for k, ((i, j), crop) in enumerate(tiles):
    px, py = (k % cols) * 116, (k // cols) * 116
    sheet.paste(crop, (px + 2, py + 2))
    dr2.text((px + 46, py + 96), f"{i},{j}", fill=(255, 0, 255))
sheet.save(r"C:\pikafish\api\_sheet.png")

json.dump({"GX": GX, "GY": GY,
           "occ": {f"{i},{j}": lst for (i, j), lst in occ.items()}},
          open(r"C:\pikafish\api\_scan.json", "w"))
print("输出 _scan_full.png / _sheet.png / _scan.json")
