"""Fail-closed daily scoring guard shared by every workflow trigger.

Only a completion artifact from the scoring workflow proves that scoring ran.
A workflow's conclusion or start time cannot provide that guarantee.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_BASE = "https://api.github.com"
BEIJING = timezone(timedelta(hours=8))
WORKFLOW_PATH = ".github/workflows/auto_score.yml"
MARKER_PREFIX = "ncmp-score-completed-v1-"
PAGE_SIZE = 100
MAX_ARTIFACT_PAGES = 100


class GuardError(Exception):
    """A safe-to-display guard error; never includes response bodies or headers."""


class _NoRedirects(HTTPRedirectHandler):
    # Never forward the Authorization header to a redirect target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubClient:
    def __init__(self, repository, token, opener=None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository or ""):
            raise GuardError("GITHUB_REPOSITORY must be an owner/repository name.")
        owner, repo = repository.split("/")
        if repo in (".", ".."):
            raise GuardError("GITHUB_REPOSITORY must be an owner/repository name.")
        if not token or "\r" in token or "\n" in token:
            raise GuardError("GH_TOKEN is missing or invalid.")
        self.repository_path = "/repos/" + quote(owner, safe="") + "/" + quote(repo, safe="")
        self._token = token
        self._opener = opener or build_opener(_NoRedirects())

    def get_json(self, path):
        if not path.startswith(self.repository_path + "/") or "\r" in path or "\n" in path:
            raise GuardError("Invalid GitHub API path.")
        try:
            request = Request(
                API_BASE + path,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": "Bearer " + self._token,
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "ncmp-daily-guard",
                },
                method="GET",
            )
            with self._opener.open(request, timeout=30) as response:
                # Artifact/run metadata is small; reject an unexpectedly large response.
                raw = response.read(10 * 1024 * 1024 + 1)
                if len(raw) > 10 * 1024 * 1024:
                    raise GuardError("GitHub API response exceeded the metadata limit.")
            payload = json.loads(raw)
        except HTTPError as error:
            raise GuardError("GitHub API request failed (HTTP %s)." % error.code) from None
        except (URLError, TimeoutError, OSError):
            raise GuardError("GitHub API request failed; unable to verify daily completion.") from None
        except (ValueError, TypeError):
            raise GuardError("GitHub API returned invalid metadata.") from None
        if not isinstance(payload, dict):
            raise GuardError("GitHub API returned invalid metadata.")
        return payload


@dataclass(frozen=True)
class Decision:
    should_run: bool
    score_date: str
    reason: str


def beijing_date(timestamp):
    """Convert an aware timestamp to a Beijing calendar date without tzdata."""
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise GuardError("GitHub completion marker has an invalid creation time.") from None
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise GuardError("Daily guard requires a timestamp with a timezone.")
    return timestamp.astimezone(BEIJING).date().isoformat()


def _requested_day(requested):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", requested):
        raise GuardError("REQUESTED_SCORE_DATE must be a valid YYYY-MM-DD date.")
    try:
        return date.fromisoformat(requested).isoformat()
    except ValueError:
        raise GuardError("REQUESTED_SCORE_DATE must be a valid YYYY-MM-DD date.") from None


def find_completion_run(client, score_date):
    """Find a trusted, unexpired completion artifact, including later API pages."""
    marker_name = MARKER_PREFIX + score_date
    checked_runs = {}
    page = 1
    while True:
        query = urlencode({"name": marker_name, "per_page": PAGE_SIZE, "page": page})
        payload = client.get_json(client.repository_path + "/actions/artifacts?" + query)
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            raise GuardError("GitHub artifacts API returned invalid metadata.")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise GuardError("GitHub artifacts API returned invalid metadata.")
            # The API's name filter is insufficient: independently check exact identity.
            if artifact.get("name") != marker_name:
                continue
            if artifact.get("expired") is True:
                continue
            if artifact.get("expired") is not False:
                raise GuardError("GitHub completion marker has an invalid expiry state.")
            if beijing_date(artifact.get("created_at")) != score_date:
                continue
            workflow_run = artifact.get("workflow_run")
            if not isinstance(workflow_run, dict):
                raise GuardError("GitHub completion marker is missing its workflow run.")
            if workflow_run.get("head_branch") != "main":
                continue
            run_id = workflow_run.get("id")
            if type(run_id) is not int or run_id <= 0:
                raise GuardError("GitHub completion marker has an invalid workflow run.")
            if run_id not in checked_runs:
                run = client.get_json(client.repository_path + "/actions/runs/" + str(run_id))
                if not isinstance(run, dict):
                    raise GuardError("GitHub workflow run API returned invalid metadata.")
                checked_runs[run_id] = (
                    type(run.get("id")) is int and run["id"] == run_id
                    and run.get("path") == WORKFLOW_PATH and run.get("head_branch") == "main"
                )
            if checked_runs[run_id]:
                # Ignore conclusion and run_started_at: scoring may have completed
                # before a later step failed, or after a run crossed midnight.
                return run_id
        if len(artifacts) < PAGE_SIZE:
            return None
        if page >= MAX_ARTIFACT_PAGES:
            raise GuardError("GitHub completion marker lookup exceeded the pagination limit.")
        page += 1


def evaluate_guard(client, *, now=None, event_name="workflow_dispatch", trigger_source="manual",
                   requested_score_date="", verification_only=False):
    today = beijing_date(now or datetime.now(timezone.utc))
    if event_name not in ("schedule", "workflow_dispatch"):
        raise GuardError("Unsupported event for the daily scoring guard.")
    source = "schedule" if event_name == "schedule" else (trigger_source or "manual")
    if source not in ("schedule", "manual", "backup"):
        raise GuardError("TRIGGER_SOURCE must be manual or backup.")
    requested = (requested_score_date or "").strip()
    if source == "backup" and not requested:
        raise GuardError("Backup dispatch requires an explicit REQUESTED_SCORE_DATE.")
    score_day = _requested_day(requested) if requested else today
    if score_day != today:
        return Decision(False, score_day, "requested Beijing date is not today; stale or future dispatch skipped")
    completion_run = find_completion_run(client, score_day)
    would_run = completion_run is None
    reason = (
        "no completion marker for Beijing date " + score_day
        if would_run else
        "scoring completed for Beijing date %s (run %s)" % (score_day, completion_run)
    )
    if verification_only:
        return Decision(False, score_day, "verification only; would_run=%s; %s" % (str(would_run).lower(), reason))
    return Decision(would_run, score_day, reason)


def write_decision(decision, output_path, summary_path=None):
    """Write a single final decision, only after every API validation succeeded."""
    if not output_path:
        raise GuardError("GITHUB_OUTPUT is missing.")
    try:
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as summary:
                summary.write(
                    "### NCMP daily guard\n\n"
                    "- Beijing scoring date: `%s`\n"
                    "- Decision: `%s`\n"
                    "- Reason: %s\n\n" % (
                        decision.score_date, "run" if decision.should_run else "skip", decision.reason
                    )
                )
        with open(output_path, "a", encoding="utf-8") as output:
            output.write("should_run=%s\nscore_date=%s\nreason=%s\n" % (
                str(decision.should_run).lower(), decision.score_date, decision.reason
            ))
    except OSError:
        raise GuardError("Unable to write GitHub output files.") from None


def main(environ=None, *, now=None, opener=None):
    env = os.environ if environ is None else environ
    try:
        raw_verification = env.get("VERIFICATION_ONLY", "").strip().lower()
        if raw_verification not in ("", "true", "false"):
            raise GuardError("VERIFICATION_ONLY must be true or false.")
        client = GitHubClient(env.get("GITHUB_REPOSITORY", ""), env.get("GH_TOKEN", ""), opener=opener)
        decision = evaluate_guard(
            client,
            now=now,
            event_name=env.get("GITHUB_EVENT_NAME", "workflow_dispatch"),
            trigger_source=env.get("TRIGGER_SOURCE", "manual"),
            requested_score_date=env.get("REQUESTED_SCORE_DATE", ""),
            verification_only=raw_verification == "true",
        )
        write_decision(decision, env.get("GITHUB_OUTPUT"), env.get("GITHUB_STEP_SUMMARY"))
    except GuardError as error:
        print("::error::Daily guard failed: " + str(error), file=sys.stderr)
        return 1
    except Exception:
        # Unexpected transport/library errors must also fail closed without
        # printing request headers, response bodies, or arbitrary exceptions.
        print("::error::Daily guard failed: unexpected validation error.", file=sys.stderr)
        return 1
    print("Daily guard: " + decision.reason + "; should_run=" + str(decision.should_run).lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
