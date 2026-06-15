# JARVIS Frontend — Deployment Instructions

## What this is
Two files:
- `app.py` — Python backend API (runs on Render)
- `index.html` — Jarvis web interface (hosted on GitHub Pages or Render static)

## Step 1 — Create a GitHub repo
1. Go to github.com and create a new repo called `jarvis-frontend`
2. Upload all files from this folder to it

## Step 2 — Deploy backend to Render
1. Go to render.com and sign up (free)
2. New → Web Service → Connect your GitHub repo
3. Settings:
   - Name: jarvis-ci
   - Runtime: Python 3
   - Build command: pip install -r requirements.txt
   - Start command: gunicorn app:app
4. Add environment variable:
   - Key: ANTHROPIC_API_KEY
   - Value: your Anthropic API key
5. Click Deploy
6. Render gives you a URL like: https://jarvis-ci.onrender.com
   SAVE THIS URL

## Step 3 — Update the frontend with your Render URL
In index.html find this line:
  const API_BASE = ... 'https://jarvis-ci.onrender.com'
Replace jarvis-ci.onrender.com with your actual Render URL

## Step 4 — Host the frontend
Option A (easiest) — GitHub Pages:
1. In your GitHub repo, go to Settings → Pages
2. Source: Deploy from branch → main → / (root)
3. Your frontend will be at: https://yourusername.github.io/jarvis-frontend

Option B — Render Static:
Add a second service in Render as a Static Site pointing to index.html

## Step 5 — Bookmark it
Add the GitHub Pages URL to your phone home screen and desktop bookmarks.
That's your Jarvis front end — available on every device, always.

## Voice input
Click the arc reactor (circle) or the mic button to speak.
Say "Hey Jarvis" then your question — it sends automatically when you stop speaking.
Works on Chrome on any device. Safari has limited support.

## Notes
- Free Render tier spins down after 15 minutes of inactivity — first message after a gap takes ~30 seconds to wake up. Upgrade to paid ($7/month) for instant response.
- The backend keeps conversation history in memory — resets if Render restarts. Persistent history can be added later via Notion.
