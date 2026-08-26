# Keystone — 5 minute demo

All of it in the dashboard. No terminal, no setup, no local install.

Open these four tabs before you record:

1. `https://github.com/HankH18/keystone/blob/main/ARCHITECTURE.md`
2. `https://keystone-dashboard-2rot.onrender.com/`
3. `https://keystone-dashboard-2rot.onrender.com/proposals/2`
4. `https://keystone-dashboard-2rot.onrender.com/proposals/206`
5. `https://keystone-dashboard-2rot.onrender.com/audit`

The dashboard is slow on the very first load — the host sleeps when idle. Load every tab once
before recording so none of them wake up on camera.

---

## 1. The diagram · 0:45

**TAB 1.** Show the first diagram.

> "Three systems disagree — a CRM, an app database, and payments. Keystone copies all three in
> read-only, resolves them into one identity layer, and runs committed SQL rules over the result.
>
> Every conflict becomes a proposal. A proposal is a suggestion with evidence attached. Nothing it
> suggests ever reaches production until a person approves it.
>
> That last part is the whole system. Everything else is plumbing."

---

## 2. The dashboard · 0:40

**TAB 2.** The Overview page.

> "Three thousand and fifty conflicts, across fourteen types, from three hundred and sixty thousand
> records.
>
> Every number on this page is fetched twice — once from the graded scorecard, once live from the
> API — and compared. That's the Match column. If those two ever disagreed, the page would say so
> rather than quietly showing you one of them."

Scroll to **Proposals by status**.

> "Three thousand and fifty conflicts, three thousand and fifty proposals. Twenty-six seventy
> waiting for a reviewer, three eighty held because they touch something sensitive.
>
> Zero applied. Nothing has been written to anything."

---

## 3. A conflict and its proposal · 1:00

**TAB 3.** Proposal 2.

> "Same person in two systems, different grade. This is the fix Keystone proposes, and this is why."

Scroll to the **Evidence packet**, then to **confidence → terms**.

> "Confidence is 0.90. That's not a model output and it's not a guess — it's a weighted sum of named
> signals, and the proposal carries every term that produced it, including the four that contributed
> nothing. A reviewer can add it up by hand.
>
> Status: pending. Nothing has been written."

---

## 4. Low confidence stays with a person · 0:55

**TAB 3, same page.** Scroll to **Auto-apply gate (R24)**.

> "Ten conditions. Confidence 0.90 misses the 0.95 floor, so this doesn't move.
>
> But look at the last row. Status is pending — and the role that applies changes is only allowed to
> move something from *approved* to *applied*. So even at perfect confidence, a machine still can't
> skip the person. That's a database grant, not an if-statement in code somebody can delete."

**TAB 4.** Proposal 206.

> "This one's different. One condition was evaluated and it stopped right there — the fix touches a
> legal name.
>
> Sensitive fields can never auto-apply, at any confidence, including a perfect one. It doesn't even
> reach the confidence check. It short-circuits before it gets there."

---

## 5. The spend cap · 0:50

**TAB 5.** The Audit log page. Point at **Verification checks** at the top.

> "The reconciler spends money, so it runs under a hard cap. This is the burst test: a hundred and
> twenty jobs rush the budget at once.
>
> Six get through. A hundred and fourteen are refused. Ledger violations: zero — it never goes
> negative, not once. And the retry wave of ten was granted nothing, so retrying doesn't get you
> around it.
>
> The refusal comes from the database, not from the application."

Scroll down to the audit rows.

> "And every action the system takes lands here — the proposals, the runs, the reviewer decisions.
> This is the same log those numbers come from."

---

## 6. Close · 0:40

Back to **TAB 2**.

> "Nothing here is true because I said so.
>
> The reconciler can't approve its own work — that's a grant. The cap can't be overrun — that's a
> trigger. The sources can't be written to — the read-only interface has no write method at all, and
> a test fails the build if one ever appears.
>
> Sixteen of sixteen on the committed harness, against a database rebuilt from the committed seed.
> And everything you just watched was the deployed instance, not my laptop."

---

# If something goes wrong mid-take

**Don't approve anything.** Approving is permanent and single-use. The demo never needs you to click
Approve — every beat is read-only.

If a proposal page won't load, the host went to sleep. Reload once and wait.

Swap-in ids, same behaviour:

- **Beat 3 / 4 (0.90, ten conditions):** `2`, then `73`, `91`, `101`
- **Beat 4 (sensitive, one condition):** `206`, then `216`, `232`, `275`

---

# Don't

- **Don't click Approve or Reject.** Nothing in this demo needs it, and it can't be undone.
- **Don't say "sixteen of sixteen"** while pointing at the Verification checks panel unless you mean
  the whole harness — that panel shows the full run, which is genuinely 16/16, but only the spend-cap
  row is the one you're talking about in beat 5.
- **Don't promise the deployed instance is the graded environment.** It runs the same code and the
  same dataset, but the graded numbers come from the committed harness run.
