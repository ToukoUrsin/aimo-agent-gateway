# Aimo Agent Gateway

Junction 2026, Valio Aimo challenge "Trusted Trade Agents".

Customers' AI agents are about to start buying. Whoever builds the door they come through owns the customer. This is that door, built for Valio Aimo: one open interface (MCP and REST) where any agent on any model can look up products, get a signed offer and order. Inside limits both companies set it needs nobody. Outside them a second person approves on their own device, and the device signs the yes. The rules run in code, never in a model, and every order can be checked without trusting this server.

Live, no sign-up: https://trusted-trade-agents.fly.dev

It runs on the challenge's mock price and availability API and product data. It is not a Valio Aimo system.

## What to look at

| Page | What it shows |
|---|---|
| `/chat` | The order drawn inside the chat. The connector answers every tool call with a card (MCP Apps, `ui://valio-aimo/card.html`): the options, the signed offer with each rule, the stop for a second person with a QR, the order with its signatures checked inside the card, the Peppol order, the payment terms. The page shows the plain text any connector returns next to the card, in a small chat host of our own with scripted steps. In Claude the assistant picks the steps. |
| `/menu` | A phone photo of a menu becomes one checked order. Gemini reads the photo. Code works out recipe quantities, pack sizes, allergens, stock and price, and solves a shortage by what the ingredient does in the dish. Dietary notes are applied dish by dish. A dish that cannot be made safe is held with a question. A product with no allergen declaration goes to a person, who ticks each such line on its own. |
| `/byhand` | The same order typed by hand in a bare web shop, timed. |
| `/seller` | Valio Aimo's own agent. It proposes what a kitchen missed (a drifted standing order, short-dated stock, a shortage ahead) as signed offers under the same rules. |
| `/tower` | Valio Aimo's control room. The Monday rush: 60 kitchens' agents ask for the same short milk at once and the stock is split by contract share. A visitor can start a small rush live. Valio Aimo's switches are here: agent ordering on or off per customer, a cap per agent order, a ceiling per account and delivery day that no approval lifts. |
| `/demo` | Be the buyer. A real model orders on a sandbox account made for you. "Milk" gets milk and never coffee milk, 600 eggs are 600 eggs, and "we need some milk" gets a proposal from what this kitchen usually orders. The person who asked cannot approve: a second person does, on their own device (a passkey, or a key the browser holds). |
| `/proof` | Four tests counted in code from the signed records: 38 requests to five models, 60 menus to 60 orders, the Monday rush, and the test suites. Includes the bug the first run found in our own allergen rule, and what the product data does not say about allergens. |
| `/ledger` | Every step, hash-chained. Each reader sees their own entries and can verify the rest. |
| `/spec` | How it works, every rule, and what is real here and what is not. |

Your own assistant: add `https://trusted-trade-agents.fly.dev/mcp` to Claude as a custom connector (Customize, Connectors, Add custom connector). Signing in opens a consent page where you set what the assistant may order alone. Claude draws the cards on the web, in the desktop app and on the phone. ChatGPT reads the same cards by the same standard; we have not run it there.

## Check it without trusting us

Two small programs in `verify/`. They import nothing from the gateway.

```
uv run verify/verify_order.py "<order link>"            # checks every signature on one order; Valio Aimo's key is pinned in this repo
uv run verify/verify_order.py "<order link>" --tamper   # lowers one price by a cent first: the checks fail
uv run verify/own_key.py https://trusted-trade-agents.fly.dev
```

`own_key.py` is the buyer holding its own key. It makes a company key on your machine, registers only the public half, signs an agent's mandate with a 400 euro limit, and orders. The gateway lets 100 litres through, sends 400 litres to a person, and refuses a mandate signed by any other key. The order's record says whose key signed the mandate.

## What is real

- Real: live calls to the challenge API for every offer, the challenge's product data, the signatures (ES256 over canonical JSON), the approver's device key (a WebAuthn passkey, or a key the browser holds and cannot export), a buyer's own signing key when it registers one, the rules, the ledger, the agents (Claude, GPT, Gemini and open-weights Gemma models making their own tool calls), Gemini reading the menu photo, the Peppol order (it passes the UBL 2.1 schema and OpenPeppol's own rules for an Order in `tests/peppol.py`). Every number on the pages comes from a recorded run and links to it.
- Stand-in: the buyers, their order histories and standing orders are invented. On the demo accounts the buyer's mandate key is held by the gateway so that a visitor can order without setting anything up; `verify/own_key.py` shows the other way. What each product is ("milk", "coffee milk", "egg") was written once by a model from the product names, because the challenge data has category numbers and no names; Valio Aimo's own product hierarchy would replace it. The 27 recipe cards are ours. The sample menu photos were made with an image model and are labelled as samples. Best-before dates are assigned by the gateway because the challenge data has none. Stock holds live in the gateway, not in an ERP.
- Estimated: the by-hand time for the menu order (45 seconds a line, our assumption) until someone finishes `/byhand`. The page says which it is.
- Not done: the Peppol order has not been sent through an access point. Orders stop at the gateway; nothing reaches an ERP. There is no payment step by design: orders go on the customer's existing invoice terms.

## Measured

