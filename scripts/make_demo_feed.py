"""Build a simulated toll-plaza camera feed for the MVP demo.

Default (zero setup): uses the public sample image bundled with InsightFace. One adult face becomes the
"test case" identity; the other faces are passers-by. This is a *pipeline smoke test*, not an accuracy
test, because the target in the video comes from the same photo.

Realistic demo: register a consenting volunteer with 2-3 photos, then pass --target with a *different*
photo of the volunteer (or just record a phone video of them and upload that instead).

    python scripts/make_demo_feed.py                       # default smoke test
    python scripts/make_demo_feed.py --target me2.jpg --others crowd_dir/
"""
import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.face_engine import engine, portrait_crop  # noqa: E402


def load_faces(img):
    return [portrait_crop(img, f, margin=0.7, size=200)[0] for f in engine().detect(img)]


def degrade(face, rng):
    """Make a face look like it was captured by a toll plaza camera through a vehicle window."""
    lab = cv2.cvtColor(face, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[..., 0] -= 15
    lab[..., 2] += 8
    face = cv2.cvtColor(lab.clip(0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
    small = cv2.resize(face, (90, 90), interpolation=cv2.INTER_AREA)
    face = cv2.resize(small, (150, 150))
    return cv2.flip(face, 1) if rng.random() < 0.5 else face


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="photo of the test identity to hide in the feed")
    ap.add_argument("--others", help="folder of photos of consenting passers-by")
    ap.add_argument("--out", default=str(ROOT / "data" / "demo"))
    ap.add_argument("--seconds", type=int, default=20)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)

    if args.target:
        target_img = cv2.imread(args.target)
        target = load_faces(target_img)[0]
        others = []
        for p in sorted(Path(args.others).glob("*")) if args.others else []:
            img = cv2.imread(str(p))
            if img is not None:
                others += load_faces(img)
    else:
        from insightface.data import get_image
        img = get_image("t1")
        faces = sorted(engine().detect(img), key=lambda f: f.bbox[0])  # left to right
        # Registration photo for the test case (same source photo: smoke test only).
        cv2.imwrite(str(out_dir / "test_case_photo.jpg"), portrait_crop(img, faces[0], size=320)[0])
        target = portrait_crop(img, faces[0], margin=0.7, size=200)[0]
        others = [portrait_crop(img, f, margin=0.7, size=200)[0] for f in faces[1:]]

    fps, w, h = 25, 1280, 720
    writer = cv2.VideoWriter(str(out_dir / "toll_plaza_feed.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    total = args.seconds * fps
    t_in, t_out = int(total * 0.4), int(total * 0.7)  # target visible from 40% to 70% of the clip

    # Each "vehicle" crosses the frame with a passenger face in the window.
    vehicles = []
    for i in range(max(6, len(others) * 2)):
        vehicles.append({"face": degrade(others[i % len(others)], rng) if others else None,
                         "start": int(rng.integers(0, total - 60)), "lane": int(rng.integers(0, 2))})
    vehicles.append({"face": degrade(target, rng), "start": t_in, "lane": 0, "target": True})

    for n in range(total):
        frame = np.full((h, w, 3), 70, np.uint8)
        cv2.rectangle(frame, (0, 380), (w, h), (55, 55, 55), -1)
        for x in range(0, w, 80):
            cv2.line(frame, (x + (n * 6) % 80, 550), (x + 40 + (n * 6) % 80, 550), (200, 200, 200), 4)
        for v in vehicles:
            dur = (t_out - t_in) if v.get("target") else 90
            k = n - v["start"]
            if not (0 <= k < dur) or v["face"] is None:
                continue
            x = int(w - (w + 420) * k / dur)
            y = 260 if v["lane"] == 0 else 330
            cv2.rectangle(frame, (x, y), (x + 420, y + 230), (35, 60, 130) if not v.get("target") else (40, 40, 40), -1)
            cv2.rectangle(frame, (x + 30, y + 15), (x + 200, y + 175), (90, 110, 120), -1)
            fx, fy = x + 40, y + 20
            face = v["face"]
            x0, x1 = max(0, fx), min(w, fx + face.shape[1])
            if x1 > x0:
                frame[fy:fy + face.shape[0], x0:x1] = face[:, x0 - fx:x1 - fx]
            if v.get("target"):
                cv2.putText(frame, "DHAKA METRO-GA 11-2233", (x + 230, y + 200), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (230, 230, 230), 1)
        noise = rng.normal(0, 5, frame.shape)
        frame = (frame + noise).clip(0, 255).astype(np.uint8)
        cv2.putText(frame, "PADMA BRIDGE TOLL PLAZA - CAM 03 (SIMULATED)", (20, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (255, 255, 255), 2)
        cv2.putText(frame, f"{n / fps:06.2f}s", (w - 160, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        writer.write(frame)
    writer.release()
    print(f"Wrote {out_dir / 'toll_plaza_feed.mp4'} ({args.seconds}s).")
    if not args.target:
        print(f"Register a test case with {out_dir / 'test_case_photo.jpg'} then scan the feed.")


if __name__ == "__main__":
    main()
