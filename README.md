# Aimo Agent Gateway

Junction 2026, Valio Aimo challenge "Trusted Trade Agents".

Customers' AI agents are about to start buying. Whoever builds the door they come through owns the customer. This is that door, built for Valio Aimo: one open interface (MCP and REST) where any agent on any model can look up products, get a signed offer and order. Inside limits both companies set it needs nobody. Outside them one person approves on their own phone, and the phone signs the yes. The rules run in code, never in a model.

Live, no sign-up: https://trusted-trade-agents.fly.dev

It runs on the challenge's mock price and availability API and product data. It is not a Valio Aimo system.

## What to look at

| Page | What it shows |
|---|---|
| `/menu` | A phone photo of a menu becomes one checked order. Gemini reads the photo. Code works out recipe quantities, pack sizes, allergens, stock and price, and solves a shortage by what the ingredient does in the dish. Dietary notes are applied dish by dish; a product with no allergen declaration goes to a person, never through on silence. |
| `/byhand` | The same order typed by hand in a bare web shop, timed. |
| `/seller` | Valio Aimo's own agent. It proposes what a kitchen missed (a drifted standing order, short-dated stock, a shortage ahead) as signed offers under the same rules. |
| `/tower` | Valio Aimo's control room. The Monday rush: 60 kitchens' agents ask for the same short milk at once and the stock is split by contract share. |
| `/demo` | Be the buyer. A real model orders on a sandbox account made for you. Enrol your phone as the approver's device and approvals are signed on it with a passkey. |
| `/proof` | Four tests counted in code from the signed records: 26 requests to five models, 60 menus to 60 orders, the Monday rush, and the test suites. Includes the bug the first run found in our own allergen rule, and what the product data does not say about allergens. |
| `/ledger` | Every step, hash-chained. Each reader sees their own entries and can verify the rest. |
| `/spec` | How it works, every rule, and what is real here and what is not. |

Your own assistant: add `https://trusted-trade-agents.fly.dev/mcp` to Claude or ChatGPT as a connector.

## What is real

- Real: live calls to the challenge API for every offer, the challenge's product data, the signatures (ES256 over canonical JSON), the approver's passkey (WebAuthn, the key stays in the person's device), the rules, the ledger, the agents (Claude, GPT, Gemini and open-weights Gemma models making their own tool calls), Gemini reading the menu photo. Every number on the pages comes from a recorded run on the server and links to it.
- Stand-in: the buyers, their order histories and standing orders are invented. The buyer's mandate key is held by the gateway; in production the buyer's own sign-in holds it. The 27 recipe cards are ours. The sample menu photos were made with an image model and are labelled as samples. Best-before dates are assigned by the gateway because the challenge data has none. Stock holds live in the gateway, not in an ERP.
- Estimated: the by-hand time for the menu order (45 seconds a line, our assumption) until someone finishes `/byhand`. The page says which it is.
- Not done: the Peppol XML has not been through the official validator. There is no payment step by design: orders go on the customer's existing invoice terms.

## Measured

| What | Result | Where it comes from |
|---|---|---|
| Menu photo to checked order | 41 lines for 180 guests, about 8 seconds to read the photo and under 1 second to plan, model cost under one cent | `GET /api/menu/latest` |
| Menu test | 60 menus to 60 signed orders, 1,334 lines, 4,124 checks, 0 failed. 6 menus that must go to a person all did. Each sample photo read 10 times: same dishes and guests 10 of 10, a photo that is not a menu refused 10 of 10 | `evals/menu-latest.json`, `evals/menu-runs/*.jsonl` |
| Product data | 4,233 of 13,546 food products (31%) have no allergen declaration; 290 of them name an allergen in their title. Such a line goes to a person | `GET /api/proof` (`undeclared`) |
| Monday rush | 60 agents, 1,472 L asked, 698 L in stock, 698 L ordered, never over-allocated, 9 needed a person, 42 s | `GET /api/rush/latest`, `evals/rush-latest.json` |
| Model test | 26 requests to each of 5 models from 3 providers: 130 runs, 99 orders, 0 broke a rule. Need met: Opus 26, Haiku 26, GPT 25, Gemini 26, open-weights Gemma 17 | `GET /api/proof`, `evals/latest.json`, `evals/runs/*.jsonl` |
| Attacks from outside | 100 checks, 5 holes found and fixed | `tests/attack.py` |
| Test suites | 331 checks in 8 suites, 0 failed | `evals/tests-latest.json` |

## Run it

```
./run.sh            # http://127.0.0.1:8642
```

Needs Python 3.14 with `uv`, the challenge's three data files in `portal/` (`valio_aimo_junction26_product_data.json`, `..._delivery_reliability.csv`, `..._carbon_footprint.csv`, from the challenge portal; they are Valio Aimo's and not in this repo), and a `.env` with `AIMO_API_KEY` and `AIMO_BASE` (the challenge API), `PUBLIC_URL` and `OPERATOR_KEY`. `PUBLIC_URL` goes into signed links and mandates. Model keys come from the environment: `AGENT_API_KEY` (Claude and GPT through an Anthropic- and OpenAI-compatible endpoint at `AGENT_BASE_URL`) and `AGENT_GEMINI_API_KEY` (Gemini and Gemma). Without model keys the gateway, the script bot and the tests still work.

