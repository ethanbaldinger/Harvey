# First-round PA deployment

The release is mirror replay only. No MW key or live-provider activation is needed.

1. Back up the existing PA WSGI file and deploy the tested `mw/api.py` cache-only
   fix. Quota 0 must refuse an unheld plain GET; the old implementation did not.
2. Copy the `barbie` Python package to `/home/badangel/barbie-release/barbie`.
   The PA virtualenv already used by the mirror needs `mysql-connector-python`.
3. From `/home/badangel/barbie-release`, run the mirror virtualenv's Python:
   `python -m barbie.provision_pa`. It writes secrets only to `/home/badangel/.barbie`,
   creates three prefixed MySQL tables in the mirror database, and adds one
   quota-0, non-donating mirror client. It preserves existing credentials on rerun.
4. Install the updated `mw-mirror/deploy/pa_wsgi.py` as the app's WSGI file and
   reload the existing app. `/mw` and `/mw-browser` retain their mounts;
   `/barbie` is enabled only when its private configuration exists.
5. Verify unauthenticated mailbox requests return 401; authenticated status
   survives a web reload; assign/retrieve/ack a verified stored lookup; verify
   `/mw/v1/status` reports zero outbound calls for Barbie's new client.
6. Transfer only `worker-credentials.json` to Dad as `barbie/credentials.json`,
   plus a `worker.json` naming that file and the PA endpoints. Keep the manager
   credential on Ethan's laptop. Do not put either credential in the ZIP.
7. Start the local coordinator with a generated replay manifest. The manager
   token is supplied in `BARBIE_MANAGER_TOKEN`; it is not printed by the tools.

Backups: include `barbie_jobs`, `barbie_control`, and `barbie_heartbeat` in the
existing MySQL backup, and retain Ethan's coordinator journal with this mailbox.
Test a restore into a separate schema before relying on recovery. Do not reset
one side's journal while retaining the other side's assignment history.

Rollback: pause the worker while connected, stop the coordinator, restore the
previous WSGI file, and reload. Keep the mailbox tables and local databases.
An offline worker cannot receive a pause; Stop.bat on its PC is authoritative.

This is a deployment procedure, not evidence that PA deployment has occurred.
