# Keystone — 5 minute demo

Everything happens in the browser. Five tabs, no terminal, no setup.

**Open these and load each one once before you record** (the host sleeps when idle, and you don't
want a page waking up on camera):

1. `https://github.com/HankH18/keystone/blob/main/ARCHITECTURE.md`
2. `https://keystone-dashboard-2rot.onrender.com/`
3. `https://keystone-dashboard-2rot.onrender.com/proposals/2`
4. `https://keystone-dashboard-2rot.onrender.com/proposals/206`
5. `https://keystone-dashboard-2rot.onrender.com/audit`

**Never click Approve or Reject.** You don't need to, and it can't be undone.

---

## 1. What problem this solves · 45s

**Tab 1.** Show the first diagram. Trace the arrows left to right with your cursor while you talk.

> "Most companies keep the same customer in three or four different systems. A CRM, an app database,
> a payments processor. They're supposed to agree with each other. Over time they quietly stop.
>
> Somebody pays but no deal gets created. Two records share an email. A refund goes through in one
> place and not the other. Nobody notices until a customer is angry.
>
> Keystone is the thing that notices. It pulls a read-only copy of all three systems into one place,
> works out which records are the same person, and then constantly checks rules that should always
> be true. When one breaks, it writes up what it found and what it thinks the fix is — and then it
> stops and waits for a human.
>
> It never fixes anything on its own. That's the part I want to show you."

---

## 2. What it found · 40s

**Tab 2.** The Overview page.

> "This is running against 360,000 records across those three systems. It found 3,050 places where
> they disagree, and sorted them into fourteen kinds of disagreement.
>
> Now — every number on this page is deliberately fetched twice. Once from the test harness that
> graded this run, and once live from the database right now. Then it compares them. That's what
> this Match column is.
>
> The reason it does that: a dashboard that reads one number and prints it will happily show you a
> confident, wrong figure forever. This one can't. If those two ever disagreed, this page would say
> Mismatch instead of picking one."

Scroll down to **Proposals by status**.

> "Every one of those 3,050 problems got a written-up fix. 2,670 are waiting for someone to look at
> them. 380 are held back because the fix would touch something sensitive.
>
> And zero have been applied. Nothing has been written to anything."

---

## 3. What one of them actually looks like · 60s

**Tab 3.** Proposal 2.

> "Here's one. Same person exists in the CRM and in the app database, and the two systems disagree
> about what grade they're in.
>
> This is the fix it's proposing — and everything underneath is why it thinks so."

Scroll to the **Evidence packet**, then keep going to **confidence → terms**.

> "That 0.90 confidence is the part people usually hand-wave. It's not a gut feeling and it isn't a
> language model guessing at a number.
>
> It's arithmetic, and it's all right here. These are the specific things it checked. This one
> matched on a hard ID, worth 0.35. Emails matched, worth 0.25. It also checked name and date of
> birth and got nothing, so that's zero — and it shows you that too. Then it subtracts for the
> fields that disagree.
>
> If you don't trust the number, you can add it up yourself on paper. That's the point of showing
> the terms that scored nothing — you can see what it looked at, not just what it liked."

---

## 4. Why it won't fix this itself · 55s

**Still tab 3.** Scroll to **Auto-apply gate (R24)**.

> "So here's the interesting bit. There's a version of this system that's allowed to fix things
> automatically — but only if it clears every one of these ten conditions.
>
> This one scores 0.90 and the bar is 0.95. So it stops. A human gets it.
>
> But look at the bottom row, because that's the one that actually matters. It says the status is
> still pending. The part of this system that's allowed to write changes is only permitted to touch
> something a human has already marked approved.
>
> That's not an if-statement in my code that somebody could delete. That's a permission in Postgres.
> Even if the confidence were a perfect 1.0, it would still stop right here and wait for a person."

**Tab 4.** Proposal 206.

> "And this one's different in a way I think is the strongest thing in the project.
>
> Look how short the list is. It checked one condition and stopped. The fix here would change
> someone's legal name — and legal names, billing owners, anything with a financial consequence are
> permanently off limits to automation.
>
> It didn't even get as far as reading the confidence score. Doesn't matter what the number is.
> It short-circuits before it can matter."

---

## 5. It can't run up a bill either · 50s

**Tab 5.** The Audit log page. The **Verification checks** panel is at the top.

> "The other thing an unattended agent can do to you is spend your money. So it runs under a hard
> cap, and this is the test that proves the cap holds.
>
> It throws 120 jobs at the budget simultaneously, all trying to spend at once. Six get through.
> 114 get refused. And the ledger never goes negative — not once, not by a cent.
>
> Two details worth calling out. That refusal comes from the database itself, not from application
> code. And see the retry wave — ten jobs retried after being refused, and got granted nothing.
> Retrying doesn't get you around the cap."

Scroll down to the log rows underneath.

> "And underneath, every single thing this system has done. Every proposal it wrote, every run,
> every decision. Those numbers up top come out of this log — so you can check them against it."

---

## 6. Close · 40s

Back to **tab 2**.

> "The through-line for all of it: none of this is safe because I said it's safe.
>
> The reconciler can't approve its own work — that's a database permission. It can't overrun the
> budget — that's a database trigger. It can't write back to the source systems — the interface it
> uses to read them doesn't have a write method at all, and there's a test that fails the build if
> anybody ever adds one.
>
> The committed test harness passes sixteen out of sixteen against a database rebuilt from scratch.
> And everything you just watched was the live deployed version, not my laptop."

---

# If something goes sideways

**A page won't load** — the host fell asleep. Reload once and give it a few seconds.

**You want a second take** — nothing you did changed anything, so just go again. Every beat here is
read-only. But if you want different records:

- **Beats 3 and 4** (0.90 confidence, ten conditions): `2`, then `73`, `91`, `101`
- **Beat 4's sensitive one** (one condition, stops immediately): `206`, then `216`, `232`, `275`

---

# Three things not to say

**Don't click Approve or Reject.** Each proposal can only be used once and there's no undo. The demo
never needs it.

**Don't call the Verification checks panel "the spend cap test."** That panel is the whole harness,
all sixteen checks. The spend cap is one row in it. Say "here's the spend cap check" and point at
the row, not at the panel.

**Don't say the deployed version is what got graded.** It runs the same code against the same
dataset, but the graded numbers come from the committed harness run. It's a real deployment, not the
grading environment — that distinction is worth keeping straight if anyone asks.
