# Harbie: 24/7 Merriam-Webster Worker on PythonAnywhere

Harbie is the autonomous, 24/7 remote worker for Ethan's Merriam-Webster Collegiate API key, hosted on PythonAnywhere (`badangel.pythonanywhere.com`).

---

## 1. Architectural Model

```
+-------------------------------------------------------------------------+
| ETHAN'S LAPTOP (Harvey Planner)                                          |
| - Local database: all_your_base (86 candidate quarry tables, 4.58M rows)|
| - Evaluates candidate graph & fanout scores                             |
| - Extracts priority candidate words (~20,000) & hoover words (~5,000)   |
| - Command: python -m harbie.push_to_pa                                  |
+------------------------------------+------------------------------------+
                                     | (Pushes batch via SSH tunnel)
                                     v
+-------------------------------------------------------------------------+
| PYTHONANYWHERE (badangel$mw)                                            |
| - Table: harbie_queue (Priority candidate words + Hoover buffer pool)   |
| - Table: harbie_control (Heartbeat, Connected vs Offline mode, Pacing)  |
| - Table: harbie_heartbeat (Live worker status report)                   |
| - Table: snapshot (Raw MW JSON responses, worker='HARBIE')              |
| - Table: mw_lookup_reservation (Atomic mutual locks vs. Barbie)         |
+------------------------------------+------------------------------------+
                                     ^
                                     | (Consumes queue 24/7)
+------------------------------------+------------------------------------+
| PYTHONANYWHERE ALWAYS-ON TASK (Harbie Worker)                           |
| - Command: python -m harbie.run_harbie_pa                               |
| - Connected Mode: Harvey is online (heartbeat < 5m). Pacing coordinated.|
| - Offline Mode: Laptop shut/dark. Autonomous mellow execution (~80s).   |
| - Hoover Tripwire: Switches from CANDIDATE to HOOVER lane at 950 hits   |
|   to protect the 1,000-hit daily quota ceiling.                         |
+-------------------------------------------------------------------------+
```

---

## 2. Daily Workflow

### Evening (Before Closing Laptop):
Run the batch pusher on your laptop. It takes ~5 seconds to query local `all_your_base` and push the words to PA:
```powershell
python -m harbie.push_to_pa --candidates 20000 --hoover 5000
```
*You can now safely shut your laptop, put it to sleep, or travel. Harbie will run continuously on PythonAnywhere throughout the night.*

### Morning (When Opening Laptop):
Run the sync reconciliation to pull newly attested words into your local database:
```powershell
python -m harbie.sync_from_pa
```
*All newly certified headwords and decisive misses are immediately recorded in your local `word` table and candidate graphs.*

---

## 3. Harbie Worker Safeguards & Pacing

1. **Micro-Burst Cadence (Offline Mode)**:
   * **Burst Size**: Fires **3 to 6 lookups** in a rapid human-like micro-burst.
   * **Active Burst Duration**: The entire burst of lookups spans **~9 seconds** (spaced at ~1.8s to 4.5s intervals).
   * **Randomized Rest Pause**: After each burst, Harbie pauses for a randomized rest sampled from a **Gaussian (normal) distribution** with **mean = 80 seconds** and **standard deviation = 10 seconds** (safety floor = 30s).
   * Approximately 68% of rests fall between 70s and 90s, and 95% between 60s and 100s.

2. **Where to Fiddle with Parameters**:
   All parameters are clearly exposed and commented at the top of [`c:\BaseFinder\harbie\common.py`](file:///c:/BaseFinder/harbie/common.py):
   ```python
   # PACING & MICRO-BURST PARAMETERS (TUNE HERE)
   BURST_MIN_LOOKUPS = 3       # Minimum lookups per burst (default: 3)
   BURST_MAX_LOOKUPS = 6       # Maximum lookups per burst (default: 6)
   BURST_DURATION_SEC = 9.0    # Active burst elapsed duration in seconds (default: 9.0)

   REST_MEAN_SEC = 80.0        # Gaussian mean rest pause in seconds (default: 80.0)
   REST_STD_DEV_SEC = 10.0     # Gaussian standard deviation in seconds (default: 10.0)
   REST_MIN_SEC = 30.0         # Hard safety floor for rest pause (default: 30.0)
   ```
   They can also be tuned on PythonAnywhere via environment variables (`BURST_MIN`, `BURST_MAX`, `BURST_DURATION_SEC`, `REST_MEAN_SEC`, `REST_STD_DEV_SEC`) or in `config.json`.

3. **The 950-Hit Hoover Tripwire**:
   * As long as daily hits are under 950, Harbie burns puzzle candidate words (`CANDIDATE` lane).
   * The moment Harbie reaches **950 hits** (50 hits remaining under the 1,000 quota limit), it automatically trips into **Hoover Mode** (`HOOVER` lane).
   * In Hoover Mode, it looks up long fringe words (12–24 letters) with near-zero positive probability to safely soak remaining API calls without blowing past the 1,000-hit daily limit.

4. **Midnight Reset**:
   * Harbie automatically detects when date changes (crossing midnight UTC) and resets daily call and hit counters to zero, returning to the `CANDIDATE` lane.

5. **Zero Collisions with Barbie**:
   * Before looking up any word, Harbie verifies the atomic reservation in `mw_lookup_reservation`.
   * If Dad's worker (Barbie) has locked that word, Harbie defers it immediately and moves to the next word.

---

## 4. Local Test Suite

The entire workflow is verified locally with a mock database and simulated API:
```powershell
python -m unittest harbie.test_harbie_system
```
All 15 automated unit and integration tests pass cleanly in under 2 seconds.
