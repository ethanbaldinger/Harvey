# Harvey

Manager and remote worker distribution ecosystem for MW dictionary mirror operations.

## Architecture & Layout

Harvey coordinates distributed query workers and manages prioritized candidate word lookups across remote environments.

```
Harvey/
  harbie/               # 24/7 Remote worker for PythonAnywhere (Always-On)
    common.py           # Timing parameters, schemas, models
    worker.py           # Worker loop, micro-burst cadence engine
    queue_store.py      # MySQL / SQLite queue abstraction & logging
    run_harbie_pa.py    # PythonAnywhere Always-On entrypoint
    control.py          # CLI for status, telemetry, log inspection, force-offline
    push_to_pa.py       # Pushes candidate & hoover batches to PA
    sync_from_pa.py     # Pulls completed mirror records back to local quarry
    test_harbie_system.py # End-to-end integration and pacing tests
    config.example.json # Template for credentials and db settings
  barbie/               # (Planned) Remote worker package for Dad's machine
```

## Harbie: PythonAnywhere Worker

Harbie runs 24/7 on PythonAnywhere as an Always-On task.

### Operational Features
- **Adaptive Cadence**:
  - Micro-burst mode: 3 to 6 lookups spaced evenly over ~9 seconds.
  - Inter-burst rest: Gaussian distribution (mean = 80s, std dev = 10s, min safety floor = 30s).
  - Daytime heartbeat throttle: When Ethan's laptop is active, pauses to yield API headroom.
- **Audit Logging**:
  - Structured event telemetry written to `badangel$mw.harbie_log`.
- **Local Control CLI**:
  - `python -m harbie.control --status`: Live view of queue backlog and active pacing mode.
  - `python -m harbie.control --force-offline`: Manually trigger burst mode while laptop is running.
  - `python -m harbie.control --auto`: Return to automatic heartbeat sensing.
  - `python -m harbie.control --watch`: Live streaming terminal feed of worker events.
  - `python -m harbie.control --night-summary`: Overnight executive breakdown.

## PythonAnywhere Deployment

1. Target directory on PythonAnywhere:
   `/home/badangel/mw-mirror/harvey`

2. Configuration file location:
   `/home/badangel/mw-mirror/harvey/harbie/config.json`

3. Always-On Task Command:
   Working directory: `/home/badangel/mw-mirror/harvey`
   Command: `python3 -m harbie.run_harbie_pa`
