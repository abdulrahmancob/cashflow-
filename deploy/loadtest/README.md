# Load test

Measures how many people the live portal API serves at once, the way the portal itself calls it.

- **One virtual user = one person:** a heartbeat every 30 s, `/api/away/me` every 15 s, and one page a minute. Desk accounts open `/api/auth/me`; posting accounts open the first page of the eligibility list.
- **Stages** run for 3 minutes each: 50, 100, 200 and 400 users. A stage passes when fewer than 5% of requests fail and p95 is under 2 s.
- **Monitoring:** `monitor.log` records Postgres connections and API CPU and memory every 10 s. The run stops at the first failed stage, or when Postgres connections reach 85.
- **No limits in the way:** k6 runs on the compose network and talks to `nginx` directly. nginx exempts docker addresses from its request limits, so this measures the app, not the limits.

## Accounts

`cashflow_ops.loadtest_users` handles them:
- It creates `loadtest-NN@internal.invalid` (40 desk, 10 posting) with random passwords nobody knows.
- It switches them on for the run and prints one-hour session tokens for k6.
- `run.sh` switches them off again on exit, Ctrl-C included, which also takes them off the away board.

## Run

Run at a quiet hour, after the nightly: 06:30–08:00 Cairo.

```bash
sudo bash /opt/cashflow/deploy/loadtest/run.sh            # 50 100 200 400
sudo bash /opt/cashflow/deploy/loadtest/run.sh 50 100     # fewer stages
```

Results go to `/data/logs/loadtest/<time>/`: `stage-N.json` (k6 summary), `stage-N.log` and `monitor.log`. The tokens file is deleted at the end.
