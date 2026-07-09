"""
Permitlify product demo screen-recording.
Logs in as the dev admin, walks through the entire client + admin app,
saves a webm via Playwright, then ffmpeg-converts to a clean mp4.

Output: exports/demo/permitlify-walkthrough.mp4
"""
import os
import time
import shutil
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright, Page

BASE = "http://127.0.0.1:5000"
EMAIL = "admin@permitlify.com"
PASSWORD = "windows20824193"
W, H = 1600, 900

OUT_DIR = Path("exports/demo")
OUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR = OUT_DIR / "raw"
if RAW_DIR.exists():
    shutil.rmtree(RAW_DIR)
RAW_DIR.mkdir()


def hold(page: Page, secs: float):
    """Idle wait so the viewer can read the screen."""
    page.wait_for_timeout(int(secs * 1000))


def smooth_scroll(page: Page, target_px: int, duration_ms: int = 1800):
    """JS-driven smooth scroll so it looks like a human glided down the page."""
    page.evaluate(
        """({y, dur}) => new Promise(res => {
            const start = window.scrollY;
            const delta = y - start;
            const t0 = performance.now();
            function step(now){
                const t = Math.min(1, (now - t0) / dur);
                const ease = t<.5 ? 2*t*t : 1 - Math.pow(-2*t+2,2)/2;
                window.scrollTo(0, start + delta*ease);
                if(t<1) requestAnimationFrame(step); else res();
            }
            requestAnimationFrame(step);
        })""",
        {"y": target_px, "dur": duration_ms},
    )
    page.wait_for_timeout(duration_ms + 100)


def goto(page: Page, path: str, settle: float = 1.4):
    page.goto(BASE + path, wait_until="domcontentloaded", timeout=20000)
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass
    hold(page, settle)


def tour_page(page: Page, path: str, label: str, scrolls=None, hold_s=2.5):
    """Navigate, optional smooth scrolls, hold for reading."""
    print(f"  → {path}  ({label})")
    goto(page, path)
    hold(page, 1.4)
    if scrolls:
        for y, dur in scrolls:
            smooth_scroll(page, y, dur)
            hold(page, 1.0)
        smooth_scroll(page, 0, 1200)
    else:
        hold(page, hold_s)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path="/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(RAW_DIR),
            record_video_size={"width": W, "height": H},
            device_scale_factor=1,
        )
        page = ctx.new_page()

        # ─── 1. Login ────────────────────────────────────────────
        print("Login…")
        goto(page, "/login/", settle=1.8)
        page.fill('input[name="email"]', EMAIL)
        hold(page, 0.4)
        page.fill('input[name="password"]', PASSWORD)
        hold(page, 0.6)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle", timeout=15000)
        hold(page, 1.5)

        # ─── 2. CLIENT TOUR ──────────────────────────────────────
        print("Client tour…")
        tour_page(page, "/dashboard/", "Dashboard",
                  scrolls=[(500, 1800), (1100, 1800), (1700, 1800)])
        tour_page(page, "/permits/",   "Permits feed",
                  scrolls=[(450, 1800), (900, 1800)])
        tour_page(page, "/notifications/", "Notification center",
                  scrolls=[(400, 1500)])
        tour_page(page, "/settings/", "Settings",
                  scrolls=[(500, 1500), (1000, 1500)])
        tour_page(page, "/settings/alerts/", "Alert delivery preferences",
                  scrolls=[(450, 1500)])
        tour_page(page, "/profile/", "User profile",
                  scrolls=[(500, 1500)])
        tour_page(page, "/api/", "Agency API portal",
                  scrolls=[(500, 1500), (1000, 1500)])
        tour_page(page, "/billing/portal/", "Billing portal",
                  scrolls=[(500, 1500)])
        tour_page(page, "/support/", "Support center",
                  scrolls=[(400, 1300)])

        # ─── 3. ADMIN TOUR ───────────────────────────────────────
        print("Admin tour…")
        tour_page(page, "/admin-panel/", "Admin overview",
                  scrolls=[(500, 1800), (1100, 1800), (1700, 1800)])
        tour_page(page, "/admin-panel/scrapers/", "Scraper mission control",
                  scrolls=[(500, 1800), (1000, 1800)])
        tour_page(page, "/admin-panel/scrapers/cron/", "Daily cron schedule",
                  scrolls=[(500, 1500), (1000, 1500)])
        tour_page(page, "/admin-panel/scrapers/accela-search/", "Accela permit-search finder",
                  scrolls=[(500, 1500)])
        tour_page(page, "/admin-panel/states/", "State permit stats",
                  scrolls=[(500, 1500)])
        tour_page(page, "/admin-panel/users/", "User management",
                  scrolls=[(500, 1500), (1000, 1500)])
        tour_page(page, "/admin-panel/revenue/", "Revenue dashboard",
                  scrolls=[(500, 1500), (1000, 1500)])
        tour_page(page, "/admin-panel/blog/", "AI blog editor",
                  scrolls=[(500, 1500)])
        tour_page(page, "/admin-panel/support/", "Support inbox",
                  scrolls=[(400, 1300)])
        tour_page(page, "/admin-panel/cities-manager/", "Cities manager",
                  scrolls=[(500, 1500), (1000, 1500)])

        # ─── 4. CLOSING — back to homepage ───────────────────────
        print("Closing on homepage…")
        page.goto(BASE + "/logout/", wait_until="domcontentloaded")
        hold(page, 1.0)
        goto(page, "/", settle=1.5)
        smooth_scroll(page, 600, 1800)
        hold(page, 1.5)
        smooth_scroll(page, 0, 1200)
        hold(page, 2.0)

        ctx.close()
        browser.close()

    # ─── Convert webm → mp4 ──────────────────────────────────────
    webms = sorted(RAW_DIR.glob("*.webm"))
    if not webms:
        raise SystemExit("No webm produced!")
    src = webms[0]
    print(f"Encoded webm: {src} ({src.stat().st_size // 1024} KB)")

    out_mp4 = OUT_DIR / "permitlify-walkthrough.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-vf", "fade=t=in:st=0:d=0.6,fps=30",
        "-movflags", "+faststart",
        "-an",
        str(out_mp4),
    ]
    print("Encoding mp4…")
    subprocess.run(cmd, check=True, capture_output=True)
    size_mb = out_mp4.stat().st_size / 1024 / 1024
    # Also report duration
    dur = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(out_mp4)
    ]).decode().strip()
    print(f"\n✓ {out_mp4}  →  {size_mb:.1f} MB, {float(dur):.1f}s")


if __name__ == "__main__":
    main()
