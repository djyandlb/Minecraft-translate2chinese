# -*- coding: utf-8 -*-
"""从 pixel-translation-icon.svg（矢量源）直接渲染应用图标（ico/png）。

用 svglib 渲染 SVG 源而非手工重绘——保留矢量全部细节（圆角、多边形抗锯齿、光标等），
所有尺寸（16~256）都来自同一矢量源，小图标不丢细节。
产物：assets/app-icon.ico（多尺寸 16~256）、assets/app-icon.png、assets/pack.png。
SVG 本身用作前端 favicon（矢量自适应）。

依赖：svglib、reportlab、Pillow（仅生成图标用，不进入运行时打包）。
"""
import io
from pathlib import Path

from PIL import Image
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "assets" / "pixel-translation-icon.svg"


def _render(size: int) -> Image.Image:
    """按目标尺寸渲染 SVG 源。

    reportlab renderPM 输出像素 = drawing 尺寸(点) × dpi/72，而 svg2rlg 的 width 为 viewBox 64。
    drawing.scale 不更新 width/height（之前所有尺寸都渲染成 64px → ico 帧全错乱，图标只一角），
    改用 dpi 精确控制输出像素 = 64 × dpi/72 = size。
    """
    drawing = svg2rlg(str(SVG))
    dpi = round(72 * size / drawing.width)
    png = renderPM.drawToString(drawing, fmt="PNG", dpi=dpi)
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    # reportlab 渲染时把圆角外（SVG 透明区）填成纯白画布底 → 转透明，否则 ico 四角是白块
    img = img.point(lambda v: v)   # no-op 保引用
    new = [(0, 0, 0, 0) if p == (255, 255, 255, 255) else p for p in img.getdata()]
    img.putdata(new)
    return img


def _write_ico(frames: list[Image.Image], path: Path) -> None:
    """手动构造多尺寸 ICO：ICONDIR + 目录 + PNG 压缩图像（Windows Vista+ 支持 PNG 帧）。

    Pillow 的 ICO 多尺寸保存在此版本有 bug（append_images 只落第一帧，ico 只有 16x16），
    改为手写容器：每个尺寸一张 PNG，全部写入，Windows 各场景取对应尺寸。
    """
    import struct
    images = []
    for f in frames:
        buf = io.BytesIO()
        f.save(buf, format="PNG")
        images.append(buf.getvalue())
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)          # reserved / type=icon / count
    entries = b""
    offset = 6 + 16 * count                            # 头 + 目录之后
    for img, f in zip(images, frames):
        dim = 0 if f.width >= 256 else f.width         # 256 用 0 表示
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(img), offset)
        offset += len(img)
    path.write_bytes(header + entries + b"".join(images))


def main() -> None:
    out_dir = ROOT / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 多尺寸 ico（exe / 安装包）：16~256，全部来自同一矢量源（手写容器，避免 Pillow bug）
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [_render(s) for s in ico_sizes]
    _write_ico(frames, out_dir / "app-icon.ico")
    # 256 PNG（源/资源包图标用）
    base = _render(256)
    base.save(str(out_dir / "app-icon.png"), format="PNG")
    base.save(str(out_dir / "pack.png"), format="PNG")
    print("已生成:", out_dir / "app-icon.ico", "app-icon.png", "pack.png")


if __name__ == "__main__":
    main()
