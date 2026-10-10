// One virtual user = one person with the portal open, at the pace the portal itself sets:
// a heartbeat every 30 s, /api/away/me every 15 s, and one page a minute (the eligibility
// list for posting accounts, /api/auth/me for desk accounts). run.sh runs one stage per VU count.
import http from 'k6/http';
import { check, sleep } from 'k6';

const tokens = JSON.parse(open('/out/tokens.json'));
const BASE = __ENV.BASE || 'http://nginx';
const VUS = parseInt(__ENV.VUS || '50', 10);
const DURATION = __ENV.DURATION || '3m';

export const options = {
  scenarios: {
    portal: { executor: 'constant-vus', vus: VUS, duration: DURATION, gracefulStop: '20s' },
  },
  thresholds: {
    http_req_failed: [{ threshold: 'rate<0.05', abortOnFail: true, delayAbortEval: '30s' }],
    http_req_duration: ['p(95)<2000'],
  },
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  const account = tokens[(__VU - 1) % tokens.length];
  const params = {
    headers: { Cookie: `rcm_session=${account.token}`, 'Content-Type': 'application/json' },
  };
  sleep(Math.random() * 15);
  for (let tick = 0; tick < 4; tick += 1) {
    if (tick % 2 === 0) {
      const beat = JSON.stringify({ state: 'active', visible: true, tab_id: `k6-${__VU}`, page_path: '/my-day' });
      check(http.post(`${BASE}/api/analytics/heartbeat`, beat, { ...params, tags: { name: 'heartbeat' } }),
        { 'heartbeat 200': (r) => r.status === 200 });
    }
    check(http.get(`${BASE}/api/away/me`, { ...params, tags: { name: 'away_me' } }),
      { 'away_me 200': (r) => r.status === 200 });
    if (tick === 0) {
      const page = account.kind === 'posting'
        ? http.get(`${BASE}/api/eligibility/items?page=1&page_size=50`, { ...params, tags: { name: 'eligibility_list' } })
        : http.get(`${BASE}/api/auth/me`, { ...params, tags: { name: 'auth_me' } });
      check(page, { 'page 200': (r) => r.status === 200 });
    }
    sleep(15);
  }
}
