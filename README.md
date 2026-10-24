<div align="center">

# Relay

**A work desk that lives in your team chat, run by an AI agent that speaks the Switch protocol natively.**

Requests arrive in a channel. An agent triages them. Every accepted case gets a room
of its own, a worker agent, a human review gate, and a clock that pages whoever is
on duty when a promise is about to break.

`Flip the Switch` · Track 01, Best Business Workflow

</div>

---

## The problem

Every team has the channel. `#help`, `#requests`, `#ops`. Requests arrive half-formed,
nobody owns them, they scroll away, and no one can tell you what is open or who is
waiting.

Dropping an AI bot in the channel does not fix this, because answers were never the
missing piece. **Ownership and handoff** were. A bot that answers questions brilliantly
still leaves you with no idea which of last Tuesday's requests died.

## What Relay does

Relay turns that channel into a desk with a pipeline you can see.

```
#relay-desk  ─────────────────────────────────────────────────────────────

  sam        the export job has been dropping rows since this morning

  intake     @relay open export job silently dropping rows for EU tenants
  relay      RLY-14 — export job silently dropping rows for EU tenants
             triaging · P2 · acknowledge within 30m, resolve within 480m

  priya      @relay accept RLY-14 p1
  relay      RLY-14 accepted · P1 · resolve within 60m
             Room case RLY-14 is open and linked here.
             I will page @duty if it goes quiet.

                                 ┌─ case RLY-14 ──────────────────────────
                                 │  worker  reproduced it — the tenant
                                 │          filter drops NULL regions.
                                 │          @relay review RLY-14
                                 │  relay   @duty RLY-14 is ready for review
                                 │  priya   @relay done RLY-14 patched the
                                 │          filter, regression test added
                                 └────────────────────────────────────────

  relay      RLY-14 resolved by priya — patched the filter, regression test added
             41m from open to close, within SLA.
```

Nothing in that transcript is a special command surface. It is a chat channel, and
every participant — person or agent — drives the desk the same way.

## The idea: split judgement from bookkeeping

Relay is two halves, and the split is the whole point.

| | Who | Why |
|---|---|---|
| **Judgement** | Claude Code / Codex agents, run by Switch Console | Reading a half-formed request, deciding what it is, reproducing it, writing the fix. Models are good at this. |
| **Bookkeeping** | The **conductor** — plain Python, no model | Which cases are open, who owns each, how long it has been sitting, who to page. |

An LLM asked to keep a ledger will do it well most of the time. A desk that loses one
case in twenty is a desk nobody trusts. So the conductor has no model behind it at all:
it is a state machine, a clock, and a set of Switch operations.

What makes it a **teammate** rather than a cron job is that it holds its own `always_on`
connection to the Switch agent bridge and is addressed in the room like anyone else.

## Why this stops working without Switch

The judging rubric asks whether the project would still work if Switch were removed. It
would not, and not incidentally — Relay is built out of Switch primitives:

- **The agent protocol.** The conductor is not a Switch Console session. It registers
  itself with a full integration profile declaring `connection_model: always_on`, opens
  one SSE stream, holds a 2-second heartbeat, and calls operations over HTTP. That is
  [`conductor/relay/switch.py`](conductor/relay/switch.py), written against the published
  wire contract with no SDK in between.
- **A room per case.** On acceptance the conductor calls `create_room_from_yaml` with
  [its own template](templates/relay-case.room.yaml) — the request lands in the new room's
  instructions, the worker agent is already in it, and the review gate exists as a role.
- **Linked rooms.** `link_rooms` points the desk at each case room under the case id, so
  anyone in the channel can follow the work without being told where it went.
- **Roles, not names.** Escalation uses `send_targeted_message(target_roles=["duty"])`,
  which fans out to whoever holds the role right now. The workflow outlives the rota.
- **Room documents.** On close, the full trail is written into the case room with
  `create_room_document`, so what was decided stays where it was decided.
- **The room as the audit log.** Every transition was a message someone sent. There is no
  second system to go and look in — which is also why the board has no buttons.

## What is in here

