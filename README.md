# 🏇 Triple Crown Pick'em

A friends-only horse race picking app. Pick 3 horses per race, score points based on
their finishing positions weighted by morning-line odds. **No money — just bragging rights.**

## Scoring

Final score = **base points × odds multiplier**, summed across your 3 picks.

**Base points by finish:**

| Finish | Points |
|-------:|-------:|
| 🥇 1st | 20 |
| 🥈 2nd | 12 |
| 🥉 3rd | 8 |
| 4th | 4 |
| 5th | 2 |
| Anywhere else | 0 |

**Odds multiplier (based on morning-line odds):**

| Tier | ML range | Multiplier |
|------|----------|-----------:|
| ⭐ Favorite | 5/2 or shorter | ×1.0 |
| Mid-tier | 3-1 to 9-1 | ×1.3 |
| 🎯 Longshot | 10-1 to 19-1 | ×1.7 |
| 💣 Bomb | 20-1 and up | ×2.5 |

**Quick example:** Two players each pick the winner of a race.
- Player A picked the **6/5 favorite** → 20 × 1.0 = **20 pts**
- Player B picked the **30/1 bomb** → 20 × 2.5 = **50 pts**

Picking favorites is safe but doesn't pay big. Calling longshots that hit is the move.

---

## Run locally (development)

```bash
pip install -r requirements.txt
streamlit run app.py
```

The app uses a local SQLite file (`horse_picks.db`) by default — no setup required
beyond the pip install. Open `http://localhost:8501` in a browser.

To use a real Postgres locally, copy `.streamlit/secrets.toml.example` to
`.streamlit/secrets.toml` and fill in `database_url`. The app auto-detects which
backend to use.

---

## Deploy to Streamlit Community Cloud (free, persistent)

### 1. Create a Postgres database on Neon (free)

1. Go to https://neon.tech and sign up (GitHub login works fine, no credit card).
2. Click "New Project", name it whatever, accept defaults.
3. On the project Dashboard, copy the connection string. It looks like:
   ```
   postgresql://user:password@ep-xxxxx.region.aws.neon.tech/dbname?sslmode=require
   ```
4. Paste it somewhere safe — you'll need it in step 4.

### 2. Push code to GitHub (public repo is fine)

The code itself contains no secrets. The admin password and database URL live in
Streamlit's secrets store, never on GitHub.

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/triple-crown-pickem.git
git push -u origin main
```

The `.gitignore` is set up so your local `secrets.toml` and `horse_picks.db` are
excluded automatically.

### 3. Deploy on Streamlit Community Cloud

1. Go to https://share.streamlit.io and sign in with GitHub.
2. Click "New app" → point at the repo, set main file = `app.py`.
3. Click "Deploy".

### 4. Add secrets in Streamlit's web UI

1. Once deployed, click the "⋮" menu next to your app → "Settings" → "Secrets".
2. Paste this in (with YOUR real values):
   ```toml
   admin_password = "your-strong-admin-password"
   database_url = "postgresql://user:password@host/dbname?sslmode=require"
   ```
3. Save. The app restarts automatically and starts using Postgres for persistence.

### 5. Share the public URL

Streamlit gives you a URL like `https://your-app-name.streamlit.app`. Drop that
into your group chat. Everyone can make picks; only you can access Admin.

---

## Admin tasks

The **⚙️ Admin** page (password-gated) lets you:

- Create new races (Preakness, Belmont, Breeders' Cup, any race you want)
- Add horses one at a time, OR
- **📋 Smart Paste** — copy entries from Equibase / DRF / track website / news article,
  paste into the textbox, parser pulls out post numbers, horse names, and morning-line
  odds automatically. You preview & edit before saving.
- Close picks before post time
- Enter finishing positions after the race
- Mark race as "settled" to lock results into the cumulative leaderboard

### Smart Paste — supported formats

```
1  Renegade  I. Ortiz Jr.  T. Pletcher  4-1     ← Equibase tabular
Post 1: Renegade (4-1) - Trained by Pletcher    ← News article
1. Renegade — 4-1                                ← Numbered list
Renegade 4-1                                     ← Just name + ML
Renegade                                         ← Just name
```

It also understands fractional odds (`5/2`, `9/2`, `7/2`), `EVEN`/`EVS`, skips header
rows, and de-duplicates repeated names. Always shows a preview before saving.

---

## File layout

```
app.py                                # The whole app
requirements.txt                      # streamlit + psycopg (Postgres driver)
.gitignore                            # Excludes secrets.toml + horse_picks.db
.streamlit/secrets.toml.example       # Template for secrets (commit this)
.streamlit/secrets.toml               # YOUR real secrets (gitignored)
horse_picks.db                        # Local SQLite (auto-created, gitignored)
```

---

## Backups

Neon's free tier includes point-in-time recovery for the last 24 hours, so you have
some automatic safety net. For longer-term backups, run `pg_dump` against your
connection string periodically.
