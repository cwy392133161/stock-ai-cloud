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

DIAGNOSTIC_CODES = {
    "policy_denied", "permission_or_policy_denied", "conflict_or_validation",
    "rate_limited", "redirect_rejected", "http_rejected", "read_uncertain",
    "write_uncertain", "readback_uncertain", "readback_mismatch", "invalid_response",
    "snapshot_rejected", "handoff_denied", "already_claimed", "stale_checkout",
    "ownership_lost", "phase_rejected", "local_io_failed", "process_failed", "invalid_state",
}
OPERATIONS = {"read-reference", "create-reference", "update-reference", "read-commit",
              "read-tree", "read-blob", "create-tree", "create-commit", "confirm-write",
              "unknown"}

class FenceError(RuntimeError):
    def __init__(self, message, code="policy_denied", operation=None, status=None):
        super().__init__(message)
        self.code = code if code in DIAGNOSTIC_CODES else "policy_denied"
        self.operation = operation if operation in OPERATIONS else None
        self.status = status if type(status) is int and 100 <= status <= 599 else None

def diagnostic(exc):
    """Allowlisted metadata only; never print exception text or server bodies."""
    if isinstance(exc, FenceError):
        value = "code=" + exc.code
        if exc.operation:
            value += " operation=" + exc.operation
        if exc.status is not None:
            value += " http=" + str(exc.status)
        return value
    if isinstance(exc, subprocess.SubprocessError):
        return "code=process_failed"
    if isinstance(exc, OSError):
        return "code=local_io_failed"
    return "code=invalid_state"

def api_operation(method, path):
    if path.startswith("/git/ref/"):
        return "read-reference"
    if path.startswith("/git/refs"):
        return "create-reference" if method == "POST" else "update-reference"
    for part, name in (("commits", "commit"), ("trees", "tree"), ("blobs", "blob")):
        if path.startswith("/git/" + part):
            return ("create-" if method == "POST" else "read-") + name
    return "unknown"

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
        operation = api_operation(method, path)
        uncertain = "read_uncertain" if method == "GET" else "write_uncertain"
        try:
            r = self.session.request(method, self.url + path, headers=self.headers,
                                     json=payload, timeout=30, allow_redirects=False)
        except requests.RequestException:
            raise FenceError("ledger transport result unknown", uncertain, operation) from None
        status = r.status_code
        if status == 404 and missing:
            return None
        if status not in (200, 201):
            if status in (401, 403):
                code = "permission_or_policy_denied"
            elif status in (409, 422):
                code = "conflict_or_validation"
            elif status == 429:
                code = "rate_limited"
            elif 300 <= status < 400:
                code = "redirect_rejected"
            elif status >= 500:
                code = uncertain
            else:
                code = "http_rejected"
            raise FenceError("ledger API stopped", code, operation, status)
        try:
            return r.json()
        except ValueError:
            raise FenceError("ledger response invalid", uncertain, operation, status) from None

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
        try:
            observed, saved = self.read(day)
        except (FenceError, KeyError, ValueError, TypeError):
            raise FenceError("ledger readback uncertain", "readback_uncertain", "confirm-write") from None
        if observed != sha or saved != record:
            raise FenceError("ledger write not confirmed; stop before side effect", "readback_mismatch", "confirm-write")
        return sha

