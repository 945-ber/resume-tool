"""本地 OCR 模块：RapidOCR（onnxruntime），0 token 成本

首次调用加载模型约 5-7s，之后引擎常驻内存，单图识别约 1-2s。
识别失败/无结果时返回空字符串，由调用方决定是否走视觉 API 兜底。
"""
import time

_ENGINE = None
_LOADED_AT = None


def _get_engine():
    global _ENGINE, _LOADED_AT
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
        _LOADED_AT = time.time()
    return _ENGINE


def ocr_image(image_path: str, max_px: int = 2200) -> str:
    """本地 OCR 单张图片，返回拼接文本；失败返回空字符串。

    max_px: 超过该长边的图先等比压缩（超长截图必须压，否则识别框爆内存）。
    """
    from PIL import Image

    try:
        img = Image.open(image_path)
        w, h = img.size
        if max(w, h) > max_px:
            scale = max_px / float(max(w, h))
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            tmp = image_path + ".ocr_tmp.png"
            img.save(tmp)
            image_path = tmp
        engine = _get_engine()
        result, _ = engine(image_path)
        if image_path.endswith(".ocr_tmp.png"):
            import os
            try:
                os.remove(image_path)
            except Exception:
                pass
        if not result:
            return ""
        lines = [str(item[1]).strip() for item in result if item and len(item) > 1 and str(item[1]).strip()]
        return "\n".join(lines)
    except Exception:
        return ""


def is_available() -> bool:
    try:
        _get_engine()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    p = sys.argv[1]
    t0 = time.time()
    txt = ocr_image(p)
    print(f"耗时 {round(time.time() - t0, 1)}s, 字数 {len(txt)}")
    print(txt[:2000])
