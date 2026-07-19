---
name: market-sentiment
description: Gather and clearly-labelled external sentiment on a company — retail/Reddit chatter, news headlines, the narrative — as a SEPARATE, sourced layer alongside the edgar MCP's primary-source financials. Use when the user asks "what are people saying about", "sentiment on", "the Reddit take", "recent headlines", "what's the narrative". Uses web search/fetch (no scraping keys). Sentiment is never presented as fact and never overrides the filings.
---

# Market sentiment (a labelled layer, never the truth)

You are gathering what *other people are saying* about a company — news, retail
forums, the prevailing narrative — to sit NEXT TO, never on top of, the
primary-source analysis from the `edgar` MCP. This exists because the user
sometimes wants the mood; it must never contaminate the facts.

## Hard rules

1. **Sentiment ≠ fact.** Everything here is "X reported…", "a Reddit thread
   claimed…", "headlines framed it as…". Never state a rumour, price target,
   or "the Street thinks" as if it were established. If it's not from a filing,
   it's opinion — label it.
2. **Attribute and date every claim.** Source name + date + link. "Unsourced"
   means it doesn't ship. Anonymous forum posts are labelled as exactly that.
3. **The filings win, always.** When sentiment contradicts the numbers, say so
   and side with the primary source: "the thread says margins are collapsing;
   the 10-K shows gross margin 46.9% [acc …], flat YoY — the claim is wrong."
4. **No trading calls.** You summarize narrative; you do not tell the user to
   buy or sell, and you flag when sentiment looks like pump/hype or
   coordinated posting.
5. **Separate section, clearly walled.** In any combined deliverable, sentiment
   is its own clearly-titled block, visually distinct from the sourced
   financials.

## Workflow

1. **Anchor on the facts first.** If not already done, get the primary-source
   picture (`company_dossier` or the `company-dossier` skill) so you can
   fact-check sentiment against it.
2. **News/headlines.** `WebSearch` for recent coverage ("<company> earnings",
   "<company> guidance", "<company> lawsuit"). Capture outlet, date, headline,
   and the substantive claim. `WebFetch` the ones that matter for detail.
3. **Retail / forum sentiment.** `WebSearch` for the discussion (e.g.
   `site:reddit.com <ticker>`, r/investing, r/stocks threads). Summarize the
   dominant view, the bull/bear points people actually make, and the tone
   (euphoric / fearful / mixed). Never launder an anonymous claim into a fact.
4. **Fact-check the loud claims.** Take the 2–3 most consequential assertions
   and check each against the filings via the edgar MCP. Report which hold up
   and which don't, with citations.
5. **Deliver.** A short, clearly-labelled "External sentiment (opinion, dated)"
   block: (a) headline narrative, (b) retail take + tone, (c) the
   fact-check verdicts, (d) any hype/manipulation flags. End with the
   one-liner: "This is narrative, not fundamentals — see the sourced financials
   for what the company actually reported."

## Pitfalls
- **Recency.** Sentiment rots fast — always date it; prefer the last weeks.
- **Echo chambers.** One viral thread isn't "sentiment". Note how widely a view
  is actually held.
- **Ticker collisions.** Confirm you've got the right company (name + exchange),
  not a same-ticker namesake.
- **Don't let it leak into the dossier's factual sections.** The `company-
  dossier` skill forbids "what other investors are doing" in its sourced parts;
  this skill is where that material lives, quarantined and labelled.