```
templates/
  relay-desk.template.yaml   Console runs this: two agents, the desk room, the kickoff
  relay-case.room.yaml       the conductor runs this itself, once per accepted case
conductor/
  relay/switch.py            the agent protocol — SSE down, HTTP up
  relay/cases.py             the state machine, the SLA clocks
  relay/commands.py          the grammar people and agents type
  relay/conductor.py         events in, Switch operations out
  relay/api.py               a read-only HTTP face, and an SSE stream for the board
  demo.py                    the board against seeded data, no Switch needed
  tests/test_workflow.py     the state machine and the grammar
  tests/test_protocol.py     the protocol client, against a mock agent bridge
board/
  index.html board.css board.js    the live board — no build step, no framework
```

---

## See the board in 30 seconds

No Switch, no chat app, no API keys. This seeds the real case store with a real walk
through the real state machine and serves the real board over it.

```bash
pip install httpx fastapi uvicorn pydantic pydantic-settings
python conductor/demo.py
# -> http://localhost:8787/
```

<div align="center">
<em>Six open cases, two over SLA, one waiting on the requester with its clock paused.</em>
</div>

Run the tests the same way. 35 of them, no network, no Switch:

```bash
pip install -r requirements.txt
python -m pytest conductor/tests -q
```

`test_workflow.py` covers the state machine and the grammar. `test_protocol.py` stands
up a mock agent bridge and holds the client to the wire contract: that `connection_state`
is read rather than assumed, that the heartbeat starts only once a stream is attached and
carries the cursor, that an `evicted` frame reopens and resumes with `Last-Event-ID`, and
that operations carry the connection id header.

---

## Run it for real

Everything below is free. No paid plan on any platform.

### 1. Switch

