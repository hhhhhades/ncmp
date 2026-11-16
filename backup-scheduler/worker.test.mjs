import assert from "node:assert/strict";
import test from "node:test";
import worker, { beijingDate, runBackup } from "./worker.js";

const TOKEN = "test-secret-never-log-me";
const NOW = Date.parse("2026-11-16T18:45:00Z");
const DATE = "2026-11-17";
const MARKER = "ncmp-score-completed-v1-" + DATE;
const ENV = { GITHUB_TOKEN: TOKEN };
const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
const artifact = (overrides = {}) => ({
  name: MARKER,
  expired: false,
  created_at: "2026-11-16T18:00:00Z",
  workflow_run: { id: 123, head_branch: "main" },
  ...overrides
});
const run = (overrides = {}) => ({
  id: 123,
  path: ".github/workflows/auto_score.yml",
  head_branch: "main",
  conclusion: "success",
  ...overrides
});

function harness({ pages = [[]], details = run(), dispatchStatus = 204, responder } = {}) {
  const calls = [];
  const logs = [];
  const options = {
    now: NOW,
    scheduledTime: NOW,
    logger: { log: (message) => logs.push(JSON.parse(message)) },
    fetcher: async (url, init) => {
      calls.push({ url: new URL(url), init });
      if (responder) return responder(url, init, calls.length);
      if (init.method === "POST") {
        return new Response(null, { status: dispatchStatus });
      }
      if (url.includes("/actions/artifacts?")) {
        const page = Number(new URL(url).searchParams.get("page"));
        return json({ artifacts: pages[page - 1] || [] });
      }
      return json(details);
    }
  };
  return { calls, logs, options };
}

test("Beijing dates use UTC+8, including midnight and numeric Cron timestamps", () => {
  assert.equal(beijingDate("2026-11-16T15:59:59Z"), "2026-11-16");
  assert.equal(beijingDate("2026-11-16T16:00:00Z"), DATE);
  assert.equal(beijingDate(NOW), DATE);
  assert.equal(beijingDate("invalid"), null);
});

test("a successful/skipped workflow without a completion artifact still dispatches", async () => {
  const h = harness({ responder: (url, init) => init.method === "POST"
    ? new Response(null, { status: 204 })
    : json({ artifacts: [], workflow_runs: [run({ conclusion: "success" })] }) });
  const result = await runBackup(ENV, h.options);
  assert.equal(result.status, "dispatched");
  assert.equal(h.calls.length, 2);
  assert.match(h.calls[0].url.pathname, /\/actions\/artifacts$/);
  assert.equal(h.calls[0].url.searchParams.get("name"), MARKER);
  assert.equal(h.calls[0].url.searchParams.get("per_page"), "100");
  const dispatch = h.calls[1];
  assert.equal(dispatch.init.headers.Authorization, "Bearer " + TOKEN);
  assert.deepEqual(JSON.parse(dispatch.init.body), {
    ref: "main",
    inputs: {
      trigger_source: "backup",
      score_date: DATE,
      request_id: "ncmp-backup-" + NOW,
      verification_only: false
    }
  });
});

test("only the exact score artifact and verified main workflow suppress dispatch", async () => {
  const h = harness({ pages: [[artifact()]], details: run({ conclusion: "failure", run_started_at: "2000-01-01T00:00:00Z" }) });
  const result = await runBackup(ENV, h.options);
  assert.equal(result.status, "already_scored");
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].url.pathname, "/repos/hhhhhades/ncmp/actions/runs/123");
  assert.equal(h.calls.some(({ init }) => init.method === "POST"), false);
});

