# Darbie: Third Remote Lookup Worker

Autonomous remote client worker for Merriam-Webster dictionary lookups.

Designed to run on a standalone laptop, communicating with PythonAnywhere over HTTPS.

## Architecture

- **Worker Identity**: `DARBIE`
- **Protocol**: HTTPS over PythonAnywhere mailbox (`https://badangel.pythonanywhere.com/barbie`)
- **Zero Inbound Ports**: Does not require any open ports or SSH tunnels on the host machine.
- **Dual-Lane Prioritization**:
  - `live`: Highest priority real-time dispatches from Harvey.
  - `reserve`: Local offline fallback pool executed when disconnected from Harvey.
- **Safety**: Hardwired daily ceiling (1,000 calls max) and randomized human pacing (4–12s).

## Deployment Instructions

1. Unzip `Darbie-worker.zip` to a folder on the target laptop.
2. Double-click `Setup.bat` to input credentials (or copy `worker.example.json` to `worker.json`).
3. Double-click `Start.bat` to launch the worker.
4. To stop, close the terminal window or run `Stop.bat`.
