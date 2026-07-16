"""Generate favicon.ico for Wing Section Studio (a wing over a ground line).

Run once:  .venv\\Scripts\\python.exe app\\static\\make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BG = (11, 16, 27, 255)       # --plane
WING = (57, 135, 229, 255)   # --e1
STEEL = (143, 160, 192, 255)


def render(size: int) -> Image.Image:
    s = 8  # supersample
    W = size * s
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded dark tile
    r = int(W * 0.22)
    d.rounded_rectangle([0, 0, W - 1, W - 1], radius=r, fill=BG)

    # wing section (cambered teardrop), main-ish shape
    def pt(fx, fy):
        return (W * fx, W * fy)

    upper = [pt(0.16, 0.52), pt(0.30, 0.36), pt(0.52, 0.30),
             pt(0.74, 0.34), pt(0.88, 0.44)]
    lower = [pt(0.88, 0.44), pt(0.66, 0.52), pt(0.42, 0.55), pt(0.16, 0.52)]
    d.polygon(upper + lower[1:], fill=WING)

    # ground line
    gy = W * 0.72
    d.line([(W * 0.12, gy), (W * 0.88, gy)], fill=STEEL, width=max(2, int(W * 0.02)))
    # a few ground hatches
    for i in range(6):
        x = W * (0.16 + i * 0.12)
        d.line([(x, gy), (x - W * 0.05, gy + W * 0.05)], fill=STEEL,
               width=max(1, int(W * 0.012)))

    return img.resize((size, size), Image.LANCZOS)


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [render(s) for s in sizes]
    out = HERE / "favicon.ico"
    imgs[0].save(out, format="ICO", sizes=[(s, s) for s in sizes],
                 append_images=imgs[1:])
    # also a PNG for docs / window managers that prefer it
    render(256).save(HERE / "favicon.png")
    print("wrote", out, "and favicon.png")


if __name__ == "__main__":
    main()
