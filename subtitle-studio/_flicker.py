"""Measure temporal jitter of a video's subtitle band = mean frame-to-frame abs pixel
change in that region. Higher = more shimmer/flicker. Usage: _flicker.py <video> <y0> <y1>"""
import sys, cv2, numpy as np
vid, y0, y1 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
cap = cv2.VideoCapture(vid)
prev = None; s = 0.0; n = 0
while True:
    ok, f = cap.read()
    if not ok:
        break
    band = f[y0:y1].astype(np.float32)
    if prev is not None:
        s += float(np.mean(np.abs(band - prev))); n += 1
    prev = band
cap.release()
print(f"{vid.split(chr(92))[-1]}: flicker={s/n:.2f} over {n} frames" if n else "no frames")