Install [Switch Console](https://github.com/sandbox-quantum/switch#get-started-in-two-steps)
for your platform and open it. It starts a local `switch-core` in Docker and walks you
through connecting a chat app.

**Pick Discord** if you have a choice: you can create your own server with full admin
rights in under a minute, and a bot token is free. Telegram is equally free via
`@BotFather`. Slack works but needs someone who can install an app into a workspace.

### 2. The desk

In Console: **Templates → Create from template**, and give it
[`templates/relay-desk.template.yaml`](templates/relay-desk.template.yaml).

It creates `relay-intake` and `relay-worker` on whichever provider you have installed,
and the `relay-desk` room, bridged into your chat app.

### 3. The conductor

```bash
cp .env.example .env
```

Put two things in it:

- `RELAY_SWITCH_BASE_URL` — the **agent bridge**, `http://localhost:8000` by default.
  This is usually *not* the Gateway URL. `curl http://localhost:8000/health` should
  answer; a 401 means right host, wrong path.
- `RELAY_REGISTRATION_TOKEN` — `AGENT_REGISTRATION_TOKEN` from your Switch server's
  `.env`. It is used once and never read again.

Then:

```bash
pip install httpx fastapi uvicorn pydantic pydantic-settings
python -m relay            # from inside conductor/, or: cd conductor && python -m relay
```

On first start it registers itself, stores its credentials in `.relay/`, and prints the
board's address. **The API key is returned exactly once**, which is why it is persisted
rather than re-requested.

### 4. Let Relay into the desk

The conductor is not an agent Console created, so Console does not add it to rooms for
you. In Console, add `relay` to the `relay-desk` room, then restart the conductor — it
finds the room by name and defines the `duty` role in it.

### 5. Take the duty role

In the desk channel, have a person assume `duty`. That is the human gate: cases reach
them at `awaiting_review`, pages go to them, and nothing closes without them.

---

## The grammar

Anyone in the room — person or agent — drives the desk with these.

| | |
|---|---|
| `@relay open <what you need>` | file a request; Relay answers with its case id |
| `@relay accept <id> [p1\|p2\|p3]` | take it on — opens a case room and links it to the desk |
| `@relay info <id> <question>` | hand it back to the requester; **the clock pauses** |
| `@relay claim <id>` | put your name on it |
| `@relay start <id>` | work has begun |
| `@relay note <id> <text>` | add to the trail without moving the case |
| `@relay review <id>` | hand to whoever holds `duty` |
| `@relay changes <id> <why>` | send it back to the worker |
| `@relay done <id> <outcome>` | close it; the outcome goes back to the requester's thread |
| `@relay reject <id> <why>` | close it without doing it |
| `@relay priority <id> p1` | re-rank it, which re-sets its promises |
| `@relay status [id]` | the pipeline, or one case |
| `@relay help` | this table, in the room |

Relay returns `None` for anything it cannot read as a command. It sits in a room where
people are also talking to each other, and acting on an ambiguous sentence is worse than
waiting to be asked plainly.

## The workflow

```
            ┌──────────────┐
  open ───► │   triaging   │ ─── reject ──┐
            └──────┬───────┘              │
                   │                      │
         info ◄────┼────► accept          │
            ┌──────▼───────┐              │
            │  needs_info  │  clock paused│
            └──────┬───────┘              │
                   │ accept               │
            ┌──────▼───────┐              │
            │   accepted   │ ◄── a room of its own, linked to the desk
            └──────┬───────┘              │
                   │ start                │
            ┌──────▼───────┐              │
            │ in_progress  │ ◄──┐         │
            └──────┬───────┘    │ changes │
                   │ review     │         │
            ┌──────▼───────┐    │         │
            │await_review  │ ───┘         │
            └──────┬───────┘              │
                   │ done                 ▼
            ┌──────▼───────┐       ┌────────────┐
            │     done     │       │  rejected  │
            └──────────────┘       └────────────┘
```

Transitions not on this diagram are refused, loudly, in the room. An accidental
`@relay review` on a brand-new case fails rather than skipping the work.

### The clocks

| Priority | Acknowledge within | Resolve within |
|---|---|---|
| P1 | 5m | 1h |
| P2 | 30m | 8h |
| P3 | 4h | 48h |

Three things get a page, each exactly once per case until it moves again:

- **Unacknowledged** — still in triage past its ack promise.
- **Over SLA** — past its resolve promise, in any open state.
- **Stalled** — no transition in `RELAY_STALL_MINUTES`, which defaults to 20.

Time in `needs_info` counts against none of them. A desk judged on time it spent waiting
for an answer would quickly learn to stop asking questions.

## The board

`http://localhost:8787/` — the pipeline, SLA meters, and the full trail per case, pushed
over SSE whenever anything moves.

It is deliberately **read-only**. Every way of moving a case goes through the room,
because that is where the other participants are and where the audit trail lives. A
button on the board would be a decision taken somewhere nobody else can see.

---

## Notes for anyone reading the code

A few things in [`switch.py`](conductor/relay/switch.py) are easy to get wrong, and the
comments there say why each is the way it is:

- The **client** generates the connection id and reuses it across reopens. That is what
  makes a dropped socket a resume rather than a new connection. Two processes sharing one
  id would evict each other on every reconnect.
- A **404 or 409 on the heartbeat means reopen, not retry.** Retrying keeps a dead
  connection dead. The beat is also refused when no stream is attached, so it is owned by
  the stream and cancelled with it.
- **Rooms are declared on the open URL, never after.** Catch-up runs immediately on open,
  so a room subscribed a moment later has its buffered events skipped and the cursor moved
  past them — losing exactly what a resume exists to recover.
- **Every heartbeat must carry the stream's `generation`.** From protocol 6 the server
  stamps each stream with an incarnation of the connection id and refuses any tick that
  does not echo it — a displaced client still holds the id and the API key, so the id
  alone proves nothing. This is not in the published agent-protocol page, which documents
  the pre-fencing beat body. A client that omits it looks perfectly healthy — the stream
  opens, `connection_state` arrives, operations work — and then the connection is swept
  for a lapsed heartbeat every six seconds, forever. The server's error message is
  excellent; the logs are where you find it.
- A **`gap` is not a wake.** It is held and attached to the next event surfaced.
- `filter=addressed` would be the tempting setting for a desk and is the wrong one: it
  never delivers `command` events, and a desk wants the room's whole context.

## Licence

MIT, for everything in this repository. Switch itself is Apache 2.0 with the Commons
Clause, and is not vendored here.
