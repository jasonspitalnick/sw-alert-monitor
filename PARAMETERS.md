# Alert Monitor: Project Parameters
*Law Firm Competitive Intelligence & Draft Generation Tool*

---

## Purpose

Monitor competitor law firm websites for client alerts. When a narrow legal topic reaches saturation among peer firms and Snell & Wilmer has not yet published on it, automatically generate a draft client alert and deliver it by email.

---

## Firm List

Source file: `law_firm_alerts.json`

Contains approximately 120 law firms with their publications page URLs. Four firms have null URLs (Wachtell, Williams & Connolly, Keker Van Nest, Fish & Richardson) — skip these silently. Note that Locke Lord merged into Troutman Pepper Locke in January 2025; treat their separate entries as distinct sources but be aware they are now one firm. For trigger counting purposes, they count as one firm, not two.

Snell & Wilmer's publications URL: `https://www.swlaw.com/publications/?all`
Snell & Wilmer's URL is used exclusively for gap validation (Step 3 below), not for trigger counting.

---

## Primary Trigger Rule

The drafting function fires only when ALL THREE conditions are met:

**1. SATURATION**
At least 2 different law firms (excluding Snell & Wilmer) have published a dedicated alert on the same narrow subject within a rolling 10-day window.

**2. GAP**
Snell & Wilmer has NOT published a dedicated alert on that specific narrow subject. A "dedicated alert" means a standalone article or post focused primarily on that topic. A passing mention, a single bullet point in a roundup, or a tangential reference does NOT satisfy the gap condition — the drafting trigger still fires.

**3. LOCKING**
The subject has not already been drafted. Check `drafted_topics.json` for locked subjects before triggering. Once a draft is generated, the subject is locked and will not be re-drafted even if additional firms publish on it later.

---

## Defining "Narrow Subject"

This is the hardest judgment call in the system. Use the following framework:

**Too broad — do not trigger:**
- "Recent Trends in SEC Enforcement"
- "Quarterly Regulatory Roundup"
- General mentions of agency activity or leadership changes
- Broad practice area updates without a specific triggering event

**Properly narrow — trigger:**
- A specific procedural change with a defined effective date
- A specific court ruling with named parties and a concrete holding
- A precise update to a regulatory manual or guidance document
- A new filing requirement or deadline
- A specific enforcement action establishing a new precedent

**Illustrative example:**
- Too broad: "The SEC updated its Enforcement Manual in 2026."
- Properly narrow: "The SEC's 2026 Enforcement Manual update now requires Commission approval for Formal Orders of Investigation, revoking previously delegated authority to senior staff."

When assessing whether multiple alerts cover the "same" narrow subject, use semantic similarity — not title matching. Two alerts with different headlines may address the same specific development. Two alerts that both mention the SEC Enforcement Manual but focus on different provisions do not constitute a cluster.

---

## Practice Area Scope

Monitor for alerts relevant to the following areas only. Alerts outside this scope should be logged but should not count toward trigger thresholds:

- SEC enforcement (Division of Enforcement policy, procedure, and priorities)
- SEC examinations (Division of Examinations priorities and findings)
- Internal investigations (privilege, scope, procedure, best practices)
- White collar criminal defense (DOJ policy, cooperation credit, declinations, DPAs/NPAs)
- FCPA and anti-bribery enforcement
- Whistleblower programs and False Claims Act
- Securities fraud litigation (as it intersects with regulatory enforcement)
- Corporate monitors
- Cybersecurity enforcement (SEC, DOJ)
- Digital assets enforcement (SEC and DOJ actions, not legislative developments)
- Individual accountability in corporate enforcement
- Congressional investigations (as they relate to white collar defense)

---

## State Management

Maintain `alert_tracker.json` with the following structure:

- Log every identified alert from monitored firms: firm name, article title, URL, date detected, date published (if determinable), and assigned subject cluster key
- Group alerts into subject clusters based on semantic similarity — assign a short descriptive `subject_key` (e.g., `sec_enforcement_manual_formal_order_2026`)
- Track cluster size (number of distinct firms) and date range of publications
- Once a subject triggers a draft, set status to `locked` with timestamp
- Never re-draft a locked subject

---

## Execution Schedule

Scan all firm publication pages once daily at 7:00 AM Mountain Time, weekdays only (Monday–Friday).

Each scan:
1. Visit each firm's publications URL from `law_firm_alerts.json`
2. Identify articles published or updated since the last scan
3. For each new article, assess whether it falls within the practice area scope (above)
4. If in scope, assign it to an existing subject cluster or create a new one
5. Check whether any cluster now meets the saturation threshold (2+ firms, within 10-day rolling window)
6. For any cluster meeting saturation, check the S&W gap condition
7. For any cluster meeting both conditions, check `drafted_topics.json` for lock status
8. If not locked, trigger drafting and delivery, then lock the subject

**Technical note on scraping:** Many firm websites render content via JavaScript. A headless browser (Playwright) will be required for a meaningful subset of sites. Implement graceful failure — if a site cannot be scraped, log the failure and continue; do not halt the scan.

---

## Draft Generation

When triggered, the draft generator receives:
- The `subject_key` and a brief description of the triggering development
- Titles, URLs, and summaries of all competitor alerts in the cluster
- The full text of competitor alerts where extractable

The generator calls the Anthropic API (Claude) with:
- A system prompt incorporating the full contents of `STYLE_GUIDE.md`
- The competitor alert content as context
- An instruction to produce an original draft — not a synthesis or summary of competitor alerts, but an independent analysis of the underlying development

**Target length:** approximately 1,200 words  
**Format:** The draft should include a title, byline placeholder (`By Jason Spitalnick`), and body text only. Do not generate the About Snell & Wilmer boilerplate, the disclaimer footer, or contributor headshots — leave a placeholder comment at the end: `[INSERT S&W STANDARD FOOTER]`

---

## Email Delivery

**From:** jason.spitalnick@gmail.com (via Gmail SMTP with app password)  
**To:** jspitalnick@swlaw.com  
**Subject line format:** `[DRAFT ALERT] {Subject Key Description} — {Date}`  
**Attachment:** Draft as a .docx file  
**Email body:** Brief summary including:
- What triggered the draft (which firms published, on what, with links)
- Date range of competitor publications
- Confirmation that S&W has not published on this topic
- Note that the attached .docx is a first draft for review and editing before publication

---

## What This Tool Does Not Do

- It does not publish alerts. All output is a draft for human review.
- It does not monitor social media, legal news aggregators, or non-firm sources.
- It does not analyze the quality or accuracy of competitor alerts — only their existence and topic.
- It does not send alerts on topics outside the defined practice area scope, even if saturation is reached.
