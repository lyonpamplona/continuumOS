import math
from PIL import Image, ImageDraw


def make_icon(size):
    S = size * 4
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(S * 0.23)
    # fundo arredondado (tema Console)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=(13, 13, 15, 255))
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, outline=(42, 42, 48, 255), width=max(2, S // 110))
    # arco laranja em forma de C (aberto a direita)
    pad = int(S * 0.27)
    bbox = [pad, pad, S - pad, S - pad]
    w = int(S * 0.135)
    d.arc(bbox, start=48, end=312, fill=(255, 106, 0, 255), width=w)
    # ponta de destaque (amarelo) no fim superior do arco
    cx, cy = S / 2.0, S / 2.0
    rad = (S - 2 * pad) / 2.0
    ang = math.radians(312)
    dx = cx + rad * math.cos(ang)
    dy = cy + rad * math.sin(ang)
    dr = w * 0.55
    d.ellipse([dx - dr, dy - dr, dx + dr, dy + dr], fill=(255, 176, 32, 255))
    return img.resize((size, size), Image.LANCZOS)


make_icon(192).save('static/icons/icon-192.png')
make_icon(512).save('static/icons/icon-512.png')
make_icon(256).save('static/logo.png')
print('assets OK: icon-192, icon-512, logo.png')
