"""Draws the Vocalworks icon (app/assets/vocalworks.png and .ico) and the wide capsule image used in
READMEs (app/assets/vocalworks-capsule.png). Run again after changing the design.

  .venv\\Scripts\\python.exe -m app.make_icon
"""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen)

ASSETS = Path(__file__).resolve().parent / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)
CAPSULE = (920, 430)  # 2x the 460x215 Steam capsule ratio


def draw(size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 256, size / 256)  # design on a 256 grid

    # background: rounded square, blue to purple
    grad = QLinearGradient(0, 0, 256, 256)
    grad.setColorAt(0, QColor("#1e88e5"))
    grad.setColorAt(1, QColor("#8e24aa"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(8, 8, 240, 240), 56, 56)

    white = QColor("#ffffff")
    # microphone capsule
    p.setBrush(white)
    p.drawRoundedRect(QRectF(100, 46, 56, 104), 28, 28)
    # grille lines (only where they stay visible)
    if size >= 48:
        p.setPen(QPen(QColor(30, 136, 229, 110), 5, Qt.SolidLine, Qt.RoundCap))
        for y in (78, 96, 114):
            p.drawLine(QPointF(112, y), QPointF(144, y))

    # holder (U shape), stem and base
    pen = QPen(white, 13, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    holder = QPainterPath()
    holder.moveTo(80, 112)
    holder.cubicTo(80, 176, 176, 176, 176, 112)
    p.drawPath(holder)
    p.drawLine(QPointF(128, 160), QPointF(128, 200))
    p.drawLine(QPointF(98, 204), QPointF(158, 204))

    # sound waves either side (dropped at the smallest sizes, where they'd blur into the mic)
    if size >= 32:
        p.setPen(QPen(white, 11, Qt.SolidLine, Qt.RoundCap))
        for r, alpha in ((70, 255), (96, 150)):
            c = QColor(white)
            c.setAlpha(alpha)
            p.setPen(QPen(c, 11, Qt.SolidLine, Qt.RoundCap))
            box = QRectF(128 - r, 98 - r, 2 * r, 2 * r)
            p.drawArc(box, 150 * 16, 60 * 16)   # left
            p.drawArc(box, -30 * 16, 60 * 16)   # right
    p.end()
    return img


def draw_capsule():
    """Wide banner: the icon on the left, the name and a tagline on the right."""
    w, h = CAPSULE
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)

    # background: the icon's blue-to-purple, a shade deeper so the icon tile stands out
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0, QColor("#1565c0"))
    grad.setColorAt(1, QColor("#6a1b9a"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(0, 0, w, h), 40, 40)

    # icon with a soft drop shadow
    icon_size = 300
    x, y = 56, (h - icon_size) / 2
    tile = QRectF(x + 9, y + 9, icon_size - 18, icon_size - 18)  # the icon's rounded square inside its 256 grid
    for i in range(12, 0, -1):
        p.setBrush(QColor(20, 0, 60, 7))
        p.drawRoundedRect(tile.adjusted(-i, -i + 10, i, i + 10), 66 + i, 66 + i)
    p.drawImage(QPointF(x, y), draw(icon_size))

    # name and tagline, shrunk to fit the space right of the icon
    left = x + icon_size + 44
    room = w - left - 56
    title = QFont("Segoe UI", weight=QFont.Bold)
    title.setPixelSize(104)
    while QFontMetricsF(title).horizontalAdvance("Vocalworks") > room:
        title.setPixelSize(title.pixelSize() - 2)
    tag = QFont("Segoe UI Semibold")
    tag.setPixelSize(32)
    title_h = QFontMetricsF(title).height()
    tag_h = QFontMetricsF(tag).lineSpacing() * 2
    top = (h - title_h - 8 - tag_h) / 2
    p.setPen(QColor("#ffffff"))
    p.setFont(title)
    p.drawText(QRectF(left, top, room, title_h), Qt.AlignLeft | Qt.AlignVCenter, "Vocalworks")
    p.setPen(QColor(255, 255, 255, 200))
    p.setFont(tag)
    p.drawText(QRectF(left + 4, top + title_h + 8, room, tag_h), Qt.AlignLeft,
               "Sing covers that sound\nlike the original")
    p.end()
    return img


def main():
    QGuiApplication.instance() or QGuiApplication([])
    ASSETS.mkdir(exist_ok=True)
    draw(256).save(str(ASSETS / "vocalworks.png"))
    write_ico([draw(s) for s in SIZES], ASSETS / "vocalworks.ico")
    draw_capsule().save(str(ASSETS / "vocalworks-capsule.png"))
    print(f"Wrote {ASSETS / 'vocalworks.png'} and {ASSETS / 'vocalworks.ico'} ({', '.join(map(str, SIZES))} px)")
    print(f"Wrote {ASSETS / 'vocalworks-capsule.png'} ({CAPSULE[0]}x{CAPSULE[1]} px)")


def write_ico(images, path):
    """Multi-size .ico with PNG-compressed entries (supported since Windows Vista)."""
    import struct

    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    blobs = []
    for img in images:
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        blobs.append((img.width(), bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, payload = b"", b""
    for w, blob in blobs:
        entries += struct.pack("<BBBBHHII", w % 256, w % 256, 0, 0, 1, 32, len(blob), offset + len(payload))
        payload += blob
    Path(path).write_bytes(header + entries + payload)


if __name__ == "__main__":
    main()
