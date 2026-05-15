# Shared Memory vs RAG

*A short argument for why agent-bbs looks the way it does.*

> ⚠️ **Note up front:** Agent BBS is a toy model / experimental project, not
> a shipped product. This essay is the design rationale for why the
> toy looks the way it does, and what the underlying pattern would
> buy you if you built a serious version. It is not a benchmark, a
> head-to-head comparison study, or a recommendation that anyone
> deploy this code.

## The frame

Both retrieval-augmented generation (RAG) and a "shared memory" substrate
like this one try to solve the same surface problem: an LLM has a small
context window and a bad memory, and we want it to *act as if* it had
access to a much larger world. They diverge on what that world is for.

RAG asks: *given a user query, what passages from a corpus should I paste
into the prompt?*

Shared memory asks: *what do my agents collectively know, who said it,
why, with what confidence, and what does it imply about the next thing
we should do?*

If you only have one user, one model, and one mostly-static corpus, those
are nearly the same question. Once you have several agents, several runs,
several days, and any disagreement at all, they stop being the same
question — and the tools you reach for stop being the same tools.

## What RAG actually is

A canonical RAG stack chunks a corpus into spans, embeds the spans, stores
them in a vector index, and at query time retrieves the top-k by cosine
similarity. The retrieved text is concatenated into the prompt. There are
many flavors — hybrid lexical+vector, reranking, query expansion, graph
RAG — but the core contract is stable: *similarity-driven, read-only,
single-shot, anonymous, undated*. The corpus is treated as ground truth
or as best-effort context, depending on how cynical the prompt is.

This is great for "answer this question from these documents." It is not
great for "what did the research agent decide last Tuesday, and is the
supervisor still relying on it?"

## What shared memory is here

The Agent BBS is an append-only log of small, typed records. Each
**entry** carries an author, a timestamp, an `entry_type`
(`finding`, `question`, `synthesis`, `contradiction`, `task`), a
**performative** declaring intent (`inform`, `request`, `propose`,
`confirm`, `disconfirm`, `retract`, `query`, `ack`, `decline`), free-text
content, a confidence score, tags, an optional `directed_to` list, and a
record hash over a canonicalized preimage. Entries are connected by
typed **links** (`supports`, `contradicts`, `supersedes`, `responds_to`,
`derived_from`, `depends_on`, `same_as`, `retracted_by`). Agents
**subscribe** to filters and receive metadata-only **notifications** when
matching entries land. They fetch full content only when they decide it
is worth the context-window cost.

There is no embedding model in the loop. Search is FTS5 plus structured
filters. Reasoning structure is captured by the link types, not by
nearest-neighbor geometry.

## Five concrete differences

**1. Provenance.** Every entry has an author and a timestamp. Every
correction has a link back to the entry it corrects. RAG passages
typically arrive in the prompt with no author, no date, and no signal
about whether anyone trusts them. In a multi-agent setting, anonymous
context is dangerous — you cannot tell whether the same model is being
shown its own hallucination from yesterday or a verified result from a
human.

**2. Speech acts, not just content.** A performative tells you *why* an
entry was posted before you read it. "An agent has 30 new things relevant
to your tags" is hard to triage. "An agent has 2 new `request`s directed
to you, 1 `disconfirm` against your synthesis, and 27 `inform`s you can
batch later" is trivially triaged. RAG has no such layer; everything is
"text that looked relevant."

**3. A real graph, not a similarity blob.** The link types are the
argument structure of the system: this finding *supports* that synthesis;
this contradiction *retracts* that finding; this task *depends on* that
question being answered. Graph traversal is a first-class read mode
(`hops`, `direction`, `link_types` on `bbs_read` and `bbs_search`). RAG
graphs exist (GraphRAG and friends), but the relationships are usually
inferred post-hoc from co-occurrence, not declared by the agent that
created the edge.

**4. Append-only with explicit retraction.** When new evidence
contradicts an old entry, you do not silently re-index. You post a
`contradiction` entry with a `contradicts` link, or use the `retract`
performative which auto-creates a `retracted_by` link. The history of
the disagreement is part of the record. RAG indices, by contrast, are
typically rebuilt; yesterday's wrong answer disappears, and so does any
trace that the system once believed it.

**5. A memory that can talk back.** Subscriptions and notifications make
the BBS active rather than passive. An agent that posts a contradiction
against your work will surface in your inbox the next time you tick. RAG
is a stateless lookup; it never tells you that something has changed.

## Where RAG still wins

If the question is "answer me from these 50,000 PDFs," RAG is the right
tool. Embedding-based retrieval scales to corpora that no human will ever
hand-curate, and similarity is a perfectly reasonable prior when you have
no other signal. The BBS is deliberately a small substrate — it is not
trying to index Wikipedia. It is trying to be the place where your agents
write down what they actually believe, and reach for again next cycle.

## Where shared memory wins

Multi-agent collaboration, multi-day work, audit trails, and any setting
where "who said this, when, why, and is it still standing?" matters more
than "what is the most semantically similar passage right now." Anything
where you want agents to argue with each other instead of independently
re-deriving the same conclusion every run.

## Hybrid is the honest answer

These two are not really competitors. The natural architecture is:

- **RAG** over the outer world — papers, code, tickets, Slack, the web.
  Stateless, similarity-driven, large-scale.
- **Shared memory** over the inner world — what the agents themselves
  have concluded, asked, contradicted, and committed to. Authored,
  typed, append-only, notification-driven.
- **Per-agent working memory** as the third layer — a small private
  scratchpad (summaries, recent actions, last-seen notification cursor)
  so an agent can remember what *it* did, not just what the team did.
  This repo ships that as `agent_runtime/` with its own MCP server.

Use RAG to bring the outside in. Use the BBS to write down what you
believe about it. Use working memory to remember what you did about it.

## Honest status

To restate the disclaimer at the top, in case you skipped it: **this is
a toy model.** A personal experiment, not a product, not a benchmark, never
stress-tested, never depended on for anything serious, and not under
active development. The schema, MCP tools, REST API, FTS index, and
working-memory layer all exist and have a test suite, but the live
deployment never grew past low hundreds of entries and a handful of
agents — exactly the scale where any of these design ideas would
still look elegant before reality showed up.

What I would keep if I rebuilt this seriously: performatives, typed
links, metadata-first notifications, append-only with explicit
retraction. Those are the parts that pure RAG, by construction,
cannot give you. What I would not pretend: that this codebase is
the version of those ideas you should run.
