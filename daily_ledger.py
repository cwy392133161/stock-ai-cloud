"""Durable daily execution fence. Git refs, never Actions cache, own the claim.

Dedicated refs contain only ledger.json, no workflows or credentials. A new
commit has exactly the observed head as its parent; non-forced ref updates
reject sibling writers. Ambiguous writes fail closed before side effects.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from datetime import datetime
import requests
from market_schedule import CHINA, scheduling_decision, is_market_ready

PREFIX = "execution-ledger/a-"
TERMINAL = {"published", "market_failed", "ai_failed", "interrupted_or_unknown",
            "blocked_existing_result"}
TRANSITIONS = {
    "market_started": {"market_completed", "market_failed", "interrupted_or_unknown"},
    "market_completed": {"ai_started", "publication_started", "interrupted_or_unknown"},
    "report_reserved": {"ai_started", "interrupted_or_unknown"},
    "ai_started": {"ai_completed", "ai_failed", "interrupted_or_unknown"},
    "ai_completed": {"publication_started", "interrupted_or_unknown"},
    "ai_failed": {"publication_started"},
    "market_failed": {"publication_started"},
    "publication_started": {"published", "interrupted_or_unknown"},
}
SHA = re.compile(r"[0-9a-f]{40}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

class FenceError(RuntimeError):
    pass

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def stamp():
    return datetime.now(CHINA).isoformat(timespec="seconds")

def valid_day(value):
    if not isinstance(value, str) or not DAY.fullmatch(value):
        raise FenceError("invalid trading day")
    datetime.strptime(value, "%Y-%m-%d")
    return value

def ref_for(day):
    return "heads/" + PREFIX + valid_day(day)

class GitStore:
    """GitHub.com only; no redirects, credential logging, retries or force."""
    def __init__(self, repo, token, session=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or "") or not token:
            raise FenceError("missing repository/token")
        self.url = "https://api.github.com/repos/" + repo
        self.session = session or requests.Session()
        self.headers = {"Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}

    def api(self, method, path, payload=None, missing=False):
        try:
            r = self.session.request(method, self.url + path, headers=self.headers,
                                     json=payload, timeout=30, allow_redirects=False)
        except requests.RequestException:
            raise FenceError("ledger transport result unknown; no automatic retry") from None
        if r.status_code == 404 and missing:
            return None
        if r.status_code not in (200, 201):
            raise FenceError("ledger API rejected or uncertain: HTTP " + str(r.status_code))
        try:
            return r.json()
        except ValueError:
            raise FenceError("ledger response invalid") from None

    def main_head(self):
        return self.api("GET", "/git/ref/heads/main")["object"]["sha"]

    def read(self, day):
        ref = ref_for(day)
        data = self.api("GET", "/git/ref/" + ref, missing=True)
        if data is None:
            return None, None
        head = data["object"]["sha"]
        commit = self.api("GET", "/git/commits/" + head)
        tree = self.api("GET", "/git/trees/" + commit["tree"]["sha"])
        entries = tree.get("tree", [])
        if len(entries) != 1 or entries[0].get("path") != "ledger.json" or entries[0].get("type") != "blob":
            raise FenceError("invalid ledger tree")
        blob = self.api("GET", "/git/blobs/" + entries[0]["sha"])
        if blob.get("encoding") != "base64":
            raise FenceError("invalid ledger encoding")
        try:
            record = json.loads(base64.b64decode(blob["content"], validate=False))
        except (ValueError, KeyError):
            raise FenceError("invalid ledger payload") from None
        if record.get("version") != 1 or record.get("day") != day:
            raise FenceError("invalid ledger identity")
        return head, record

    def write(self, day, expected, record, base):
        parent = expected or base
        if not SHA.fullmatch(parent or ""):
            raise FenceError("invalid parent")
        tree = self.api("POST", "/git/trees", {"tree": [
            {"path": "ledger.json", "mode": "100644", "type": "blob", "content": encode(record)}]})
        commit = self.api("POST", "/git/commits", {
            "message": "Daily execution audit " + day + " " + record["phase"],
            "tree": tree["sha"], "parents": [parent]})
        sha = commit["sha"]
        if expected is None:
            self.api("POST", "/git/refs", {"ref": "refs/" + ref_for(day), "sha": sha})
        else:
            self.api("PATCH", "/git/refs/" + ref_for(day), {"sha": sha, "force": False})
        # Even a successful HTTP response is not authority to spend until read-back.
        observed, saved = self.read(day)
        if observed != sha or saved != record:
            raise FenceError("ledger write not confirmed; stop before side effect")
        return sha

class Ledger:
    def __init__(self, store, day, owner, base):
        self.store, self.day, self.owner, self.base = store, valid_day(day), owner, base

    def claim(self, phase, existing=False):
        head, record = self.store.read(self.day)
        if head is not None:
            raise FenceError("trading day already claimed; inspect durable audit, never auto-retry")
        if phase not in ("market_started", "report_reserved"):
            raise FenceError("invalid initial phase")
        phase = "blocked_existing_result" if existing else phase
        record = {"version": 1, "day": self.day, "owner": self.owner, "base": self.base,
                  "phase": phase, "ai_ever_started": False,
                  "events": [{"phase": phase, "at": stamp(), "event_id": uuid.uuid4().hex}]}
        self.store.write(self.day, None, record, self.base)
        if existing:
            raise FenceError("published result predates ledger; refuse automatic regeneration")
        return record

    def transition(self, phase, details=None):
        head, record = self.store.read(self.day)
        if not head or record["owner"] != self.owner or record["base"] != self.base:
            raise FenceError("lost or invalid ownership")
        if phase not in TRANSITIONS.get(record["phase"], set()):
            raise FenceError("transition refused: " + record["phase"] + " -> " + phase)
        record = copy.deepcopy(record)
        record["phase"] = phase
        record["ai_ever_started"] = record["ai_ever_started"] or phase == "ai_started"
        event = {"phase": phase, "at": stamp(), "event_id": uuid.uuid4().hex}
        if details is not None:
            event["details"] = details
        record["events"].append(event)
        self.store.write(self.day, head, record, self.base)
        return record

    def finish(self):
        _, record = self.store.read(self.day)
        if record is None or record["owner"] != self.owner:
            raise FenceError("cannot finalize another execution")
        # Do not relabel an explicit outcome, and never clear the paid fence.
        if record["phase"] not in TERMINAL:
            self.transition("interrupted_or_unknown")

def context(env, now, snapshot, status):
    if env.get("GITHUB_REF") != "refs/heads/main":
        raise FenceError("only the main daily workflow may acquire a live fence")
    run, attempt, base = (env.get(k, "") for k in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA"))
    if not re.fullmatch(r"[0-9]+", run) or attempt != "1" or not SHA.fullmatch(base):
        raise FenceError("invalid execution or rerun; manual reconciliation required")
    report = env.get("REPORT_ONLY") == "true"
    event = env.get("GITHUB_EVENT_NAME")
    if event not in ("schedule", "workflow_dispatch") or (report and event != "workflow_dispatch"):
        raise FenceError("unsupported execution event")
    if not report and not scheduling_decision(now, event, attempt)["market_ready"]:
        raise FenceError("outside unchanged publication window")
    local_day = now.astimezone(CHINA).date().isoformat()
    snapshot_day = str(snapshot.get("updated", ""))[:10]
    day = valid_day(snapshot_day if report else local_day)
    if report:
        rows = snapshot.get("a_shares", [])
        codes = [row.get("代码") for row in rows]
        if len(codes) != 20 or len(set(codes)) != 20 or not all(codes):
            raise FenceError("incomplete A-share snapshot")
    existing = (not report and snapshot_day == day) or (
        report and str(status.get("report_snapshot", ""))[:10] == day and
        bool(status.get("research_generation_id")))
    return day, run + ":" + attempt, base, report, existing

def record_recovery(store, day, expected, recovery_commit):
    """Record a human-supplied recovery commit, never unlock or certify success."""
    if not SHA.fullmatch(expected or "") or not SHA.fullmatch(recovery_commit or ""):
        raise FenceError("recovery requires exact commit identifiers")
    head, record = store.read(valid_day(day))
    if head != expected or record is None:
        raise FenceError("recovery lease mismatch")
    store.api("GET", "/git/commits/" + recovery_commit)
    updated = copy.deepcopy(record)
    updated["events"].append({"phase": "manual_recovery_reference", "at": stamp(), "event_id": uuid.uuid4().hex,
                              "details": {"commit": recovery_commit,
                                          "verification": "commit-exists-only-not-publication-or-billing-proof"}})
    store.write(day, expected, updated, record["base"])
    return updated

def execute_market(ledger, commands, runner, on_claim=lambda: None):
    ledger.claim("market_started")
    on_claim()
    try:
        for command in commands:
            runner(command, check=True)
    except BaseException:
        ledger.transition("market_failed")
        raise
    ledger.transition("market_completed")

def execute_ai(ledger, runner):
    # Durable attempt covers the entire batch, including partial paid replies,
    # cache misses and the existing within-run provider retry policy.
    ledger.transition("ai_started")
    try:
        runner([sys.executable, "ai_report.py"], check=True)
    except BaseException:
        ledger.transition("ai_failed")
        raise
    ledger.transition("ai_completed", {"meaning": "process-exited-zero-not-proof-of-all-model-success"})

def files_digest():
    paths = ["docs/data.json", "docs/ai.html", "docs/ai-status.json", "docs/index.html"]
    return {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths if Path(p).is_file()}

def local_file():
    root = os.environ.get("RUNNER_TEMP")
    if not root:
        raise FenceError("RUNNER_TEMP missing")
    return Path(root) / "daily-execution-context.json"

def save_context(day, owner, base, report):
    # Metadata only; tokens/keys/prompt/response text are never persisted.
    local_file().write_text(encode({"day": day, "owner": owner, "base": base, "report": report}))

def loaded(store):
    d = json.loads(local_file().read_text())
    owner = os.environ.get("GITHUB_RUN_ID", "") + ":" + os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if d["owner"] != owner or d["base"] != os.environ.get("GITHUB_SHA"):
        raise FenceError("local context belongs to another execution")
    return Ledger(store, d["day"], d["owner"], d["base"]), d

def window(d):
    now = datetime.now(CHINA)
    if not d["report"] and (not is_market_ready(now) or now.date().isoformat() != d["day"]):
        raise FenceError("execution crossed publication window/date; do not continue")

def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    command = args[0] if args else ""
    if command == "record-recovery":
        if len(args) != 4 or os.getenv("GITHUB_ACTIONS") == "true":
            raise FenceError("manual recovery audit requires day, expected head, recovery commit; not an Actions step")
        store = GitStore(os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_TOKEN"))
        record_recovery(store, args[1], args[2], args[3])
        return 0
    if command not in ("run-market", "run-ai", "before-publish", "published", "finish"):
        raise FenceError("unknown command")
    # Late/pre-close diagnostic-only runs have no claim and retain existing banners.
    if command in ("before-publish", "published", "finish") and not local_file().exists():
        if command != "finish" and local_file().with_suffix(".entered").exists():
            raise FenceError("acquisition failed or unknown; refuse publication")
        return 0
    if command in ("run-market", "run-ai"):
        local_file().with_suffix(".entered").write_text("attempted")
    store = GitStore(os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_TOKEN"))
    if command == "run-market" or (command == "run-ai" and not local_file().exists()):
        snapshot = json.loads(Path("docs/data.json").read_text())
        status = json.loads(Path("docs/ai-status.json").read_text())
        day, owner, base, report, existing = context(os.environ, datetime.now(CHINA), snapshot, status)
        if (command == "run-market") == report:
            raise FenceError("wrong entry point")
        if store.main_head() != base:
            raise FenceError("checkout is stale relative to main; do not collect or spend")
        ledger = Ledger(store, day, owner, base)
        print("Daily execution audit: refs/" + ref_for(day) + "; owner=" + owner, flush=True)
        # No local ownership context is created on a lost/ambiguous acquisition.
        if existing:
            ledger.claim("report_reserved" if report else "market_started", existing=True)
        if command == "run-market":
            execute_market(ledger,
                           [[sys.executable, name] for name in
                            ("picker.py", "trend_audit.py", "a_share_history.py")],
                           subprocess.run,
                           lambda: save_context(day, owner, base, report))
            return 0
        ledger.claim("report_reserved")
        save_context(day, owner, base, report)
    ledger, d = loaded(store)
    if command == "finish":
        ledger.finish()
        return 0
    window(d)
    if command == "run-ai":
        snapshot = json.loads(Path("docs/data.json").read_text())
        if str(snapshot.get("updated", ""))[:10] != d["day"]:
            raise FenceError("AI input date differs from claimed date")
        execute_ai(ledger, subprocess.run)
    elif command == "before-publish":
        ledger.transition("publication_started", {"files_sha256": files_digest()})
    elif command == "published":
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if not SHA.fullmatch(sha) or store.main_head() != sha:
            raise FenceError("publication result unknown; manual reconciliation required")
        ledger.transition("published", {"commit": sha, "files_sha256": files_digest()})
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FenceError, KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
        # Do not dump response bodies, env, credentials or persisted research.
        print("::error::daily execution fence stopped: " + type(exc).__name__)
        raise SystemExit(1)