test("expired, wrong-date, wrong-name, and wrong-branch artifacts cannot block recovery", async (t) => {
  const cases = {
    expired: artifact({ expired: true }),
    "wrong date": artifact({ created_at: "2026-11-16T15:59:59Z" }),
    "wrong name": artifact({ name: MARKER + "-other" }),
    "wrong artifact branch": artifact({ workflow_run: { id: 123, head_branch: "feature" } }),
    "missing artifact branch": artifact({ workflow_run: { id: 123 } }),
    "invalid run ID": artifact({ workflow_run: { id: "123", head_branch: "main" } })
  };
  for (const [name, marker] of Object.entries(cases)) {
    await t.test(name, async () => {
      const h = harness({ pages: [[marker]] });
      assert.equal((await runBackup(ENV, h.options)).status, "dispatched");
      assert.equal(h.calls.length, 2);
    });
  }
});

test("artifact owner run must have the expected ID, workflow path and main branch", async (t) => {
  for (const [name, detail] of Object.entries({
    "other workflow": run({ path: ".github/workflows/other.yml" }),
    "other branch": run({ head_branch: "feature" }),
    "other ID": run({ id: 124 })
  })) {
    await t.test(name, async () => {
      const h = harness({ pages: [[artifact()]], details: detail });
      assert.equal((await runBackup(ENV, h.options)).status, "dispatched");
      assert.equal(h.calls.at(-1).init.method, "POST");
    });
  }
});

test("completion artifacts are paginated before dispatching", async () => {
  const invalid = artifact({ expired: true });
  const h = harness({ pages: [Array(100).fill(invalid), [artifact()]] });
  assert.equal((await runBackup(ENV, h.options)).status, "already_scored");
  assert.equal(h.calls[1].url.searchParams.get("page"), "2");
  assert.equal(h.calls.length, 3);
});

test("artifact API failures still dispatch and never log response bodies or secrets", async (t) => {
  for (const scenario of ["http", "network", "invalid JSON", "invalid data", "run read"]) {
    await t.test(scenario, async () => {
      const h = harness({ responder: (url, init) => {
        if (init.method === "POST") return new Response(null, { status: 204 });
        if (scenario === "http") return new Response(TOKEN, { status: 503 });
        if (scenario === "network") throw new Error(TOKEN);
        if (scenario === "invalid JSON") return new Response(TOKEN, { status: 200 });
        if (scenario === "invalid data") return json({ secret: TOKEN });
        if (url.includes("/actions/artifacts?")) return json({ artifacts: [artifact()] });
        return new Response(TOKEN, { status: 404 });
      } });
      assert.equal((await runBackup(ENV, h.options)).status, "dispatched");
      assert.equal(h.logs[0].status, "marker_check_failed");
      assert.equal(JSON.stringify(h.logs).includes(TOKEN), false);
      for (const log of h.logs) {
        assert.equal(log.date, DATE);
        assert.equal(log.request_id, "ncmp-backup-" + NOW);
      }
    });
  }
});

test("a GitHub API request times out after 15 seconds and can recover via dispatch", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let readSignal;
  const h = harness({ responder: (url, init) => {
    if (init.method === "POST") return new Response(null, { status: 204 });
    readSignal = init.signal;
    return new Promise((resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new Error(TOKEN)), { once: true });
    });
  } });
  const pending = runBackup(ENV, h.options);
  t.mock.timers.tick(14_999);
  assert.equal(readSignal.aborted, false);
  t.mock.timers.tick(1);
  assert.equal((await pending).status, "dispatched");
  assert.equal(h.logs[0].reason, "api_timeout");
  assert.equal(JSON.stringify(h.logs).includes(TOKEN), false);
});

test("dispatch accepts both GitHub 204 and 200 responses", async (t) => {
  for (const dispatchStatus of [204, 200]) {
    await t.test(String(dispatchStatus), async () => {
      const h = harness({ dispatchStatus });
      assert.equal((await runBackup(ENV, h.options)).status, "dispatched");
    });
  }
});