class Ledger:
    def __init__(self, store, day, owner, base):
        self.store, self.day, self.owner, self.base = store, valid_day(day), owner, base

    def claim(self, phase, existing=False, skip_ai=False):
        head, record = self.store.read(self.day)
        if head is not None:
            raise FenceError("trading day already claimed; inspect durable audit, never auto-retry", "already_claimed")
        if phase not in ("market_started", "report_reserved"):
            raise FenceError("invalid initial phase")
        phase = "blocked_existing_result" if existing else phase
        record = {"version": 1, "day": self.day, "owner": self.owner, "base": self.base,
                  "phase": phase, "ai_ever_started": False, "ai_claimed_once": False,
                  "skip_ai": skip_ai is True, "origin_kind": "market" if phase == "market_started" else "report",
                  "events": [{"phase": phase, "at": stamp(), "event_id": uuid.uuid4().hex}]}
        self.store.write(self.day, None, record, self.base)
        if existing:
            raise FenceError("published result predates ledger; refuse automatic regeneration")
        return record

    def claim_report(self, snapshot, snapshot_sha256, status, status_sha256, now):
        """Only an audited same-day skip_ai publication can hand off AI once."""
        fresh_snapshot(snapshot, now)
        head, record = self.store.read(self.day)
        def denied():
            raise FenceError("skip_ai handoff proof missing or incompatible", "handoff_denied")
        if (not head or not record or record.get("version") != 1 or
                record.get("day") != self.day or self.day != str(snapshot.get("updated", ""))[:10] or
                record.get("phase") != "published" or record.get("skip_ai") is not True or
                record.get("origin_kind") != "market" or record.get("ai_ever_started") is not False or
                record.get("ai_claimed_once") is not False or record.get("owner") == self.owner):
            denied()
        events = record.get("events", [])
        if [e.get("phase") for e in events] != [
                "market_started", "market_completed", "publication_started", "published"]:
            denied()
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256 or "") or not re.fullmatch(r"[0-9a-f]{64}", status_sha256 or ""):
            denied()
        for event in events[-2:]:
            details = event.get("details", {})
            hashes = details.get("files_sha256", {})
            if (hashes.get("docs/data.json") != snapshot_sha256 or
                    hashes.get("docs/ai-status.json") != status_sha256 or
                    details.get("snapshot_updated") != snapshot["updated"]):
                denied()
        published = events[-1]["details"].get("commit")
        if not SHA.fullmatch(published or ""):
            denied()
        if (status.get("snapshot") != snapshot["updated"] or
                status.get("screening_outcome") != "success" or
                status.get("last_screening_outcome") != "success" or
                status.get("market_state") != "updated" or status.get("ai_step") != "skipped" or
                (str(status.get("report_snapshot", ""))[:10] == self.day and
                 status.get("research_generation_id"))):
            denied()
        updated = copy.deepcopy(record)
        updated.update(owner=self.owner, base=self.base, phase="report_reserved", ai_claimed_once=True)
        updated["events"].append({
            "phase": "report_reserved", "at": stamp(), "event_id": uuid.uuid4().hex,
            "details": {"previous_owner": record["owner"], "previous_base": record["base"],
                        "previous_ledger_head": head, "skip_ai_publication_commit": published,
                        "snapshot_sha256": snapshot_sha256, "snapshot_updated": snapshot["updated"]}})
        # Both owner transfer and the permanent claim bit use the same atomic update.
        # No local capability exists until the write AND readback have succeeded.
        self.store.write(self.day, head, updated, self.base)
        return updated

    def transition(self, phase, details=None):
        head, record = self.store.read(self.day)
        if not head or record["owner"] != self.owner or record["base"] != self.base:
            raise FenceError("lost or invalid ownership", "ownership_lost")
        if phase not in TRANSITIONS.get(record["phase"], set()):
            raise FenceError("transition refused", "phase_rejected")
        record = copy.deepcopy(record)
        record["phase"] = phase
        record["ai_ever_started"] = record["ai_ever_started"] or phase == "ai_started"
        record["ai_claimed_once"] = record.get("ai_claimed_once", False) or phase == "ai_started"
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

