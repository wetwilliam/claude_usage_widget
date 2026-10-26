"""產生 usage.ico：深色圓角底 + 用量圓環（與浮窗圓環同風格）。需要 Pillow。"""
from PIL import Image, ImageDraw

SS = 8          # 超取樣倍率，縮小後邊緣較平滑
BASE = 256
SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]


def render():
    n = BASE * SS
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=int(n * 0.22), fill="#2B2A33")

    cx = cy = n / 2
    r = n * 0.33
    w = int(n * 0.12)
    box = (cx - r, cy - r, cx + r, cy + r)
    d.ellipse(box, outline="#4A4856", width=w)
    d.arc(box, start=-90, end=-90 + 360 * 0.68, fill="#3DB89A", width=w)
    # 圓弧端點補圓頭
    import math
    for ang in (-90, -90 + 360 * 0.68):
        x = cx + (r - w / 2) * math.cos(math.radians(ang))   # PIL 線寬往內畫，中心線半徑 = r - w/2
        y = cy + (r - w / 2) * math.sin(math.radians(ang))
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill="#3DB89A")

    # 圓心小亮點（像指針軸心）
    c = n * 0.07
    d.ellipse((cx - c, cy - c, cx + c, cy + c), fill="#FFFFFF")
    return im.resize((BASE, BASE), Image.LANCZOS)


if __name__ == "__main__":
    render().save("usage.ico", sizes=SIZES)
    print("usage.ico written")