test("dispatch errors throw only a sanitized error and preserve diagnostic status", async () => {
  const h = harness({ dispatchStatus: 403 });
  await assert.rejects(runBackup(ENV, h.options), { message: "dispatch_failed" });
  assert.equal(h.logs.at(-1).http_status, 403);
  assert.equal(JSON.stringify(h.logs).includes(TOKEN), false);
});

test("a callback delayed across Beijing midnight does not dispatch for the next day", async () => {
  const h = harness();
  const result = await runBackup(ENV, { ...h.options, now: Date.parse("2026-11-17T16:00:00Z") });
  assert.equal(result.date, DATE);
  assert.equal(result.status, "expired_cron");
  assert.equal(h.calls.length, 0);
});

test("missing secret and invalid scheduled time do not make GitHub requests", async () => {
  const h = harness();
  await assert.rejects(runBackup({}, h.options), { message: "missing_github_token" });
  await assert.rejects(runBackup(ENV, { ...h.options, scheduledTime: "bad" }), { message: "invalid_scheduled_time" });
  assert.equal(h.calls.length, 0);
});

test("scheduled callbacks use Cloudflare scheduledTime rather than callback start time", async (t) => {
  t.mock.method(Date, "now", () => Date.parse("2026-11-17T16:00:00Z"));
  const calls = [];
  t.mock.method(globalThis, "fetch", async () => calls.push("unexpected"));
  t.mock.method(console, "log", () => {});
  let pending;
  await worker.scheduled({ scheduledTime: NOW }, ENV, { waitUntil: (promise) => { pending = promise; } });
  assert.equal((await pending).status, "expired_cron");
  assert.equal(calls.length, 0);
});

test("verify rejects missing/wrong Authorization and all query credentials", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async () => calls.push("unexpected"));
  for (const auth of [undefined, "Bearer wrong", "Basic " + TOKEN, "Bearer " + TOKEN + " extra"]) {
    const response = await worker.fetch(new Request("https://worker.example/verify", {
      method: "POST",
      headers: auth ? { Authorization: auth } : {}
    }), ENV);
    assert.equal(response.status, 401);
    assert.equal((await response.text()).includes(TOKEN), false);
  }
  for (const [url, method] of [
    ["https://worker.example/verify?token=" + TOKEN, "POST"],
    ["https://worker.example/verify", "GET"],
    ["https://worker.example/", "POST"]
  ]) {
    const response = await worker.fetch(new Request(url, { method, headers: { Authorization: "Bearer " + TOKEN } }), ENV);
    assert.equal(response.status, 404);
  }
  assert.equal(calls.length, 0);
});

test("authenticated verify dispatches only the harmless verification workflow", async (t) => {
  t.mock.method(Date, "now", () => NOW);
  const calls = [];
  const logs = [];
  t.mock.method(console, "log", (message) => logs.push(message));
  t.mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url, init });
    return new Response(null, { status: 204 });
  });
  const response = await worker.fetch(new Request("https://worker.example/verify", {
    method: "POST", headers: { Authorization: "Bearer " + TOKEN }
  }), ENV);
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.status, "verification_dispatched");
  assert.equal(result.date, DATE);
  assert.match(result.request_id, new RegExp("^ncmp-verify-" + NOW + "-"));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    ref: "main",
    inputs: { trigger_source: "backup", score_date: DATE, request_id: result.request_id, verification_only: true }
  });
  assert.equal(JSON.stringify({ logs, result }).includes(TOKEN), false);
});

test("verify reports dispatch failure without response body or token leakage", async (t) => {
  const logs = [];
  t.mock.method(console, "log", (message) => logs.push(message));
  t.mock.method(globalThis, "fetch", async () => new Response(TOKEN, { status: 403 }));
  const response = await worker.fetch(new Request("https://worker.example/verify", {
    method: "POST", headers: { Authorization: "Bearer " + TOKEN }
  }), ENV);
  assert.equal(response.status, 502);
  const body = await response.text();
  assert.equal(JSON.stringify({ logs, body }).includes(TOKEN), false);
});
