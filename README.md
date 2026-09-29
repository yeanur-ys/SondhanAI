# Shondhan AI (সন্ধান) — MVP

Privacy-first missing-child search: register a case → generate predicted looks → match camera footage
against the watchlist only → human verification → alert police → trace the route.

Built for Grameenphone FutureMakers 2026 (AI for Social Good · Safety, Security and Data Privacy).

## Run it

```bash
./run.sh                      # first run creates .venv and installs dependencies (~2 min)
open http://127.0.0.1:8000
```

Face models (InsightFace `buffalo_l`, ~280 MB) download automatically on first start to `~/.insightface`.
Requires Python 3.10+ (tested on 3.12, Apple Silicon, CPU only).

Demo accounts: `admin / admin123` · `officer / officer123` · `registrar / registrar123`

## 3-minute demo

1. `.venv/bin/python scripts/make_demo_feed.py` — builds `data/demo/toll_plaza_feed.mp4` (simulated Padma
   Bridge toll camera) and `data/demo/test_case_photo.jpg` from a public sample image bundled with InsightFace.
2. **Register case** → upload `test_case_photo.jpg`, tick guardian consent, fill GD number. 50 predicted looks
   are generated in ~5 s.
3. **Scan footage** → camera "Padma Bridge Toll Plaza (Jajira)", upload the feed, plate `DHAKA METRO-GA 11-2233`.
   ~7 s later an alert appears. Every other face is counted as *discarded*.
4. **Alerts** → review the snapshot next to the real and predicted photos → *Confirm*. Simulated SMS go to the
   nearest station, highway police and the case officer.
5. Scan the same feed again as "Padma Bridge Toll Plaza (Mawa)" with a later start time and confirm → the case
   page shows the route and suggested interception checkpoints towards Dhaka.
6. **Live camera** → point your webcam at a registered volunteer for real-time matching.
7. Close the case as *found* → all photos, looks, signatures and snapshots are deleted.

For a convincing demo, register a **consenting volunteer** with 2–3 photos and record a separate phone video of
them walking past a camera, possibly with cap/glasses. Never use real missing children's data.

## How it maps to the proposal

| Proposal step | Code |
|---|---|
| 1 Case registration (GD + consent required) | `backend/app/cases.py` `register_case` |
| 2 Up to 50 predicted looks, mix depends on time missing | `backend/app/variations.py` |
| 3 Face signatures (ArcFace 512-d), real photos weighted higher | `face_engine.py`, `watchlist.py` |
| 4 Matching on footage/streams, quality gate, non-matches discarded | `scanner.py` |
| 5 Human verification → dispatch | `main.py` `/api/alerts/{id}/review`, `dispatch.py` |
| 6 Vehicle + route + next checkpoint | `dispatch.py` `case_route` |
| Responsible AI: encryption, RBAC, audit, auto-deletion | `security.py`, `cases.close_case` |

**Privacy mechanics that are actually implemented**
- Watchlist-only: FAISS index contains only active cases. There is no index of the public.
- Non-matching faces: the signature is dropped in memory; only a counter is incremented.
- Uploaded footage is deleted after the scan. Rejected alerts have their snapshots deleted.
- All images are encrypted at rest (Fernet/AES). Key: `data/secret.key` (use a KMS in production).
- Predicted looks are labelled `PREDICTED` everywhere and are checked to still resemble the real photo
  (identity check), otherwise discarded, which reduces false matches from generated looks.
- A possible match needs several frames (or one very strong frame) before an alert is raised.
- Every login, case, scan, review and closure is written to the audit log.

## Tuning (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SHONDHAN_MATCH_THRESHOLD` | 0.40 | cosine similarity for a possible match |
| `SHONDHAN_STRONG_THRESHOLD` | 0.55 | single-frame alert |
| `SHONDHAN_MIN_HITS` | 2 | frames needed within `SHONDHAN_HIT_WINDOW` s |
| `SHONDHAN_GENERATED_PENALTY` | 0.04 | score penalty for predicted looks |
| `SHONDHAN_SCAN_FPS` | 5 | frames per second analysed |
| `SHONDHAN_MAX_LOOKS` | 50 | predicted looks per case |

In the demo, impostor faces scored ≤ 0.07 and the target scored 0.82. Thresholds for **children** must be
calibrated on a proper evaluation set before any pilot.

## Known limitations (be upfront with judges)

- **Age progression is not yet a trained model.** `variations.AgeProgressor` is the plug-in point for a GAN or
  diffusion ageing model on a local GPU. Until then, cases missing more than 6 months show a warning, and looks
  cover disguise, living-condition and camera changes only. Shaved heads and hairstyle changes also need the
  generative model.
- Disguise edits (cap, scarf, glasses) are landmark-guided overlays, not photorealistic generation.
- ArcFace is trained mostly on adults. Child and cross-age accuracy, and fairness across skin tones, still
  need testing on consented South Asian child data (proposal §6.8).
- Number plates come from the scan form (simulating a toll/ANPR feed); there is no plate OCR yet.
- Route prediction is a direction-of-travel heuristic, not a road-network model.
- SMS are simulated (`dispatch.send_sms`). Demo users and in-memory sessions stand in for police SSO.
- The camera coordinates are approximate and for demonstration only.

## Next steps

1. Integrate an age-progression model behind `AgeProgressor` (local GPU, no third-party upload).
2. Build an evaluation script: TAR at a fixed FAR, split by age and skin tone.
3. Add ANPR (plate detection + OCR) to the scanner.
4. Build an edge agent (Jetson / mini PC) that runs `process_frame` next to the camera and sends only alerts.
5. Connect an SMS gateway and a police identity provider; move the key to a KMS.