def fresh_snapshot(snapshot, now):
    """Same Beijing date, not future, complete ranking, unchanged close window."""
    try:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("naive clock")
        updated = snapshot["updated"]
        parsed = datetime.strptime(updated, "%Y-%m-%d %H:%M").replace(tzinfo=CHINA)
        local = now.astimezone(CHINA)
        rows = snapshot["a_shares"]
        codes = [row["代码"] for row in rows]
        if (parsed.strftime("%Y-%m-%d %H:%M") != updated or
                parsed.date() != local.date() or parsed > local or not is_market_ready(local) or
                len(codes) != 20 or len(set(codes)) != 20 or not all(codes)):
            raise ValueError("snapshot rejected")
    except (ValueError, KeyError, TypeError):
        raise FenceError("snapshot not eligible for same-day handoff", "snapshot_rejected") from None

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
        fresh_snapshot(snapshot, now)
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

def execute_market(ledger, commands, runner, on_claim=lambda: None, skip_ai=False):
    ledger.claim("market_started", skip_ai=skip_ai)
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

def save_context(day, owner, base, report, snapshot_sha256=None):
    # Metadata only; tokens/keys/prompt/response text are never persisted.
    local_file().write_text(encode({"day": day, "owner": owner, "base": base, "report": report,
                                      "snapshot_sha256": snapshot_sha256}))

def loaded(store):
    d = json.loads(local_file().read_text())
    owner = os.environ.get("GITHUB_RUN_ID", "") + ":" + os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if d["owner"] != owner or d["base"] != os.environ.get("GITHUB_SHA"):
        raise FenceError("local context belongs to another execution")
    return Ledger(store, d["day"], d["owner"], d["base"]), d

def window(d):
    now = datetime.now(CHINA)
    if not is_market_ready(now) or now.date().isoformat() != d["day"]:
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
            raise FenceError("checkout is stale relative to main; do not collect or spend", "stale_checkout")
        ledger = Ledger(store, day, owner, base)
        print("Daily execution audit: refs/" + ref_for(day) + "; owner=" + owner, flush=True)
        # No local ownership context is created on a lost/ambiguous acquisition.
        if existing and not report:
            ledger.claim("market_started", existing=True)
        if command == "run-market":
            execute_market(ledger,
                           [[sys.executable, name] for name in
                            ("picker.py", "trend_audit.py", "a_share_history.py")],
                           subprocess.run,
                           lambda: save_context(day, owner, base, report),
                           skip_ai=os.getenv("SKIP_AI") == "true")
            return 0
        digest = hashlib.sha256(Path("docs/data.json").read_bytes()).hexdigest()
        status_digest = hashlib.sha256(Path("docs/ai-status.json").read_bytes()).hexdigest()
        ledger.claim_report(snapshot, digest, status, status_digest, datetime.now(CHINA))
        save_context(day, owner, base, report, digest)
    ledger, d = loaded(store)
    if command == "finish":
        ledger.finish()
        return 0
    window(d)
    if command == "run-ai":
        snapshot = json.loads(Path("docs/data.json").read_text())
        if str(snapshot.get("updated", ""))[:10] != d["day"]:
            raise FenceError("AI input date differs from claimed date")
        if d["report"]:
            fresh_snapshot(snapshot, datetime.now(CHINA))
            digest = hashlib.sha256(Path("docs/data.json").read_bytes()).hexdigest()
            if digest != d.get("snapshot_sha256"):
                raise FenceError("snapshot changed after handoff", "snapshot_rejected")
        execute_ai(ledger, subprocess.run)
    elif command == "before-publish":
        ledger.transition("publication_started", {"files_sha256": files_digest(),
                                                   "snapshot_updated": json.loads(Path("docs/data.json").read_text()).get("updated")})
    elif command == "published":
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if not SHA.fullmatch(sha) or store.main_head() != sha:
            raise FenceError("publication result unknown; manual reconciliation required")
        ledger.transition("published", {"commit": sha, "files_sha256": files_digest(),
                                        "snapshot_updated": json.loads(Path("docs/data.json").read_text()).get("updated")})
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FenceError, KeyError, ValueError, TypeError, AttributeError, OSError, subprocess.SubprocessError) as exc:
        # Do not dump response bodies, env, credentials or persisted research.
        print("::error::daily execution fence stopped: " + diagnostic(exc))
        raise SystemExit(1)