Valio Aimo's side of the demo: open `/op?key=<OPERATOR_KEY>` once, then `/tower`.

## Tests

The browser test and the deck use Playwright from the film project: `cd film && npm install` once.

```
uv run -q python -m tests.flow       # one order end to end
uv run -q python -m tests.after      # short pick, delay, recall
uv run -q python -m tests.controls   # what an agent can see, rate limits, ledger
uv run -q python -m tests.menu       # menu to order
uv run -q python -m tests.seller     # Valio Aimo's own agent
uv run -q python -m tests.rush       # fair allocation
uv run -q python -m tests.attack     # 100 attacks, against a fresh server on port 8665
BASE=http://localhost:8673 node tests/passkey.mjs   # the approver's device, in a real browser with a virtual authenticator
uv run -q python -m agent.evidence   # all of the above, each in its own process, counted into evals/tests-latest.json
uv run -q python -m agent.menu_evals --cases 60 --photos 10       # the menu test behind /proof
uv run -q python -m agent.evals --base http://127.0.0.1:8642      # the model test behind /proof
uv run -q python -m agent.floor --rush --base http://127.0.0.1:8642 --key <OPERATOR_KEY>   # the Monday rush
```

## Layout

- `gateway/` the gateway. `service.py` (offers, orders, sourcing), `policy.py` (the rules), `menu.py` and `recipes.json` (menu to order), `seller.py` (Valio Aimo's agent), `allocation.py` (fair split), `after.py` (problems after the order), `passkey.py` (the approver's device), `proof.py` (what `/proof` shows), `signing.py`, `store.py` (SQLite and the ledger), `anchor.py` (ledger head on Solana devnet), `oauth.py`, `mcp_tools.py`, `peppol.py`, `aimo.py` (calls to the challenge API), `app.py` (REST and pages)
- `agent/` the buyer side. `buyer.py` (a model over MCP), `llm.py` (one harness for Claude, GPT, Gemini, Gemma), `script.py` (a bot with no model), `rogue.py` (the misbehaving bot), `floor.py` (many kitchens at once, the rush), `evals.py` (the model test), `menu_evals.py` (the menu test), `evidence.py` (runs every suite)
- `web/` the pages, one stylesheet, no build step
- `tests/`, `evals/` (recorded runs), `film/` (the demo film project), `deploy/`

## How it was built

Built during Junction 2026 (13 to 15 November 2026) by team Northbound Dispatch, with AI coding agents (Claude Code and Codex) writing most of the code under our direction. Open standards used: MCP, Universal Commerce Protocol, AP2, OAuth 2.1, JOSE, GS1, Peppol BIS Ordering 3.

MIT licence, see `LICENSE`.
