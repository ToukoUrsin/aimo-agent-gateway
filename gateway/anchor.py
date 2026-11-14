"""Anchor the audit ledger outside our own database.

Every few minutes the hash at the head of the ledger is written into a memo
transaction on Solana devnet.  Anyone can open the transaction in a public
explorer and compare it with the ledger they were given, so not even the
gateway's operator can rewrite history behind an anchor without it showing.

Only the head hash leaves the gateway: no order, price or customer data.
Runs only where a funded key exists (DATA/keys/solana-devnet.json), so test
instances with their own data folder never anchor.
"""
import base64
import json
import threading
import time

import httpx
from fastapi import APIRouter, Request

from .config import DATA

RPC = "https://api.devnet.solana.com"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
KEY = DATA / "keys" / "solana-devnet.json"
EVERY_S = 180  # how often the head is anchored while the ledger is moving
KEEP = 2000


def _rpc(method, params):
    r = httpx.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20).json()
    if "error" in r:
        raise RuntimeError(r["error"].get("message", "rpc error"))
    return r["result"]


class Anchor:
    def __init__(self, store):
        self.store, self.kp, self.lock, self.error = store, None, threading.Lock(), None
        if KEY.exists():
            try:
                from solders.keypair import Keypair

                self.kp = Keypair.from_bytes(bytes(json.loads(KEY.read_text())))
            except Exception as e:  # a broken key file must never stop the gateway
                self.error = str(e)

    @property
    def address(self):
        return str(self.kp.pubkey()) if self.kp else None

    def head(self):
        r = self.store.q("select seq, hash from events order by seq desc limit 1")
        return {"seq": r[0][0], "hash": r[0][1]} if r else None

    def anchors(self):
        return self.store.get("anchors", [])

    def covering(self, seq):
        """The first anchor written at or after ledger entry `seq`."""
        return next((a for a in self.anchors() if a["seq"] >= seq), None)

    def anchor_now(self):
        """Write the current head hash to devnet. Returns the anchor, or None when there is nothing new."""
        if not self.kp:
            return None
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import Message
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction

        with self.lock:
            head, done = self.head(), self.anchors()
            if not head or (done and done[-1]["seq"] >= head["seq"]):
                return None
            memo = f"aimo-ledger:v1:{head['seq']}:{head['hash']}"
            bh = Hash.from_string(_rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"])
            ix = Instruction(Pubkey.from_string(MEMO_PROGRAM), memo.encode(), [AccountMeta(self.kp.pubkey(), True, True)])
            tx = Transaction([self.kp], Message.new_with_blockhash([ix], self.kp.pubkey(), bh), bh)
            sig = _rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}])
            a = {"seq": head["seq"], "hash": head["hash"], "memo": memo, "signature": sig, "ts": int(time.time()),
                 "explorer": f"https://explorer.solana.com/tx/{sig}?cluster=devnet"}
            self.store.put("anchors", (done + [a])[-KEEP:])
            self.error = None
            return a

    def loop(self):
        while True:
            time.sleep(EVERY_S)
            try:
                self.anchor_now()
            except Exception as e:  # out of devnet SOL, RPC down: say so in /api/anchor, keep serving
                self.error = str(e)

    def status(self):
        done, head = self.anchors(), self.head()
        return {
            "enabled": bool(self.kp), "cluster": "devnet", "rpc": RPC, "address": self.address, "memo_format": "aimo-ledger:v1:<entry number>:<entry hash>",
            "head": head, "anchored_up_to": done[-1]["seq"] if done else 0, "count": len(done), "every_seconds": EVERY_S,
            "latest": done[-1] if done else None, "error": self.error,
        }


def build(g, ctx) -> APIRouter:
    r = APIRouter()
    a = Anchor(g.store)
    g.anchor = a
    if a.kp:
        threading.Thread(target=a.loop, daemon=True, name="ledger-anchor").start()

    @r.get("/api/anchor", tags=["orders"], summary="Where the ledger head is anchored outside this gateway (Solana devnet memo transactions)")
    def anchor_status(recent: int = 20):
        return a.status() | {"anchors": a.anchors()[-max(1, min(recent, 200)):]}

    @r.get("/api/anchor/covering/{seq}", tags=["orders"], summary="The first anchor written at or after this ledger entry")
    def anchor_covering(seq: int):
        return {"anchor": a.covering(seq), "enabled": bool(a.kp), "rpc": RPC}

    @r.post("/api/anchor/now", include_in_schema=False)
    def anchor_now(request: Request):
        if not ctx(request)["op"]:
            return {"ok": False, "error": "operator only"}
        try:
            return {"ok": True, "anchor": a.anchor_now()}
        except Exception as e:
            a.error = str(e)
            return {"ok": False, "error": str(e)}

    return r
