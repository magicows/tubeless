"""
QR codes for the upload-page screen. Uses whichever QR library the OS provides (segno on newer Raspberry Pi OS,
qrcode on older), and degrades to "no QR code, just the address" if neither is installed.
"""
from __future__ import annotations

from typing import List, Optional


def matrix(text: str) -> Optional[List[List[bool]]]:
    """The QR code for `text` as rows of dark (True) / light (False) modules, without the quiet zone."""
    try:
        import segno                                           # noqa: WPS433
        qr = segno.make(text, error="m", micro=False)
        return [[bool(v) for v in row] for row in qr.matrix]
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import qrcode                                          # noqa: WPS433
        q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
        q.add_data(text)
        q.make(fit=True)
        return [[bool(v) for v in row] for row in q.get_matrix()]
    except Exception:
        return None
