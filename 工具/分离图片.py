#!/usr/bin/env python3
"""
从错题照片中自动分离"有边框/成块的图形"，裁剪保存。

用法:
    python 分离图片.py <图片路径或文件夹> [--out 输出目录] [--min-area 0.01]

说明:
    - 适合: 几何图方框、坐标系、表格、成块的示意图
    - 输出: 每个图形存为 <原文件名>_图N.png, 同时生成一张 _预览.png 供核对
    - 嵌在文字里没有边框的图(受力图等), 见脚本末尾的 README 说明
"""
import argparse
import os
import sys

import cv2
import numpy as np


def load_image(path):
    img = cv2.imread(path)
    if img is None:
        print(f"  [!] 无法读取: {path}")
        return None
    # 控制最长边, 加速处理
    max_side = 3000
    h, w = img.shape[:2]
    if max(h, w) > max_side:
        scale = max_side / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA)
    return img


def find_figures(img, min_area_ratio=0.01):
    """返回 [(x, y, w, h), ...] 图形区域列表(按面积降序)。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # 白底黑字 -> 反色成白字黑底, 便于形态学处理
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # 闭运算: 把图形内部的线条/空隙连通成整块
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    closed = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)

    H, W = img.shape[:2]
    min_area = max(60 * 60, min_area_ratio * H * W)

    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        # 矩形度: 轮廓面积 / 外接矩形面积, 过滤细长文字行
        rect_area = w * h
        if rect_area <= 0:
            continue
        solidity = area / rect_area
        # 长宽比: 图形大致方正(允许稍扁), 文字行通常 > 8:1
        aspect = max(w, h) / max(1, min(w, h))
        if solidity < 0.35:
            continue
        if aspect > 5.0:
            continue
        boxes.append((x, y, w, h))

    # 合并严重重叠的框(大框包含小框时保留大框)
    boxes = merge_nested(boxes)
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    return boxes


def merge_nested(boxes):
    """去掉被更大框几乎完全包含的小框。"""
    keep = []
    for b in boxes:
        x, y, w, h = b
        contained = False
        for o in boxes:
            if b is o:
                continue
            ox, oy, ow, oh = o
            # 当前框的中心是否落在另一个框内, 且对方明显更大
            cx, cy = x + w / 2, y + h / 2
            if (ox <= cx <= ox + ow and oy <= cy <= oy + oh
                    and ow * oh > w * h * 1.5):
                contained = True
                break
        if not contained:
            keep.append(b)
    return keep


def crop_with_margin(img, box, margin=10):
    H, W = img.shape[:2]
    x, y, w, h = box
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(W, x + w + margin), min(H, y + h + margin)
    return img[y0:y1, x0:x1]


def process(path, out_dir, min_area_ratio, show_preview=True):
    print(f"\n[处理] {os.path.basename(path)}")
    img = load_image(path)
    if img is None:
        return
    boxes = find_figures(img, min_area_ratio)
    if not boxes:
        print("  [-] 未检测到图形。若图上图形没有边框, 请手动裁剪或用版面分析(见文末说明)。")
        return

    base = os.path.splitext(os.path.basename(path))[0]
    preview = img.copy()
    for i, (x, y, w, h) in enumerate(boxes, 1):
        crop = crop_with_margin(img, (x, y, w, h))
        out_path = os.path.join(out_dir, f"{base}_图{i}.png")
        cv2.imwrite(out_path, crop)
        print(f"  [+] {os.path.basename(out_path)}  ({w}x{h})")
        cv2.rectangle(preview, (x, y), (x + w, y + h), (0, 0, 255), 3)
        cv2.putText(preview, str(i), (x, max(20, y - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)

    if show_preview:
        prev_path = os.path.join(out_dir, f"{base}_预览.png")
        cv2.imwrite(prev_path, preview)
        print(f"  [预览] {os.path.basename(prev_path)}  (红框=检测到的图形, 编号对应输出文件)")


def main():
    ap = argparse.ArgumentParser(description="错题照片图形自动分离")
    ap.add_argument("input", help="图片文件或文件夹")
    ap.add_argument("--out", default="图片", help="输出目录 (默认: 图片/)")
    ap.add_argument("--min-area", type=float, default=0.01,
                    help="最小图形面积占整页比例 (默认 0.01)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    paths = []
    if os.path.isdir(args.input):
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.PNG", "*.webp"):
            paths += glob_quiet(os.path.join(args.input, ext))
    elif os.path.isfile(args.input):
        paths = [args.input]
    if not paths:
        print("没有找到图片。")
        return

    for p in sorted(paths):
        process(p, args.out, args.min_area)


def glob_quiet(pattern):
    import glob
    return glob.glob(pattern)


if __name__ == "__main__":
    main()

"""
== 嵌在文字里没有边框的图(受力图/电路图/数轴)怎么办? ==
v1 脚本只处理"成块/有边框"的图形。对无边框图, 两种办法:
  1. 手动: 扫描App里裁剪, 或本脚本生成 _预览.png 后目测定位再裁
  2. 自动: 装 PaddleOCR 的 PP-Structure 版面分析, 能自动标出 figure 区域
     pip install paddleocr paddlepaddle   (需要时再装, 体积较大)
"""
