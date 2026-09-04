"""Screen capture module (FR-2.28).

Public surface:
- ScreenCapture: system-level screenshot helper (macOS screencapture, Windows PowerShell)
- ScreenshotIndexEntry: JSONL audit row dataclass

The 4-phase trigger (PHASE A start + PHASE B poll + PHASE C end + PHASE D audit)
lives in `screenshot_phase_controller.py` (plan F2-5).
"""