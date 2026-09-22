# BARBIE 0.1.0 — replay test version

BARBIE is HARVEY's remote lookup worker. This first version implements the
worker, an authenticated mailbox, and a test manager. It is **not connected to
the running HARVEY and is not yet a live Merriam-Webster client**. No existing
HARVEY, mirror, database, deployment, or quota configuration is changed.

## Run the self-contained demo

Python 3.11+; standard library only. No package installation is needed.
From `C:\BaseFinder` in **PowerShell**, run:

`python -B -m barbie.demo`

Expected: a local fixture response travels through assignments, worker storage,
mailbox delivery, and durable manager collection. Repeated collection returns
zero. Temporary demo databases are removed when the demo ends.

For replay against the existing PythonAnywhere mirror, from the same directory:

`python -B -m barbie.demo --mirror-env crypt_web_first_light_v4/mirror_client.env --word brominate`

This uses the existing credential locally, never prints it, and forces `held=1`.
It adds replay accounting on the mirror but cannot dispatch to Merriam-Webster.
Do not ship HARVEY's credential file to Dad. A separate replay client credential
will be provisioned for his installation. The real BARBIE MW key is still pending.

Tests, from the same PowerShell directory:

`python -B -m unittest barbie.test_system -v`

## Agreed product behavior

- Dad uses Windows, in Ethan's time zone, and already runs Python applications.
- Availability is approximately 11 a.m.–11 p.m., often within an hour of that;
  it is not a scheduling guarantee. Dad starts the worker and can follow a
  graceful stop procedure before shutting down; crashes remain possible.
- HARVEY stays on Ethan's laptop for now. Moving it to PA is deferred.
- HARVEY owns priority selection, the local quarry, fan-out and interpretation.
  BARBIE executes assignments using her own exclusive Collegiate API key.
- PythonAnywhere is the shared mailbox. Neither laptop needs inbound access.
- Results travel BARBIE → mailbox → HARVEY → mirror. Mailbox receipt is distinct
  from HARVEY processing and from mirror donation. The extra transfer is accepted.
- Live assignments are preferred. An offline reserve is downloaded on the first
  successful contact each local day. Yesterday's unfinished reserve remains
  usable if startup happens without contact. No new reserve is necessary per call.
- HARVEY must exclude BARBIE-reserved words from its own selection once integrated.
- Each response is saved locally before delivery. Delivery happens promptly;
  accumulated results return in small batches when connectivity resumes.
- Default random delay is four to twelve seconds. Settings arrive via handshake.
- A local daily quota ledger survives restarts. The production ceiling is at
  most 1,000 entry-bearing responses, not merely exact-query positives. This
  prototype simulates that accounting separately using replay results.
- Keep the worker simple: local jobs, pending responses, settings and quota;
  central history, not an elaborate permanent local audit trail.

## Implemented protocol and storage

`mailbox.py` is a WSGI application with separate manager and worker bearer tokens.
The provided server listens only on loopback. HTTPS is required for non-loopback
URLs, and redirects are rejected to protect credentials. Assignment IDs stay
stable across repeat polls. Repeated identical results are accepted; conflicting
results are rejected. Manager acknowledgement happens after its local commit.

The worker's SQLite database stores jobs, pending payloads, settings, pacing,
and a replay-only daily ledger. After mailbox acknowledgement, the local payload
is cleared; a small completed-job marker prevents replaying the assignment.
The mailbox keeps the full response/history, including after manager collection.

The worker reserves one quota unit before dispatch and settles it when the
response is saved. An interrupted or ambiguous request retains that unit for
the day and returns an `uncertain` outcome instead of automatically retrying.
A missing mirror snapshot is `not_held`, distinct from a stored dictionary miss.
Quota uses the machine's local day for the test, matching current HARVEY code;
the actual MW reset convention must be confirmed before enabling real calls.

Daily reserve refresh is additive in this first version: unfinished assignments
retain ownership and completed ones never reappear. Replacement/revocation is
not implemented; it needs explicit confirmation from the worker before HARVEY
can safely reclaim words that might already be in flight offline.

Remote controls currently cover pacing, daily limit, and pause. An offline
worker uses its last received settings; a new pause cannot reach it until it
reconnects. Endpoints, credentials, and the replay-only execution mode remain
local installation settings. New code cannot be delivered through these controls.

## Interactive local test setup

Create two distinct random bearer secrets, each at least 24 characters, and set
`BARBIE_WORKER_TOKEN` and `BARBIE_MANAGER_TOKEN` in the relevant terminal
environments. Set `BARBIE_MIRROR_TOKEN` to the mirror client's `key.token` value.
Keep these out of Git and transcripts. Do not reuse production credentials for
the mailbox. The demo handles temporary mailbox secrets automatically.

Copy `barbie/worker.example.json` to `barbie/worker.json`. The sample mailbox
address is loopback; it only works when worker and mailbox run on the same PC.

From `C:\BaseFinder` in **PowerShell**, each command below is a complete single
line. Run the mailbox and worker in separate terminals:

- `python -B -m barbie.mailbox --db barbie/mailbox.sqlite3`
- `python -B -m barbie.manager assign --lane reserve brominate`
- `python -B -m barbie.worker --config barbie/worker.json`
- `python -B -m barbie.manager status`
- `python -B -m barbie.manager collect --db barbie/manager.sqlite3`
- `python -B -m barbie.manager settings barbie/settings.example.json`
- `python -B -m barbie.worker --config barbie/worker.json --stop`

`Start.bat` and `Stop.bat` wrap the last two worker operations for Dad's eventual
package. They require `python` on PATH and the configured credentials available
in their environment. Wait for “stopped safely” before shutdown. A current
request and final sync have 15-second network timeouts each; stalled responses
can make shutdown take longer. A crash still preserves already saved work.

## Before Dad's installation / live HARVEY integration

1. Deploy a durable mailbox under the existing PA WSGI application with isolated
   manager/worker credentials. The current SQLite mailbox is for local testing;
   select and verify PA storage (prefer its existing MySQL service), locking,
   backup and restart behavior before deployment. Existing `/mw` stays intact.
2. Add HARVEY assignment reservation and dispatch without tying BARBIE's quota
   to Ethan's key. Connect returned responses to HARVEY's existing persistence
   logic with an external-result receipt recorded in the same MySQL transaction.
3. Track donation completion separately from local processing. Current HARVEY
   donation is fire-and-forget and has no retry queue; this does not yet satisfy
   durable donation of worker results. Do not re-donate test replay responses.
4. Add the real MW transport when Dad's key arrives, confirm quota reset/billing
   rules, and preserve key separation. Replay accounting must never seed live quota.
5. Provision Dad's own credentials and endpoint settings; test the packaged
   launchers on his Windows Python setup and test shutdown/restart there.

The test manager currently accepts explicit words, not automatically mined
quarry candidates. The live integration remains a deliberate next stage.
