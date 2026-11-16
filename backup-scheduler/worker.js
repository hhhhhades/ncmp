// Cloudflare Worker: independent fallback for the daily NCMP scoring workflow.
// Scheduled triggers use UTC; the two configured times are 02:45 and 04:15 Beijing time.
const REPOSITORY = "hhhhhades/ncmp";
const WORKFLOW = "auto_score.yml";
const API_BASE = "https://api.github.com/repos/" + REPOSITORY;

function beijingDate(value) {
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return null;
  return new Date(time + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

async function runBackup(env) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("Missing Cloudflare Worker secret: GITHUB_TOKEN");
  }

  const today = beijingDate(new Date().toISOString());
  const headers = {
    "Accept": "application/vnd.github+json",
    "Authorization": "Bearer " + env.GITHUB_TOKEN,
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ncmp-backup-scheduler"
  };

  // Avoid creating an unnecessary Actions run when a successful score already exists.
  // If this read fails, dispatch anyway: the workflow has its own daily guard.
  try {
    const runsResponse = await fetch(
      API_BASE + "/actions/workflows/" + WORKFLOW +
        "/runs?branch=main&status=success&per_page=100",
      { headers }
    );
    if (!runsResponse.ok) {
      throw new Error("GitHub runs API returned HTTP " + runsResponse.status);
    }
    const data = await runsResponse.json();
    const succeededToday = Array.isArray(data.workflow_runs) &&
      data.workflow_runs.some((run) =>
        run.conclusion === "success" &&
        run.run_started_at &&
        beijingDate(run.run_started_at) === today
      );
    if (succeededToday) {
      console.log("NCMP: scoring already succeeded on " + today + " (Beijing); skipping backup.");
      return;
    }
  } catch (error) {
    console.warn("NCMP: unable to check prior runs; dispatching for workflow-side deduplication: " + String(error));
  }

  const dispatchResponse = await fetch(
    API_BASE + "/actions/workflows/" + WORKFLOW + "/dispatches",
    {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({
        ref: "main",
        inputs: { trigger_source: "backup" }
      })
    }
  );
  if (!dispatchResponse.ok) {
    throw new Error("NCMP: backup dispatch failed (HTTP " + dispatchResponse.status + ")");
  }
  console.log("NCMP: backup dispatched Auto Score for Beijing date " + today + ".");
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(runBackup(env));
  },
  async fetch() {
    return new Response("Not Found", { status: 404 });
  }
};
