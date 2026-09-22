# Deployment Guide: Lifting Harbie to PythonAnywhere

This guide provides the exact steps to lift Harbie to PythonAnywhere (`badangel.pythonanywhere.com`) when you are ready in the morning.

---

## Step 1: Upload the `harbie` Package to PythonAnywhere

On your laptop, from `C:\BaseFinder`:
```bash
# Push harbie package to PA via git or rsync / scp
scp -r harbie badangel@ssh.pythonanywhere.com:/home/badangel/BaseFinder/harbie
```
*(Or commit and pull via your repo)*

---

## Step 2: Configure Environment on PythonAnywhere

In your PythonAnywhere bash console:
1. Navigate to your project directory:
   ```bash
   cd /home/badangel/BaseFinder
   ```
2. Create or configure `harbie/config.json` (or set environment variables):
   ```json
   {
     "mw_key": "YOUR_ETHAN_MW_COLLEGIATE_API_KEY",
     "mysql": {
       "host": "badangel.mysql.pythonanywhere-services.com",
       "user": "badangel",
       "password": "SandyMagdalena",
       "database": "badangel$mw"
     },
     "pacing_sec": 80,
     "jitter_pct": 15
   }
   ```

---

## Step 3: Initialize Database Tables on PA

Run a quick one-liner on PythonAnywhere to create `harbie_queue`, `harbie_control`, and `harbie_heartbeat` (this does not touch any existing Barbie tables):
```bash
python -c "import mysql.connector; from harbie.queue_store import QueueStore; QueueStore(lambda: mysql.connector.connect(host='badangel.mysql.pythonanywhere-services.com', user='badangel', password='SandyMagdalena', database='badangel$mw'), is_mysql=True)"
```

---

## Step 4: Add Always-On Task on PythonAnywhere

1. Open your browser to PythonAnywhere: https://www.pythonanywhere.com/user/badangel/tasks_tab/
2. Under **Always-on tasks**, create a new task:
   * **Command**:
     ```bash
     /home/badangel/.virtualenvs/basefinder/bin/python -m harbie.run_harbie_pa
     ```
   * **Working directory**:
     ```bash
     /home/badangel/BaseFinder
     ```
3. Click **Enable**. PythonAnywhere will launch Harbie immediately and keep it running 24/7.

---

## Step 5: Verification

1. On PythonAnywhere, view the task log. You will see:
   ```
   [INFO] [Harbie] Connecting to PythonAnywhere database 'badangel$mw'...
   [INFO] [Harbie] Harbie PA Worker initialized. Starting 24/7 autonomous loop...
   ```
2. On your laptop, push your first batch:
   ```powershell
   python -m harbie.push_to_pa --candidates 20000 --hoover 5000
   ```
3. Harbie will immediately detect the queued words, report mode `CONNECTED`, and begin rolling on mellow 80s pacing. When you shut your laptop, it will switch to `OFFLINE` and keep rolling through the night.