| What | Result | Where it comes from |
|---|---|---|
| Menu photo to checked order | 41 lines for 180 guests, about 8 seconds to read the photo and under 1 second to plan, model cost under one cent | `GET /api/menu/latest` |
| Menu test | 60 menus to 60 signed orders, 1,168 lines, 6,843 checks, 0 failed. 52 of them carry dietary notes. 6 menus that must go back to a person with a question all did. Each sample photo read 10 times: guests right 10 of 10, a photo that is not a menu refused 10 of 10 | `evals/menu-latest.json`, `evals/menu-runs/*.jsonl` |
| Product data | 4,233 of 13,546 food products (31%) have no allergen declaration; 290 of them name an allergen in their title. Such a line goes to a person | `GET /api/proof` (`undeclared`) |
| Monday rush | 60 agents, 1,472 L asked, 698 L in stock, 698 L ordered, never over-allocated, 9 needed a person, 42 s | `GET /api/rush/latest`, `evals/rush-latest.json` |
| Model test | 38 requests to each of 5 models from 3 providers: 190 runs, 142 orders, 0 broke a rule. Need met, of 38: Opus 38, Gemini 38, Haiku 37, GPT 36, open-weights Gemma 32. All 142 orders are kept with their signatures and pass `verify/verify_order.py` | `GET /api/proof`, `evals/latest.json`, `evals/runs/*.jsonl`, `evals/orders/*.jsonl` |
| Attacks from outside | 100 checks, 5 holes found and fixed | `tests/attack.py` |
| Test suites | 331 checks in 8 suites, 0 failed | `evals/tests-latest.json` |

## Run it

```
./run.sh            # http://127.0.0.1:8642
```

Needs Python 3.14 with `uv`, the challenge's three data files in `portal/` (`valio_aimo_junction26_product_data.json`, `..._delivery_reliability.csv`, `..._carbon_footprint.csv`, from the challenge portal; they are Valio Aimo's and not in this repo), and a `.env` with `AIMO_API_KEY` and `AIMO_BASE` (the challenge API), `PUBLIC_URL` and `OPERATOR_KEY`. `PUBLIC_URL` goes into signed links and mandates. Model keys come from the environment: `AGENT_API_KEY` (Claude and GPT through an Anthropic- and OpenAI-compatible endpoint at `AGENT_BASE_URL`) and `AGENT_GEMINI_API_KEY` (Gemini and Gemma). Without model keys the gateway, the script bot and the tests still work.

Valio Aimo's side of the demo: open `/op?key=<OPERATOR_KEY>` once, then `/tower`.

## Tests

The browser test and the deck use Playwright from the film project: `cd film && npm install` once. The suites reset their data, so give them their own folder (`AIMO_DATA=/tmp/aimo-test uv run ...`) when a gateway is running from this checkout; `agent.evidence` does that by itself.

```
uv run -q python -m tests.flow       # one order end to end
uv run -q python -m tests.need       # milk gets milk, 600 eggs are 600 eggs, "chicken" asks which
uv run -q python -m tests.after      # short pick, delay, recall
uv run -q python -m tests.controls   # what an agent can see, rate limits, ledger
uv run -q python -m tests.menu       # menu to order
uv run -q python -m tests.seller     # Valio Aimo's own agent
uv run -q python -m tests.rush       # fair allocation
uv run -q python -m tests.peppol     # the order as Peppol XML, against the UBL schema and OpenPeppol's rules
uv run -q python -m tests.attack     # 100 attacks, against a fresh server on port 8665
BASE=http://localhost:8673 node tests/passkey.mjs   # the approver's device, in a real browser with a virtual authenticator
uv run -q python -m agent.evidence   # all of the above, each in its own process, counted into evals/tests-latest.json
uv run -q python -m agent.menu_evals --cases 60 --photos 10       # the menu test behind /proof
uv run -q python -m agent.evals --base http://127.0.0.1:8642      # the model test behind /proof
uv run -q python -m agent.floor --rush --base http://127.0.0.1:8642 --key <OPERATOR_KEY>   # the Monday rush
```

## Layout

- `gateway/` the gateway. `service.py` (offers, orders, sourcing), `policy.py` (the rules), `catalog.py` and `kinds.json` (what each product is), `menu.py` and `recipes.json` (menu to order), `seller.py` (Valio Aimo's agent), `allocation.py` (fair split), `after.py` (problems after the order), `passkey.py` (the approver's device), `proof.py` (what `/proof` shows), `signing.py`, `store.py` (SQLite and the ledger), `anchor.py` (ledger head on Solana devnet), `oauth.py`, `mcp_tools.py`, `peppol.py`, `aimo.py` (calls to the challenge API), `app.py` (REST and pages)
- `agent/` the buyer side. `buyer.py` (a model over MCP), `llm.py` (one harness for Claude, GPT, Gemini, Gemma), `script.py` (a bot with no model), `rogue.py` (the misbehaving bot), `floor.py` (many kitchens at once, the rush), `evals.py` (the model test), `menu_evals.py` (the menu test), `evidence.py` (runs every suite), `kinds.py` (names what each product is, run once)
- `web/` the pages, one stylesheet, no build step
- `verify/` check an order, or hold the buyer's key yourself, without the gateway's code
- `tests/`, `evals/` (recorded runs), `film/` (the demo film project), `deploy/`

## How it was built

Built during Junction 2026 (13 to 15 November 2026) by team Northbound Dispatch, with AI coding agents (Claude Code and Codex) writing most of the code under our direction. Open standards used: MCP, Universal Commerce Protocol, AP2, OAuth 2.1, JOSE, GS1, Peppol BIS Ordering 3.

MIT licence, see `LICENSE`.
