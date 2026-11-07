#!/usr/bin/env python3
"""Advance this repository's release of the newest private release by one step.

Usage: follow_release.py [--dry-run]

Run on a schedule by follow-private-release.yml. The private repository holds
only a read token, so this repository follows it rather than being pushed to.
decide() is the whole policy. A release goes live when main carries its
release.json, so nothing happens until orkmeter.com serves the wheel. A step
that fails is never started again by this script: it opens one issue and waits.

Environment: GH_TOKEN (this repository), PRIVATE_TOKEN (read-only, private
repository), GITHUB_REPOSITORY. Python 3.10+, standard library and gh only.
"""
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

PRIVATE_REPOSITORY = "EDOS-Engineering/codex-plugin"
RELEASES = "https://orkmeter.com/releases/codex/"
FIRST_FOLLOWED = (0, 1, 4)
ISSUE_PREFIX = "Release follow:"
VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
FAILED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}


def numbers(version):
    return tuple(int(part) for part in version.split("."))


def advance_pr(pr):
    if pr["ejected"]:
        return [("notify", f"the sync pull request #{pr['number']} left the merge queue without merging")]
    if pr["in_queue"]:
        return [("wait", f"sync pull request #{pr['number']} is in the merge queue")]
    if pr["checks"] == "none":
        return [("start-ci", pr["branch"])]
    if pr["checks"] == "failed":
        return [("notify", f"the sync pull request #{pr['number']} failed its checks")]
    if pr["checks"] == "pending":
        return [("wait", f"sync pull request #{pr['number']} is being checked")]
    return [("enqueue", pr["id"])]


def advance_run(workflow, version, run):
    if run == "active":
        return [("wait", f"{workflow} {version} is running or waiting for approval")]
    if run == "failed":
        return [("notify", f"{workflow} {version} failed")]
    if run == "success":
        return [("notify", f"{workflow} {version} succeeded, but the next step did not appear")]
    return [("dispatch", workflow)]


def decide(state):
    """state -> [(action, detail)]. Pure, and every state is unit tested."""
    version = state["version"]
    if version is None:
        return [("done", "no private release to follow")]
    if state["released"]:
        return [("done", f"{version} is released")]
    main = state["main_version"]
    if numbers(main) > numbers(version):
        return [("done", f"main is already past {version}")]
    if main == version:
        return advance_run("publish-release", version, state["runs"].get("publish-release"))
    if not state["delivered"]:
        return [("notify", f"deliver {version} to orkmeter.com")]
    if state["sync_pr"]:
        return advance_pr(state["sync_pr"])
    return advance_run("public-sync", version, state["runs"].get("public-sync"))


def gh(*args, token=None):
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, env=env).stdout


def newest_private_release(token):
    listed = json.loads(gh("release", "list", "-R", PRIVATE_REPOSITORY, "--exclude-drafts", "--exclude-pre-releases",
                           "--limit", "30", "--json", "tagName", token=token))
    found = []
    for release in listed:
        match = VERSION.fullmatch(release["tagName"])
        if match:
            found.append(tuple(int(part) for part in match.groups()))
    newest = max(found, default=None)
    if newest is None or newest < FIRST_FOLLOWED:
        return None
    return ".".join(str(part) for part in newest)


def run_state(repository, workflow, version):
    """The newest run of a workflow for this version, by the run-name it sets."""
    runs = json.loads(gh("run", "list", "-R", repository, "--workflow", f"{workflow}.yml", "--limit", "30",
                         "--json", "status,conclusion,displayTitle"))
    for run in runs:
        if run["displayTitle"] == f"{workflow} {version}":
            if run["status"] != "completed":
                return "active"
            return "success" if run["conclusion"] == "success" else "failed"
    return None


PR_QUERY = """query($owner:String!,$name:String!,$head:String!){repository(owner:$owner,name:$name){
pullRequests(headRefName:$head,states:OPEN,first:1){nodes{id number headRefName isInMergeQueue
timelineItems(itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT],first:1){nodes{__typename}}
commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100){nodes{... on CheckRun{name status conclusion}}}}}}}}}}}"""


