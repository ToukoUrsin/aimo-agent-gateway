"""Run the test suites and write what they said to evals/tests-latest.json, which /proof shows next to the model test.

Each suite runs in its own process on its own scratch data folder, never on the live database. Nothing is scored here:
the numbers are the suites' own counts of checks passed and failed, and the exit code.

Run:  uv run -q python -m agent.evidence            (all suites, about ten minutes)
      uv run -q python -m agent.evidence menu rush  (only these)
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evals" / "tests-latest.json"

# id, what a reader is told it covers, command, and whether it needs its own server ("port") opened as localhost or 127.0.0.1.
SUITES = [
    {"id": "flow", "title": "One order end to end", "what": "Offer, signature, approval, order, and the refusals on the way: wrong product, missed cut-off, a price that moved.", "cmd": ["uv", "run", "-q", "python", "-m", "tests.flow"]},
    {"id": "controls", "title": "What an agent can see and do", "what": "No stock counts, no other customers' data, rate limits, the per-reader ledger.", "cmd": ["uv", "run", "-q", "python", "-m", "tests.controls"]},
    {"id": "after", "title": "After the order", "what": "Short pick, delay and batch recall: what the agent may settle alone and what goes to a person.", "cmd": ["uv", "run", "-q", "python", "-m", "tests.after"]},
    {"id": "menu", "title": "Menu to order", "what": "Recipe quantities, pack sizes, dietary rules per dish, a shortage solved by what the ingredient does, a photo that is not a menu.", "cmd": ["uv", "run", "-q", "python", "-m", "tests.menu"]},
    {"id": "seller", "title": "Valio Aimo's own agent", "what": "Proposals are checked against the buyer's limits before they leave; a declined one is never made again.", "cmd": ["uv", "run", "-q", "python", "-m", "tests.seller"]},
    {"id": "rush", "title": "Fair split under shortage", "what": "Many agents, one short product: split by contract share, care kitchens first, never promised twice.", "cmd": ["uv", "run", "-q", "python", "-m", "tests.rush"]},
    {"id": "attack", "title": "Attacks from outside", "what": "Forged tokens, replayed offers, other accounts' orders, oversized input, spoofed addresses, against a fresh server.", "cmd": ["uv", "run", "-q", "python", "-m", "tests.attack"], "server": {"port": 8665, "host": "127.0.0.1"}},
    {"id": "passkey", "title": "Approval signed on a person's device", "what": "Enrol a device, sign a decision with it, verify it in the browser, and the ways around it that must fail. Uses a software passkey in a test browser.", "cmd": ["node", "tests/passkey.mjs"],
     "server": {"port": 8673, "host": "localhost"}, "env": {"BASE": "http://localhost:8673"}},
]


def counts(text: str, code: int) -> tuple[int, int]:
    """(passed, failed) as the suite itself reported them."""
    m = re.search(r"ATTACK CHECKS: (\d+); PASS: (\d+); FAIL: (\d+)", text)
    if m:
        return int(m.group(2)), int(m.group(3))
    m = re.search(r"ALL (\d+) CHECKS PASS", text)
    if m:
        return int(m.group(1)), 0
    m = re.search(r"(\d+) of (\d+) CHECKS FAIL", text)
    if m:
        return int(m.group(2)) - int(m.group(1)), int(m.group(1))
    ok = len(re.findall(r"(?m)^(?:ok\b|PASS\b)", text))
    bad = len(re.findall(r"(?m)^FAIL\b", text))
    return ok, bad or (1 if code else 0)


def wait_port(host: str, port: int, secs: float = 60) -> bool:
    end = time.time() + secs
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def run(suite: dict) -> dict:
    data = tempfile.mkdtemp(prefix=f"aimo-ev-{suite['id']}-")
    env = {**os.environ, "AIMO_DATA": data, "OPERATOR_KEY": "test", **suite.get("env", {})}
    server = None
    try:
        if suite.get("server"):
            s = suite["server"]
            server = subprocess.Popen(["uv", "run", "-q", "uvicorn", "gateway.app:app", "--host", "127.0.0.1", "--port", str(s["port"])], cwd=ROOT,
                                      env={**env, "PORT": str(s["port"]), "PUBLIC_URL": f"http://{s['host']}:{s['port']}"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            if not wait_port("127.0.0.1", s["port"]):
                return {**{k: suite[k] for k in ("id", "title", "what")}, "passed": 0, "failed": 1, "exit": -1, "seconds": 0, "command": " ".join(suite["cmd"]), "note": "its test server did not start"}
        t0 = time.time()
        p = subprocess.run(suite["cmd"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=1500)
        text = p.stdout + "\n" + p.stderr
        ok, bad = counts(text, p.returncode)
        if not ok and not bad and not p.returncode and suite["cmd"][-2:-1] == ["-m"]:
            # A plain script of assert statements that ran to its end: every assert in it held.
            src = (ROOT / (suite["cmd"][-1].replace(".", "/") + ".py")).read_text(encoding="utf-8")
            ok = len(re.findall(r"(?m)^\s*assert\b", src))
        out = {**{k: suite[k] for k in ("id", "title", "what")}, "passed": ok, "failed": bad, "exit": p.returncode, "seconds": round(time.time() - t0, 1), "command": " ".join(suite["cmd"])}
        if p.returncode or bad:
            out["failures"] = [l.strip()[:300] for l in text.splitlines() if l.startswith("FAIL") or "Error" in l][:8]
        return out
    finally:
        if server:
            server.terminate()
            try:
                server.wait(10)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(data, ignore_errors=True)


def main():
    want = set(sys.argv[1:])
    prev = {}
    if want and OUT.exists():  # a partial run keeps the other suites' last results
        prev = {s["id"]: s for s in json.loads(OUT.read_text(encoding="utf-8")).get("suites", [])}
    rows = []
    for suite in SUITES:
        if want and suite["id"] not in want:
            if suite["id"] in prev:
                rows.append(prev[suite["id"]])
            continue
        r = run(suite)
        r["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(r)
        print(f"{r['id']:9} passed {r['passed']:4}  failed {r['failed']:3}  exit {r['exit']}  {r['seconds']} s")
    doc = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "suites": rows, "checks": sum(r["passed"] + r["failed"] for r in rows), "passed": sum(r["passed"] for r in rows),
           "failed": sum(r["failed"] for r in rows), "rerun": "uv run -q python -m agent.evidence"}
    OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"{doc['passed']} of {doc['checks']} checks pass in {len(rows)} suites -> {OUT.relative_to(ROOT)}")
    return 1 if doc["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
