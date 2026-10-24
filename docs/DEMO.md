# The two-minute demo

Written for whoever records the submission video. The judging rubric gives 20% to
presentation and asks one thing of the video: make it obvious how Switch is used.

The temptation is to explain the architecture. Don't. Show a request arriving from a
person and leaving as finished work, and let the rooms do the explaining.

## Before you hit record

- [ ] Switch Console running, Discord connected, `relay-desk` bridged to a channel.
- [ ] `relay-intake` and `relay-worker` onboarded, both answering.
- [ ] The conductor running, and `relay` added to the desk room.
- [ ] Someone holding the `duty` role — say it out loud on camera, it is the human gate.
- [ ] `RELAY_STALL_MINUTES=2` in `.env`, so the nudge fires inside the demo.
- [ ] The board at `http://localhost:8787/` open on a second screen, in light mode.
- [ ] Two Discord accounts if you can: one to ask, one to approve. A desk with one
      person in it does not look like a desk.

## The shot list

**0:00 — the channel, not the tool.** Open on the Discord channel with people and
agents in the member list. One line: *"This is an ordinary Discord channel. Three of
these members are AI agents."*

**0:10 — a request, in human words.** Type as the requester, not as an operator:

> the nightly export has been dropping rows for our EU tenants since this morning

Say nothing. Let `relay-intake` read it and file it. `relay` answers with `RLY-1`, its
priority and its two promises.

**0:30 — a person decides.** As the second account:

> `@relay accept RLY-1 p1`

Cut to the board: the case moves to `accepted`, the clock switches to the P1 budget.
Then back to Discord, where a **new channel has appeared** — `case RLY-1` — with the
worker agent already in it and the request in its topic.

> *"Switch created that channel. The request, the agent and the review gate came with
> it, and it is linked back to the desk."*

This is the moment that wins the "would it work without Switch" question. Spend a beat
on it.

**0:50 — the agents work.** In the case channel the worker says what it is doing and
reports back. Don't narrate over it; let it run. If it is slow, cut.

**1:15 — the gate.** The worker posts `@relay review RLY-1`. Relay addresses the `duty`
role, and the person holding it gets the ping *by role, not by name*. Worth one line:

> *"Relay doesn't know who's on duty this week. It asks the role."*

Approve it:

> `@relay done RLY-1 patched the tenant filter, regression test added`

**1:35 — the record.** Relay posts the outcome back into the requester's original
thread in the desk channel, and writes the full trail into the case channel as a
document. Open the document on camera. One line:

> *"That record lives in the room that produced it. Nothing to file anywhere else."*

**1:50 — the board.** End on it. Pipeline, SLA meters, one case over its promise.

> *"Read-only on purpose. Every case here moved because someone typed in chat, so the
> whole trail is in the channel where the team already is."*

## If you have thirty seconds more

Show the nudge. Leave a case untouched past `RELAY_STALL_MINUTES` and let Relay page
the duty role unprompted. It is the clearest demonstration that the conductor is a
participant rather than a command handler — nobody asked it to do that.

## Things worth saying once, and not twice

- The conductor is **not** a Claude Code session. It registered itself as `always_on`,
  holds its own SSE stream and 2-second heartbeat, and speaks the agent protocol
  directly. One sentence, over the board.
- Judgement is the agents', bookkeeping is the conductor's, and the split is deliberate.
- Everything shown is free: Discord server, Discord bot, self-hosted Switch.

## What not to do

- Don't demo the `demo.py` seeded board as if it were live. It is for people who have
  not set Switch up; using it in the video invites exactly the wrong question.
- Don't read the grammar table out. Showing three commands in context teaches it better.
- Don't apologise for the agents taking time. Cut the dead air instead.
