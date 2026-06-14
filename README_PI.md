# Lumen — Raspberry Pi 5 Setup Guide

## What you need

| Item | Details |
|---|---|
| **Raspberry Pi 5** | 4GB or 8GB RAM recommended |
| **USB Webcam** | Any standard USB webcam (4K preferred) |
| **MicroSD card** | 32GB+ with Raspberry Pi OS (64-bit Bookworm) |
| **Internet connection** | WiFi or Ethernet |
| **Anthropic API key** | From console.anthropic.com |
| **Student device** | Any phone, tablet, or laptop on the same WiFi |

---

## Step 1 — Set up the Pi

Install **Raspberry Pi OS (64-bit, Bookworm)** using Raspberry Pi Imager.
- Enable SSH and WiFi during imaging if you want headless setup
- Or connect a monitor, keyboard and mouse for the first run

---

## Step 2 — Copy files to the Pi

**Option A — USB drive:**
Copy the entire `Lumen` folder to the Pi's home directory (`/home/pi/`).

**Option B — SSH from Mac:**
```bash
scp -r /Users/snehal/Claude/Lumen pi@raspberrypi.local:~/
```

---

## Step 3 — Run setup (once only)

Open a terminal on the Pi and run:

```bash
cd ~/Lumen
bash setup_pi.sh
```

This will:
- Install system libraries (OpenCV dependencies, etc.)
- Create a Python virtual environment
- Install all Python packages
- Generate an SSL certificate (needed for camera from other devices)

---

## Step 4 — Add your API key

```bash
nano ~/Lumen/.env
```

Change `paste-your-key-here` to your actual Anthropic API key (starts with `sk-ant-`).
Save: `Ctrl+X` → `Y` → `Enter`

---

## Step 5 — Start the app

```bash
cd ~/Lumen
bash run_pi.sh
```

The terminal will show something like:

```
====================================================
  Lumen  —  Raspberry Pi 5
====================================================
  Claude AI   : ✓ enabled
  Data stored : /home/pi/Lumen
====================================================
  📱 Open on THIS Pi  : https://localhost:5050
  📱 Open on ANY device on the same WiFi:
      https://192.168.1.45:5050
====================================================
```

---

## Step 6 — Connect from student devices

On any phone, tablet, or laptop **on the same WiFi**:

1. Open Chrome or Safari
2. Go to `https://192.168.1.XX:5050` (use the IP shown on step 5)
3. Browser will show a security warning — this is normal for a self-signed certificate
4. Click **Advanced** → **Proceed to 192.168.1.XX (unsafe)**
5. When the app asks for camera permission → click **Allow**
6. App is ready!

> **Tip:** Bookmark the URL on students' devices so they don't have to type it each time.

---

## Optional — Auto-start on boot

So the app starts automatically whenever the Pi is powered on:

```bash
bash ~/Lumen/setup_autostart.sh
```

After this, just plug in the Pi and it's ready within 30 seconds.

---

## Camera tips for Pi

- Plug the webcam into a **USB 3.0 port** (blue port) on the Pi 5 for best speed
- If using Pi Camera Module 3, it also works — the browser sees it via `getUserMedia`
- For 4K webcams, the browser will automatically request the highest available resolution

---

## Where data is stored

| Type | Location on Pi |
|---|---|
| Captured photos | `~/Lumen/captures/` |
| Session data | `~/Lumen/sessions/` |
| Gallery images | `~/Lumen/sessions/images/` |

---

## Troubleshooting

**Camera not showing?**
- Make sure you accepted the HTTPS certificate warning first
- Camera access requires HTTPS — the self-signed cert handles this
- Try refreshing the page after accepting the warning

**App not starting?**
- Check the API key in `.env` starts with `sk-ant-`
- Check internet connection (Claude API needs internet)
- Run `bash run_pi.sh` and look for error messages

**Can't connect from phone?**
- Make sure phone and Pi are on the same WiFi network
- Double-check the IP address shown in the terminal
- Try pinging: `ping 192.168.1.XX` from your phone's browser URL bar

**App slow?**
- Claude API calls (AI Board, AI Analysis) take 5–15 seconds — this is normal
- The camera feed itself is real-time and should be smooth

---

## Keeping the Mac version working

The original Mac files are completely unchanged:
- `app.py` — Mac server (untouched)
- `run.sh` — Mac launcher (untouched)

The Pi version uses:
- `app_pi.py` — Pi server
- `run_pi.sh` — Pi launcher
