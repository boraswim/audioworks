"""Draws the Audioworks icon (app/assets/audioworks.png and .ico). Run again after changing the design.

  .venv\\Scripts\\python.exe -m app.make_icon
"""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

ASSETS = Path(__file__).resolve().parent / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)


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


def main():
    QGuiApplication.instance() or QGuiApplication([])
    ASSETS.mkdir(exist_ok=True)
    draw(256).save(str(ASSETS / "audioworks.png"))
    write_ico([draw(s) for s in SIZES], ASSETS / "audioworks.ico")
    print(f"Wrote {ASSETS / 'audioworks.png'} and {ASSETS / 'audioworks.ico'} ({', '.join(map(str, SIZES))} px)")


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
