"""从截图生成 TemplateMatch 模板（按 720p 短边缩放后裁剪）。

纯 Python 实现：PNG 解码/编码用标准库 zlib+struct，JPEG 解码用 Windows GDI+（ctypes），
缩放用 numpy（由 maafw 依赖提供）。不依赖 Pillow / OpenCV，不联网、不启动游戏。

★任务 #18 起模板按 1280×720（image_target 长边 1280）真实帧校准★：
  # 从 debug/vision 下干净的 1280×720 真实帧裁 TOUCH TO START 纯文字区
  python tools/make_template.py --src debug/vision/xxx_HandleLoadingScreen_yyy.jpg \
      --already-720 --out resource/image/touch_to_start.png --crop 468 576 793 599
  # crop 为缩放后图像上的 [x0 y0 x1 y1]，本例得 325×23

  # 目测用：只输出缩放后整图
  python tools/make_template.py --src <png|jpg> --preview <out.png>

校准参数与真实帧 NCC/box 见 artifacts/18-click-fix.md 与 config.recognition.calibration。
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

TARGET_SHORT_SIDE = 720


# ---------- PNG 解码（支持 colortype 0/2/3/4/6，filter 0~4，非隔行） ----------
def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(path: Path) -> np.ndarray:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"不是 PNG: {path}")
    w, h, bitdepth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", data[16:29])
    if bitdepth != 8 or interlace != 0:
        raise ValueError("仅支持 8bit 非隔行 PNG")
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    # 读取 IHDR 之后的所有 chunk
    pos, idat = 8, bytearray()
    palette = None
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype_name = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if ctype_name == b"IDAT":
            idat += chunk
        elif ctype_name == b"PLTE":
            palette = np.frombuffer(chunk, dtype=np.uint8).reshape(-1, 3)
        elif ctype_name == b"IEND":
            break
        pos += 12 + length
    raw = np.frombuffer(zlib.decompress(bytes(idat)), dtype=np.uint8).copy()
    stride = w * bpp
    rows = raw.reshape(h, stride + 1)
    filters = rows[:, 0]
    px = rows[:, 1:].reshape(h, w, bpp).astype(np.uint8)
    prev = np.zeros((1, w, bpp), dtype=np.uint8)
    out = np.zeros((h, w, bpp), dtype=np.uint8)
    for y in range(h):
        f = int(filters[y])
        cur = px[y].astype(np.int32)
        if f == 1:      # Sub
            for i in range(1, w):
                cur[i] = (cur[i] + cur[i - 1]) & 0xFF
        elif f == 2:    # Up
            cur = (cur + prev[0].astype(np.int32)) & 0xFF
        elif f == 3:    # Average
            # 逐像素处理（8bit PNG 每行不大）
            for i in range(w):
                a = cur[i - 1] if i > 0 else 0
                b = int(prev[0, i])
                cur[i] = (cur[i] + ((a + b) // 2)) & 0xFF
        elif f == 4:    # Paeth
            for i in range(w):
                a = int(cur[i - 1]) if i > 0 else 0
                b = int(prev[0, i])
                c = int(prev[0, i - 1]) if i > 0 else 0
                cur[i] = (cur[i] + _paeth(a, b, c)) & 0xFF
        elif f != 0:
            raise ValueError(f"未知 PNG filter: {f}")
        out[y] = cur.astype(np.uint8)
        prev = out[y:y + 1]
    # 统一转成 RGBA
    if ctype == 6:
        rgba = out
    elif ctype == 2:
        rgba = np.concatenate([out, np.full((h, w, 1), 255, np.uint8)], axis=2)
    elif ctype == 0:
        g = np.repeat(out, 3, axis=2)
        rgba = np.concatenate([g, np.full((h, w, 1), 255, np.uint8)], axis=2)
    elif ctype == 4:
        g = np.repeat(out[:, :, :1], 3, axis=2)
        rgba = np.concatenate([g, out[:, :, 1:2]], axis=2)
    elif ctype == 3 and palette is not None:
        idx = out[:, :, 0]
        rgba = np.concatenate([palette[idx], np.full((h, w, 1), 255, np.uint8)], axis=2)
    else:
        raise ValueError(f"不支持的 colortype: {ctype}")
    return rgba


# ---------- JPEG 解码（Windows GDI+ via ctypes，不依赖 Pillow/OpenCV） ----------
def decode_jpeg(path: Path) -> np.ndarray:
    """解码 JPEG 为 RGB uint8 数组。

    ★#18★ `debug/vision/*.jpg` 是 MaaFramework 存下的真实运行帧（TemplateMatcher 只把
    模板缩略图拼在右侧，会影响宽度但不影响帧内容），校准与 --check 必须能直接读它们。
    依赖 Windows 自带 gdiplus.dll；全部函数必须显式声明 argtypes/restype，否则 64 位下
    指针被截断会直接崩溃。
    """
    import ctypes

    class GdiplusStartupInput(ctypes.Structure):
        _fields_ = [
            ("GdiplusVersion", ctypes.c_uint32),
            ("DebugEventCallback", ctypes.c_void_p),
            ("SuppressBackgroundThread", ctypes.c_int),
            ("SuppressExternalCodecs", ctypes.c_int),
        ]

    class BitmapData(ctypes.Structure):
        _fields_ = [
            ("width", ctypes.c_uint32),
            ("height", ctypes.c_uint32),
            ("stride", ctypes.c_int),
            ("pixelFormat", ctypes.c_int),
            ("scan0", ctypes.c_void_p),
            ("reserved", ctypes.c_void_p),
        ]

    class GpRect(ctypes.Structure):
        _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                    ("width", ctypes.c_int), ("height", ctypes.c_int)]

    gdiplus = ctypes.WinDLL("gdiplus.dll")
    gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(GdiplusStartupInput),
                                       ctypes.c_void_p]
    gdiplus.GdiplusStartup.restype = ctypes.c_int
    gdiplus.GdiplusShutdown.argtypes = [ctypes.c_void_p]
    gdiplus.GdipCreateBitmapFromFile.argtypes = [ctypes.c_wchar_p,
                                                 ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateBitmapFromFile.restype = ctypes.c_int
    gdiplus.GdipBitmapLockBits.argtypes = [ctypes.c_void_p, ctypes.POINTER(GpRect),
                                           ctypes.c_int, ctypes.c_int,
                                           ctypes.POINTER(BitmapData)]
    gdiplus.GdipBitmapLockBits.restype = ctypes.c_int
    gdiplus.GdipBitmapUnlockBits.argtypes = [ctypes.c_void_p, ctypes.POINTER(BitmapData)]
    gdiplus.GdipBitmapUnlockBits.restype = ctypes.c_int
    gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDisposeImage.restype = ctypes.c_int

    token = ctypes.c_void_p()
    startup = GdiplusStartupInput(1, None, 0, 0)  # GdiplusVersion=1
    if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None) != 0:
        raise RuntimeError("GdiplusStartup 失败")
    bmp = ctypes.c_void_p()
    try:
        if gdiplus.GdipCreateBitmapFromFile(str(path), ctypes.byref(bmp)) != 0 or not bmp:
            raise RuntimeError(f"GdipCreateBitmapFromFile 失败: {path}")
        data = BitmapData()
        # ImageLockModeRead=1, PixelFormat32bppARGB=0x26200A
        if gdiplus.GdipBitmapLockBits(bmp, None, 1, 0x26200A, ctypes.byref(data)) != 0:
            raise RuntimeError(f"GdipBitmapLockBits 失败: {path}")
        try:
            w, h, stride = int(data.width), int(data.height), int(data.stride)
            if w <= 0 or h <= 0:
                raise RuntimeError(f"图像尺寸非法: {w}x{h}")
            buf = (ctypes.c_ubyte * (stride * h)).from_address(data.scan0)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, stride)
            bgra = arr[:, : w * 4].reshape(h, w, 4)
            return np.ascontiguousarray(bgra[:, :, 2::-1])  # BGRA -> RGB
        finally:
            gdiplus.GdipBitmapUnlockBits(bmp, ctypes.byref(data))
    finally:
        if bmp:
            gdiplus.GdipDisposeImage(bmp)
        gdiplus.GdiplusShutdown(token)


def decode_image(path: Path) -> np.ndarray:
    """按扩展名解码为 RGB(A) uint8 数组：PNG 走纯 Python，JPEG/JPG 走 GDI+。

    ★#21★ 纯 Python 的 PNG 解码只覆盖 8bit 非隔行常见格式；用户/素材里 16bit、隔行、
    调色板异常的 PNG 会抛异常，此时回退到 GDI+（Windows 自带，支持这些格式）。
    """
    path = Path(path)
    if path.suffix.lower() in (".jpg", ".jpeg"):
        return decode_jpeg(path)
    try:
        return decode_png(path)[:, :, :3]
    except Exception:
        return decode_jpeg(path)  # GDI+ 兜底（16bit / 隔行 / 异常调色板）


# ---------- 缩放到短边 720（面积平均，接近框架 INTER_AREA） ----------
def area_resize_short_side(img: np.ndarray, short: int = TARGET_SHORT_SIDE) -> np.ndarray:
    h, w, c = img.shape
    scale = short / min(h, w)
    if abs(scale - 1.0) < 1e-6:
        return img
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    src = img.astype(np.float64)
    ii = np.pad(src.cumsum(0).cumsum(1), ((1, 0), (1, 0), (0, 0)))
    ys0 = np.arange(nh) * h // nh
    ys1 = np.maximum(ys0 + 1, (np.arange(nh) + 1) * h // nh)
    xs0 = np.arange(nw) * w // nw
    xs1 = np.maximum(xs0 + 1, (np.arange(nw) + 1) * w // nw)
    s = (ii[ys1[:, None], xs1[None, :]]
         - ii[ys0[:, None], xs1[None, :]]
         - ii[ys1[:, None], xs0[None, :]]
         + ii[ys0[:, None], xs0[None, :]])
    area = (ys1 - ys0)[:, None] * (xs1 - xs0)[None, :]
    return np.clip(s / area[..., None], 0, 255).astype(np.uint8)


# ---------- PNG 编码（filter 0，RGBA） ----------
def encode_png(img: np.ndarray, path: Path) -> None:
    h, w, c = img.shape
    if c == 3:
        img = np.concatenate([img, np.full((h, w, 1), 255, np.uint8)], axis=2)
    rgba = np.ascontiguousarray(img[:, :, :4].astype(np.uint8))
    rows = np.concatenate([np.zeros((h, 1), np.uint8), rgba.reshape(h, w * 4)], axis=1)
    raw = rows.tobytes()
    comp = zlib.compress(raw, 9)

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按 720p 短边缩放并裁剪 TemplateMatch 模板")
    ap.add_argument("--src", type=Path, required=True, help="源截图 PNG/JPG（JPG 走 GDI+ 解码）")
    ap.add_argument("--out", type=Path, help="输出模板 PNG")
    ap.add_argument("--preview", type=Path, help="仅输出缩放后整图（用于目测 crop 坐标）")
    ap.add_argument("--already-720", action="store_true", help="源图已是 720 短边（运行时截屏），跳过缩放")
    ap.add_argument("--crop", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"),
                    help="缩放后图像上的裁剪框 [x0 y0 x1 y1]")
    args = ap.parse_args(argv)

    img = decode_image(args.src)
    print(f"[decode] {args.src} -> {img.shape[1]}x{img.shape[0]} {'RGB' if img.shape[2] == 3 else 'RGBA'}")
    scaled = img if args.already_720 else area_resize_short_side(img)
    print(f"[scale ] -> {scaled.shape[1]}x{scaled.shape[0]} (短边 {TARGET_SHORT_SIDE})")

    if args.preview:
        encode_png(scaled, args.preview)
        print(f"[preview] 已写出 {args.preview}")
    if args.out:
        if args.crop:
            x0, y0, x1, y1 = args.crop
            crop = scaled[y0:y1, x0:x1]
        else:
            crop = scaled
        encode_png(crop, args.out)
        print(f"[out   ] 已写出 {args.out} ({crop.shape[1]}x{crop.shape[0]})")
    if not args.preview and not args.out:
        print("未指定 --out/--preview，无输出。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
