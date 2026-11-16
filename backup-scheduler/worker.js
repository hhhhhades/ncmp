// Cloudflare Cron Triggers use UTC; these checks and the workflow use Beijing dates.
const REPOSITORY = "hhhhhades/ncmp";
const WORKFLOW = "auto_score.yml";
const WORKFLOW_PATH = ".github/workflows/" + WORKFLOW;
const API_BASE = "https://api.github.com/repos/" + REPOSITORY;
const API_TIMEOUT_MS = 15_000;
const MARKER_PREFIX = "ncmp-score-completed-v1-";

export function beijingDate(value) {
  const time = typeof value === "number" ? value : Date.parse(value);
  if (!Number.isFinite(time)) return null;
  return new Date(time + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

class GitHubRequestError extends Error {
  constructor(code, httpStatus) {
    super(code);
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

// Never include GitHub response bodies, URLs, headers, or exception messages in logs.
async function githubRequest(path, token, { fetcher, body } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  try {
    const response = await fetcher(API_BASE + path, {
      method: body === undefined ? "GET" : "POST",
      redirect: "error",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ncmp-backup-scheduler",
        ...(body === undefined ? {} : { "Content-Type": "application/json" })
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: controller.signal
    });
    if (body !== undefined) {
      if (response.status !== 204 && response.status !== 200) {
        throw new GitHubRequestError("api_http_error", response.status);
      }
      return;
    }
    if (response.status !== 200) {
      throw new GitHubRequestError("api_http_error", response.status);
    }
    try {
      return await response.json();
    } catch {
      throw new GitHubRequestError("api_invalid_json");
    }
  } catch (error) {
    if (error instanceof GitHubRequestError) throw error;
    throw new GitHubRequestError(controller.signal.aborted ? "api_timeout" : "api_network_error");
  } finally {
    clearTimeout(timeout);
  }
}

async function completedScoreExists(date, token, fetcher) {
  const marker = MARKER_PREFIX + date;
  // Follow every page. A successful/skipped run alone is never proof of scoring.
  for (let page = 1; page <= 100; page++) {
    const data = await githubRequest(
      "/actions/artifacts?name=" + encodeURIComponent(marker) + "&per_page=100&page=" + page,
      token,
      { fetcher }
    );
    if (!data || !Array.isArray(data.artifacts)) {
      throw new GitHubRequestError("api_invalid_artifacts");
    }
    for (const artifact of data.artifacts) {
      if (artifact.name !== marker || artifact.expired !== false ||
          beijingDate(artifact.created_at) !== date ||
          artifact.workflow_run?.head_branch !== "main") continue;
      const runId = artifact.workflow_run.id;
      if (!Number.isSafeInteger(runId) || runId <= 0) continue;
      const run = await githubRequest("/actions/runs/" + runId, token, { fetcher });
      if (run?.id === runId && run.path === WORKFLOW_PATH && run.head_branch === "main") {
        return true;
      }
    }
    if (data.artifacts.length < 100) return false;
  }
  throw new GitHubRequestError("api_pagination_limit");
}

export async function runBackup(env, {
  scheduledTime = Date.now(),
  now = Date.now(),
  verificationOnly = false,
  requestId,
  fetcher = globalThis.fetch,
  logger = console
} = {}) {
  const timestamp = typeof scheduledTime === "number" ? scheduledTime : Date.parse(scheduledTime);
  const date = beijingDate(timestamp);
  const request_id = requestId || "ncmp-backup-" + timestamp;
  const log = (status, details = {}) => logger.log(JSON.stringify({ date, request_id, status, ...details }));
  if (!date || !beijingDate(now)) {
    log("invalid_scheduled_time");
    throw new Error("invalid_scheduled_time");
  }
  // A delayed Cron callback must never turn yesterday's recovery into today's score.
  if (date !== beijingDate(now)) {
    log("expired_cron");
    return { date, request_id, status: "expired_cron" };
  }
  if (!env.GITHUB_TOKEN) {
    log("missing_github_token");
    throw new Error("missing_github_token");
  }

  if (!verificationOnly) {
    try {
      if (await completedScoreExists(date, env.GITHUB_TOKEN, fetcher)) {
        log("already_scored");
        return { date, request_id, status: "already_scored" };
      }
    } catch (error) {
      // The workflow independently checks its marker under a concurrency lock and
      // fails closed if GitHub cannot verify it. Dispatching here cannot bypass it.
      log("marker_check_failed", {
        reason: error instanceof GitHubRequestError ? error.code : "marker_check_error",
        ...(error instanceof GitHubRequestError && Number.isInteger(error.httpStatus)
          ? { http_status: error.httpStatus } : {})
      });
    }
  }

  try {
    await githubRequest("/actions/workflows/" + WORKFLOW + "/dispatches", env.GITHUB_TOKEN, {
      fetcher,
      body: {
        ref: "main",
        inputs: {
          trigger_source: "backup",
          score_date: date,
          request_id,
          verification_only: verificationOnly
        }
      }
    });
  } catch (error) {
    log("dispatch_failed", {
      reason: error instanceof GitHubRequestError ? error.code : "dispatch_error",
      ...(error instanceof GitHubRequestError && Number.isInteger(error.httpStatus)
        ? { http_status: error.httpStatus } : {})
    });
    throw new Error("dispatch_failed");
  }
  const status = verificationOnly ? "verification_dispatched" : "dispatched";
  log(status);
  return { date, request_id, status };
}

async function authorized(request, secret) {
  if (typeof secret !== "string" || !secret) return false;
  const header = request.headers.get("Authorization") || "";
  const match = /^Bearer ([^\s]+)$/i.exec(header);
  if (!match || match[1].length > 4096) return false;
  // Compare fixed-size hashes so the equality test does not reveal token prefixes.
  const encoder = new TextEncoder();
  const [provided, expected] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(match[1])),
    crypto.subtle.digest("SHA-256", encoder.encode(secret))
  ]);
  const left = new Uint8Array(provided);
  const right = new Uint8Array(expected);
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left[i] ^ right[i];
  return difference === 0;
}

export default {
  async scheduled(event, env, ctx) {
    // Removed/temporary triggers can continue propagating after a deployment.
    // Only the two production schedules may request a real score.
    if (!["45 18 * * *", "15 20 * * *"].includes(event.cron)) {
      console.log(JSON.stringify({ status: "ignored_unconfigured_cron" }));
      return;
    }
    ctx.waitUntil(runBackup(env, { scheduledTime: event.scheduledTime }));
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/verify" || request.method !== "POST" || url.search) {
      return new Response("Not Found", { status: 404 });
    }
    if (!await authorized(request, env.GITHUB_TOKEN)) {
      return new Response("Unauthorized", { status: 401 });
    }
    const now = Date.now();
    try {
      const result = await runBackup(env, {
        scheduledTime: now,
        now,
        verificationOnly: true,
        requestId: "ncmp-verify-" + now + "-" + crypto.randomUUID()
      });
      return Response.json(result, { status: 200 });
    } catch {
      return Response.json({ status: "dispatch_failed" }, { status: 502 });
    }
  }
};