def checks_of(contexts):
    runs = [context for context in contexts if context.get("name")]
    if not runs:
        return "none"
    if any(run["conclusion"] in FAILED for run in runs):
        return "failed"
    gate = [run for run in runs if run["name"] == "ci-gate"]
    return "green" if gate and gate[0]["conclusion"] == "SUCCESS" else "pending"


def open_pr(repository, branch):
    owner, name = repository.split("/")
    data = json.loads(gh("api", "graphql", "-f", f"query={PR_QUERY}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"head={branch}"))
    nodes = data["data"]["repository"]["pullRequests"]["nodes"]
    if not nodes:
        return None
    pr = nodes[0]
    rollup = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
    return {"id": pr["id"], "number": pr["number"], "branch": pr["headRefName"], "in_queue": pr["isInMergeQueue"],
            "ejected": bool(pr["timelineItems"]["nodes"]), "checks": checks_of(rollup["contexts"]["nodes"] if rollup else [])}


def delivered(version):
    """True when orkmeter.com serves the wheel main's release.json would pin."""
    url = f"{RELEASES}{version}/ork_meter_codex-{version}-py3-none-any.whl"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as response:  # noqa: S310 - fixed https origin
            return response.status == 200
    except urllib.error.URLError:
        return False


def released(repository, version):
    try:
        gh("release", "view", f"v{version}", "-R", repository, "--json", "tagName")
    except subprocess.CalledProcessError:
        return False
    return True


def gather(repository, token):
    version = newest_private_release(token)
    if version is None:
        return {"version": None}
    if released(repository, version):
        return {"version": version, "released": True}
    content = json.loads(gh("api", f"repos/{repository}/contents/release.json?ref=main"))["content"]
    return {
        "version": version,
        "released": False,
        "main_version": json.loads(base64.b64decode(content))["version"],
        "delivered": delivered(version),
        "sync_pr": open_pr(repository, f"sync/{version}"),
        "runs": {workflow: run_state(repository, workflow, version) for workflow in ("public-sync", "publish-release")},
    }


def sync_issue(repository, message, version):
    """One open issue per message; every other follow issue is closed."""
    title = f"{ISSUE_PREFIX} {message}" if message else None
    issues = json.loads(gh("issue", "list", "-R", repository, "--state", "open", "--search", f'"{ISSUE_PREFIX}" in:title', "--json", "number,title"))
    for issue in issues:
        if issue["title"].startswith(ISSUE_PREFIX) and issue["title"] != title:
            gh("issue", "close", str(issue["number"]), "-R", repository, "--comment", "The release moved on.")
    if title and not any(issue["title"] == title for issue in issues):
        gh("issue", "create", "-R", repository, "--title", title, "--body",
           f"The automatic public release of {version} stopped here. It continues on the next scheduled run once this is resolved.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = argv == ["--dry-run"]
    repository = os.environ["GITHUB_REPOSITORY"]
    state = gather(repository, os.environ["PRIVATE_TOKEN"])
    message = None
    for action, detail in decide(state):
        print(f"{action}: {detail}")
        if action == "notify":
            message = detail
            print(f"::warning::{detail}")
        if dry_run:
            continue
        if action == "dispatch":
            field = "version" if detail == "public-sync" else "plugin_version"
            gh("workflow", "run", f"{detail}.yml", "-R", repository, "--ref", "main", "-f", f"{field}={state['version']}")
        elif action == "start-ci":
            gh("workflow", "run", "ci.yml", "-R", repository, "--ref", detail)
        elif action == "enqueue":
            gh("api", "graphql", "-f", "query=mutation($id:ID!){enqueuePullRequest(input:{pullRequestId:$id}){clientMutationId}}", "-F", f"id={detail}")
    if not dry_run:
        sync_issue(repository, message, state.get("version"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
