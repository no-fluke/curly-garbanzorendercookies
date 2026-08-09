# 🍪 YouTube Cookie Service

Extracts YouTube cookies via headless Chromium and serves them to your Heroku API.
Deployed on **Render.com using Docker** (required for Playwright/Chromium).

---

## 🚀 Deploy on Render

### Step 1 — Push to GitHub
1. Create a new GitHub repo e.g. `yt-cookie-service`
2. Upload all these files:
   ```
   main.py
   requirements.txt
   Dockerfile
   .gitignore
   README.md
   ```
3. Commit and push

### Step 2 — Create Render Web Service
1. Go to [render.com](https://render.com) → sign up free
2. Click **New → Web Service**
3. Connect your GitHub repo
4. Configure:
   - **Name**: `yt-cookie-service`
   - **Region**: Any
   - **Branch**: `main`
   - **Runtime**: **Docker** ← important!
   - **Instance Type**: Free

### Step 3 — Add Environment Variables
Go to **Environment** tab and add:

| Key | Value |
|-----|-------|
| `GOOGLE_EMAIL` | `your@gmail.com` |
| `GOOGLE_PASS` | your Google App Password (no spaces) |
| `RENDER_SECRET` | any random string e.g. `xk9m2z7q3p8n1w` |
| `COOKIE_MAX_AGE` | `72` (hours before re-extracting) |

### Step 4 — Deploy
Click **Create Web Service** → wait for build (~3-5 mins)

### Step 5 — Copy your URL
Once deployed, copy your service URL:
```
https://yt-cookie-service.onrender.com
```
You will need this URL and your `RENDER_SECRET` for the Heroku API setup.

---

## ⚙️ How to get Google App Password
1. Go to **myaccount.google.com/security**
2. Enable **2-Step Verification**
3. Go to **myaccount.google.com/apppasswords**
4. Create app password → name it `ytapi`
5. Copy the 16-character password (remove spaces)

---

## 📡 Endpoints

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| GET | `/` | None | Health check — shows cookie status |
| GET | `/cookies` | X-Render-Secret | Returns cookies (called by Heroku) |
| POST | `/force-refresh` | X-Render-Secret | Force re-extract cookies |

---

## ✅ Verify deployment
Open in browser:
```
https://yt-cookie-service.onrender.com/
```
Should return:
```json
{
  "service": "YouTube Cookie Service",
  "status": "ready",
  "has_cookies": false
}
```
> `has_cookies` will be `true` after Heroku pings it for the first time.
