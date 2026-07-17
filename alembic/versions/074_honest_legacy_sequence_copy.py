"""Strip fabricated testimonials and invented results from the legacy sequences.

``outreach_templates`` and ``industry_verticals`` — the sequences seeded from
code — say it plainly in their own docstring: numbers are estimates the reader
can check against their own shop, never fabricated customer outcomes, and social
proof is the pattern we hear across the industry, never a named case study. The
four "Deal Cracker" sequences honour that.

The six sequences this migration rewrites predate that rule and were never held
to it. They live only in the database — hand-built through the API before the
copy moved into code — so no amount of fixing the seed templates touches them.
Between them they carried:

  * invented customer quotes ("I did not know it was a $340,000 problem")
  * invented outcomes stated as fact (8.2% -> 0.9% scrap in a week; 6.1% -> 1.4%
    in 90 days; $31K saved in a month; a $40K mold-wear catch; a $47,000 escape)
  * invented case studies with places and sizes attached — a 12-press shop in
    Michigan, a shop in Ohio, a 15-press operation in Indiana that lost a $2.1M
    account
  * invented counts ("three shops in your region signed up last quarter") and
    invented specificity dressed as a finding ("a check ring at 40% wear adding
    1.8 seconds to every cycle")

None of it happened. Every one of those is a fabricated testimonial under the
FTC's endorsement rules (16 CFR 255), which do not care whether the customer is
named — an invented anonymous customer is still an invented customer. They were
also being sent: "Final v6" is *active*, with live enrollments, and its second
step asserted a $340K -> $87K result to real prospects.

What replaces them is not weaker. It is the same argument sourced honestly: the
mechanism ("the pressure curve moves before the part does"), the math the reader
runs on their own inputs, and the pattern across the industry rather than a
result someone else got. The emotional register — the 2 AM call, the retiring
operator, the scrap bin — is untouched, because none of that was ever the lie.

Down-migration is a deliberate no-op: see ``downgrade``.

Revision ID: 074_honest_legacy_copy
Revises: 073_sequence_tone
"""

from alembic import op
import sqlalchemy as sa

revision = "074_honest_legacy_copy"
down_revision = "073_sequence_tone"
branch_labels = None
depends_on = None


# (sequence name, step number, new subject or None to leave it, new body)
# Steps not listed here carried no fabrication and are left exactly as they are.
REWRITES: list[tuple[str, int, str | None, str]] = [
    # -----------------------------------------------------------------
    # Injection Molding Outreach (4-Step)
    # -----------------------------------------------------------------
    (
        "Injection Molding Outreach (4-Step)", 1, None,
        "<p>Hi {{first_name}},</p>"
        "<p>I was looking at {{company_name}} and noticed you run injection "
        "molding. Quick question: how are you currently catching short shots, "
        "flash, or sink marks before they reach your customer?</p>"
        "<p>We built an AI tool that watches the process in real time — pressure "
        "curves, melt temp, cooling profiles — and flags drift before it becomes "
        "scrap, usually a few cycles before the first bad part comes off the "
        "press.</p>"
        "<p>Would a 15-minute screen share be worth your time? Happy to show you "
        "on a sample part.</p>"
    ),
    (
        "Injection Molding Outreach (4-Step)", 2, None,
        "<p>Hi {{first_name}},</p>"
        "<p>Some math worth running. Scrap and rework in injection molding tends "
        "to run in the mid single digits as a share of revenue — and most of it "
        "gets caught downstream at inspection, not at the press where it "
        "happened.</p>"
        "<p>Take {{company_name}}'s annual resin spend, multiply by your real "
        "scrap-and-rework rate, then add the overtime to remake parts that should "
        "have been right the first time. Run your own numbers — the figure is "
        "usually bigger than the line item anyone tracks.</p>"
        "<p>What our system changes is <em>when</em> you find out: it watches "
        "parameter drift (barrel temp, pack pressure, cycle time) and flags it "
        "before the press makes bad parts, instead of after inspection does.</p>"
        "<p>If scrap reduction is on your radar, I can show you exactly how it "
        "works in 15 minutes. Want to pick a time?</p>"
    ),
    (
        "Injection Molding Outreach (4-Step)", 3,
        "the wear your SPC charts will not show you, {{first_name}}",
        "<p>Hi {{first_name}},</p>"
        "<p>Here is the failure mode I would want to catch if I ran your "
        "floor.</p>"
        "<p>A family mold starts creeping on one cavity. Cycle-to-cycle variance "
        "widens a little, but every part still measures in-spec — so sampled SPC "
        "says everything is fine, because at the moment you check, it is. The "
        "pressure curve is where it surfaces first: early-stage core-pin wear "
        "changes the shape of the curve before it changes the part.</p>"
        "<p>That is the kind of drift our AI is built to flag — while the mold can "
        "still come out for planned maintenance, instead of after a reject run and "
        "an expedited shipment.</p>"
        "<p>I am not going to tell you what that saved someone else. I would "
        "rather show you what it sees on a mold like yours. Worth 15 minutes?</p>"
    ),

    # -----------------------------------------------------------------
    # Injection Molding — Deal Cracker v4
    # -----------------------------------------------------------------
    (
        "Injection Molding — Deal Cracker v4", 2, None,
        "<p>Hi {{first_name}},</p>"
        "<p>I did something that might make you uncomfortable. I ran the numbers "
        "on what scrap and rework probably cost {{company_name}} last year.</p>"
        "<p>Here is the formula I used — you can check it yourself:</p>"
        "<p><strong>Annual resin spend × scrap rate × (1 + rework "
        "multiplier)</strong></p>"
        "<p>For a typical injection molder running 8-15 presses, the number lands "
        "between <strong>$220K and $480K per year</strong>. And that is just the "
        "direct cost. It does not include:</p>"
        "<p>— The overtime your team works to remake parts that should have been "
        "right the first time<br/>"
        "— The air freight you pay when a scrap run blows your delivery "
        "schedule<br/>"
        "— The margin you give back on the next quote because the customer "
        "remembers the last quality issue<br/>"
        "— The operator turnover because your best people are tired of "
        "firefighting</p>"
        "<p>Here is the part I find telling: most shops track scrap by weight, not "
        "by cost. So the number that reaches the board is the one that fits on a "
        "scrap ticket — not the one that comes off the bottom line. Run it "
        "properly and it is almost always bigger than anyone expected.</p>"
        "<p>{{first_name}}, I am not asking you to commit to anything. I am asking "
        "you to see your own numbers. Because once you do, the decision makes "
        "itself.</p>"
        "<p>Can I walk you through a 15-minute analysis of what this could look "
        "like for {{company_name}}? No obligation. Just math.</p>"
    ),
    (
        "Injection Molding — Deal Cracker v4", 3, None,
        "<p>Hi {{first_name}},</p>"
        "<p>Let me tell you about the conversation I have had with every injection "
        "molding shop owner I have met this year.</p>"
        "<p>It goes like this: <em>\"We have one guy — Mike, or Steve, or Dave — "
        "who has been here 22 years. He can listen to a press and tell you if "
        "something is off. He knows every mold, every material, every trick. And "
        "he is going to retire in a few years, and I have no idea what we are "
        "going to do.\"</em></p>"
        "<p>Does that sound familiar?</p>"
        "<p>Here is the truth nobody wants to say out loud: your best operator's "
        "knowledge is not documented anywhere. It lives in his head, in his hands, "
        "in the way he adjusts pack pressure by feel. And when he walks out the "
        "door, it walks out with him.</p>"
        "<p>The shops that are thriving right now are not the ones hoping their "
        "veteran operator sticks around. They are the ones that have "
        "<em>digitized</em> that knowledge. Our AI learns what a good cycle looks "
        "like on every mold — not from a textbook, but from your actual production "
        "data. It becomes the institutional memory that never retires, never calls "
        "in sick, and never forgets.</p>"
        "<p>Your new operators do not need 10 years of experience. They need a "
        "system that tells them, in real time, when something is drifting — and "
        "what to do about it.</p>"
        "<p>{{first_name}}, the labor market is not getting easier. The shops that "
        "figure this out first will hire the best people, because the best people "
        "want to work somewhere that <em>invests in getting better</em>, not "
        "somewhere that runs on tribal knowledge and crossed fingers.</p>"
        "<p>I would love to show you what that looks like in practice. 15 minutes "
        "— and you will see a path forward that does not depend on any single "
        "person.</p>"
    ),
    (
        "Injection Molding — Deal Cracker v4", 4, None,
        "<p>Hi {{first_name}},</p>"
        "<p>I have sent you three emails now, and I know that is a lot from "
        "someone you have never met. So I want to be honest with you about why I "
        "keep writing.</p>"
        "<p>It is not because I need another sale. It is because I have watched "
        "shops wait too long and lose their biggest account over two quality "
        "escapes in a single quarter — process drift, both times. The kind that "
        "shows up in a pressure curve long before it shows up in a part.</p>"
        "<p>Too busy running parts to protect the business that runs the parts.</p>"
        "<p>I do not want that to be {{company_name}}'s story.</p>"
        "<p>So here is my final offer, and then I will respect your time and move "
        "on:</p>"
        "<p><strong>Free 48-hour process audit on one press.</strong> We connect "
        "our sensors, run for two days, and hand you a report showing every "
        "anomaly, drift pattern, and hidden inefficiency in that mold's process. "
        "Most shops uncover at least one issue they did not know they had — a worn "
        "check ring quietly adding seconds to every cycle, that kind of thing.</p>"
        "<p>Whether you become a customer or not, that report is yours. No "
        "strings.</p>"
        "<p>If you are interested, just reply <strong>\"audit\"</strong> and I "
        "will set it up. Takes 10 minutes to connect.</p>"
        "<p>Either way, {{first_name}} — I genuinely respect what you do. Running "
        "an injection molding operation is hard, unglamorous, pressure-filled "
        "work. And the people who do it well rarely get the credit they "
        "deserve.</p>"
        "<p>Wishing you and the team at {{company_name}} a good quarter.</p>"
    ),

    # -----------------------------------------------------------------
    # Injection Molding — Emotional v2
    # -----------------------------------------------------------------
    (
        "Injection Molding — Emotional v2", 1, None,
        "<p>Hi {{first_name}},</p>"
        "<p>Every injection molder I talk to has one — that part that makes their "
        "stomach drop when it comes off the press wrong. The one where a bad run "
        "means a frantic weekend, an angry customer call, and an air-freight bill "
        "that wipes out the margin.</p>"
        "<p>At {{company_name}}, I imagine you know exactly which mold I am "
        "talking about.</p>"
        "<p>We built something that watches pressure curves, melt temperature, and "
        "cooling profiles in real time — and alerts your team the moment something "
        "drifts, usually a couple of cycles before the first bad part.</p>"
        "<p>That is the whole idea: move the moment you find out from the "
        "customer's phone call to the cycle it started on.</p>"
        "<p>Would 15 minutes be worth it if it meant never getting that call "
        "again? Happy to show you exactly how it works on a part like yours.</p>"
    ),
    (
        "Injection Molding — Emotional v2", 2,
        "the scrap math nobody at {{company_name}} has run",
        "<p>Hi {{first_name}},</p>"
        "<p>I ran some numbers recently that are worth sitting with. Scrap and "
        "rework in injection molding tends to run in the mid single digits as a "
        "share of revenue. But the real cost is not the material — it is the "
        "overtime, the expedited shipping, the strained customer relationships, "
        "and the quiet dread every time you quote a tight-tolerance job.</p>"
        "<p>Here is the failure mode I would want to catch on your floor: a family "
        "mold slowly wearing on one cavity. The SPC charts say everything is fine, "
        "because at the moment you sample, it is. The pressure curve moves first — "
        "early-stage core-pin wear changes the shape of the curve before it "
        "changes the part. Catch it there and the mold comes out on a planned "
        "break instead of after a reject run.</p>"
        "<p>I am not trying to sell you software, {{first_name}}. I am asking: "
        "what would it be worth to your team to catch that kind of thing before it "
        "costs you?</p>"
        "<p>15 minutes — I will show you the actual dashboard, no slides.</p>"
    ),
    (
        "Injection Molding — Emotional v2", 3,
        "the shops winning quotes have a quiet advantage",
        "<p>Hi {{first_name}},</p>"
        "<p>I will be direct with you. The shops moving on real-time quality "
        "monitoring right now all have one thing in common: they are tired of "
        "being <em>reactive</em>.</p>"
        "<p>Tired of finding defects at final inspection instead of at the press. "
        "Tired of explaining to customers why the shipment was late. Tired of that "
        "sinking feeling when a new operator runs a job for the first time "
        "unsupervised.</p>"
        "<p>The pattern is that the advantage compounds. Every cycle of data makes "
        "the system better at your specific molds and materials, so the gap "
        "between a shop that can prove its process and a shop that runs blind "
        "between inspections widens quarter over quarter. That is not a claim "
        "about your competitors — I have no idea what is happening inside their "
        "buildings. It is just how the math of the thing works.</p>"
        "<p>Can I show you what that looks like for a shop like {{company_name}}? "
        "15 minutes, and you will know whether this is worth pursuing.</p>"
    ),

    # -----------------------------------------------------------------
    # Injection Molding — Emotional v3 (A/B Test)
    # -----------------------------------------------------------------
    (
        "Injection Molding — Emotional v3 (A/B Test)", 1, None,
        "<p>Hi {{first_name}},</p>"
        "<p>You know the one. The mold where cavity 4 runs a little hot, where the "
        "operator has to babysit the pack pressure, and where every time someone "
        "new runs it, you hold your breath until the first shot comes off "
        "clean.</p>"
        "<p>I talk to injection molding shops every day. And the story is always "
        "the same — there is a mold (sometimes three) that <em>works</em>, but "
        "barely. The process window is so tight that any drift in barrel "
        "temperature, material viscosity, or cooling water flow sends it sideways. "
        "And you do not find out until QC pulls 200 parts off the line.</p>"
        "<p>What if your team got an alert on their phone <em>on cycle 3</em> — "
        "before a single bad part hit the box?</p>"
        "<p>That is what our AI is built for. It watches pressure curves, fill "
        "time, and cushion position in real time. Not SPC after the fact — "
        "<em>during</em> the shot. The problem mold is exactly where that matters "
        "most, because a tight process window is one that drifts out of spec "
        "between the samples you take.</p>"
        "<p>I would love to show you what that looks like on a mold like yours. 15 "
        "minutes, no slides — just the live dashboard.</p>"
        "<p>Worth a look?</p>"
    ),
    (
        "Injection Molding — Emotional v3 (A/B Test)", 2, None,
        "<p>Hi {{first_name}},</p>"
        "<p>Quick math that might sting a little:</p>"
        "<p>If {{company_name}} runs 10 presses and your scrap rate is 4% (which "
        "is <em>good</em> by industry standards), you are probably losing "
        "somewhere between $180K and $350K a year. That is not just resin in the "
        "grinder. That is machine time, labor, electricity, and — worst of all — "
        "customer trust. Run your own numbers; the inputs are yours, not mine.</p>"
        "<p>Here is the kind of thing that surfaces when you watch the process "
        "instead of the parts: a partially restricted cooling line. The chiller "
        "readings look fine — supply temperature is right where it should be. The "
        "pressure curve tells a different story, because the part is not cooling "
        "the way the curve says it should. Nothing in a sampling-based inspection "
        "is built to catch that. It is not a defect yet. It is a trend.</p>"
        "<p>{{first_name}}, I am not asking you to buy anything, and I am not "
        "going to quote you someone else's results. I am asking you to look at the "
        "data coming off your own presses. One 15-minute call and I will show you "
        "what the system watches for on the resins and part geometries you "
        "run.</p>"
        "<p>What does your Tuesday or Wednesday look like?</p>"
    ),
    (
        "Injection Molding — Emotional v3 (A/B Test)", 3,
        "why some shops can quote tighter than you",
        "<p>Hi {{first_name}},</p>"
        "<p>There is a reason some injection molders can quote tighter than you "
        "and still make money. It is usually not cheaper labor, and it is not "
        "newer presses. It is that they know their process well enough to run it "
        "closer to the edge — and to prove what it does.</p>"
        "<p>Think about what a scrap allowance actually is. It is a number you add "
        "to a quote to cover the parts you expect to lose, because you cannot "
        "predict which ones they will be. Make the process visible enough and that "
        "allowance shrinks toward the real cost — not because anyone cut price, "
        "but because the cost itself came down.</p>"
        "<p>The shops still relying on hourly SPC checks and operator experience "
        "are not doing anything wrong. They are just running blind between "
        "inspections, and paying for it in the padding.</p>"
        "<p>{{first_name}}, I do not know what your competitive landscape looks "
        "like at {{company_name}}, and I would not insult you by pretending I do. "
        "But the gap between shops with real-time process data and shops without "
        "it is a real one, and it is worth 15 minutes to see which side of it you "
        "are on.</p>"
        "<p>Can I show you the difference?</p>"
    ),
    (
        "Injection Molding — Emotional v3 (A/B Test)", 4, None,
        "<p>Hi {{first_name}},</p>"
        "<p>This is my last email, and I want to make it count.</p>"
        "<p>I have been in manufacturing technology long enough to know that "
        "timing matters. Maybe right now is not the right time — you are in the "
        "middle of a tooling qualification, or capacity is maxed and you cannot "
        "think about anything new.</p>"
        "<p>So here is what I would like to offer: a <strong>free process "
        "assessment</strong> for one mold. No commitment, no contract. We connect "
        "to one press, run for 48 hours, and give you a report showing exactly "
        "what your process looks like from the inside — pressure anomalies, "
        "cooling inconsistencies, cycle-to-cycle variation.</p>"
        "<p>Shops that do this almost always find something they did not know was "
        "happening: a worn check ring quietly adding seconds to every cycle, a "
        "cooling line not flowing what the gauge says, subtle short shots that "
        "pass visual inspection but fail dimensionally. That kind of thing.</p>"
        "<p>Whether or not you ever become a customer, that report is yours. "
        "Because I believe that once you <em>see</em> your process through AI "
        "eyes, you will never want to go back to running blind.</p>"
        "<p>If you are interested, just reply \"assessment\" and I will set it up. "
        "Takes less than an hour to connect.</p>"
        "<p>Either way, I respect your time and wish you and the team at "
        "{{company_name}} all the best.</p>"
    ),

    # -----------------------------------------------------------------
    # Injection Molding — Personalized v5 (with demo link)
    # -----------------------------------------------------------------
    (
        "Injection Molding — Personalized v5 (with demo link)", 2, None,
        "<p>Hi {{first_name}},</p>"
        "<p>I ran the numbers on what scrap and rework probably cost "
        "{{company_name}} last year. Here is the formula — you can check it "
        "yourself:</p>"
        "<p><strong>Annual resin spend × scrap rate × (1 + rework "
        "multiplier)</strong></p>"
        "<p>For a typical injection molder running 8-15 presses, the number lands "
        "between <strong>$220K and $480K per year</strong>. That does not "
        "include:</p>"
        "<p>— Overtime to remake parts that should have been right the first "
        "time<br/>"
        "— Air freight when scrap blows your delivery schedule<br/>"
        "— Margin you give back because the customer remembers the last quality "
        "issue<br/>"
        "— Operator turnover from constant firefighting</p>"
        "<p>The part I find telling: most shops track scrap by weight, not by "
        "cost. The number that reaches the board is the one that fits on a scrap "
        "ticket — not the one that comes off the bottom line.</p>"
        "<p>Did you get a chance to look at the dashboard I sent? Here it is again "
        "— no login, 60 seconds:</p>"
        "<p><a href=\"{{demo_link}}\" style=\"color:#2563eb;font-weight:bold;\">"
        "Open your quality dashboard →</a></p>"
        "<p>{{first_name}}, I am not asking you to commit to anything. I am asking "
        "you to see your own numbers. Want a 15-minute analysis for "
        "{{company_name}}?</p>"
    ),
    (
        "Injection Molding — Personalized v5 (with demo link)", 3, None,
        "<p>Hi {{first_name}},</p>"
        "<p>Every injection molding shop owner I have talked to this year tells me "
        "the same story:</p>"
        "<p><em>\"We have one guy — been here 22 years. He can listen to a press "
        "and tell you if something is off. Knows every mold, every material, every "
        "trick. And he is going to retire soon, and I have no idea what we are "
        "going to do.\"</em></p>"
        "<p>Sound familiar?</p>"
        "<p>The truth is: your best operator's knowledge is not documented "
        "anywhere. It lives in his head, in his hands, in the way he adjusts pack "
        "pressure by feel. When he walks out the door, it walks out with him.</p>"
        "<p>Our AI learns what a good cycle looks like on every mold — from your "
        "actual production data, not a textbook. It becomes the institutional "
        "memory that never retires, never calls in sick, never forgets.</p>"
        "<p>The dashboard I have been sharing shows exactly how this works in "
        "practice:</p>"
        "<p><a href=\"{{demo_link}}\" style=\"color:#2563eb;font-weight:bold;\">"
        "See the AI defect predictor in action →</a></p>"
        "<p>The shops that figure this out first will hire the best people — "
        "because the best people want to work somewhere that invests in getting "
        "better, not somewhere that runs on tribal knowledge and crossed "
        "fingers.</p>"
        "<p>15 minutes, {{first_name}}. I will show you what that looks like in "
        "practice.</p>"
    ),
    (
        "Injection Molding — Personalized v5 (with demo link)", 4, None,
        "<p>Hi {{first_name}},</p>"
        "<p>Three emails from someone you have never met is a lot. So let me be "
        "honest about why I keep writing.</p>"
        "<p>It is not because I need another sale. It is because I have watched "
        "shops wait too long and lose their biggest account over two quality "
        "escapes in a single quarter — process drift, both times. The kind that "
        "shows up in a pressure curve before it shows up in a part.</p>"
        "<p>Too busy running parts to protect the business that runs the parts.</p>"
        "<p>My final offer:</p>"
        "<p><strong>Free 48-hour process audit on one press.</strong> We connect "
        "sensors, run for two days, hand you a report showing every anomaly and "
        "hidden inefficiency. Most shops find at least one issue they did not know "
        "they had — a worn check ring quietly adding seconds to every cycle, that "
        "kind of thing.</p>"
        "<p>Whether you become a customer or not, that report is yours. No "
        "strings.</p>"
        "<p>If you want to see what the report looks like first, the demo is still "
        "live:</p>"
        "<p><a href=\"{{demo_link}}\" style=\"color:#2563eb;font-weight:bold;\">"
        "Preview the quality dashboard →</a></p>"
        "<p>Interested? Just reply <strong>\"audit\"</strong> and I will set it "
        "up. Takes 10 minutes to connect.</p>"
        "<p>Either way — I respect what you do, {{first_name}}. Running an "
        "injection molding operation is hard, pressure-filled work. Wishing you "
        "and the team at {{company_name}} a good quarter.</p>"
    ),

    # -----------------------------------------------------------------
    # Injection Molding — Final v6 (emotional + demo)  [ACTIVE — was sending]
    # -----------------------------------------------------------------
    (
        "Injection Molding — Final v6 (emotional + demo)", 2,
        "the scrap math your CFO has not run yet",
        "<p>Hi {{first_name}},</p>"
        "<p>I want to share a number that is going to be uncomfortable.</p>"
        "<p>For a shop running 8-15 presses, scrap and rework usually works out "
        "somewhere between <strong>$220,000 and $480,000 per year</strong> once "
        "you count it honestly. That is an estimate, not a reading off your books "
        "— take your annual resin spend, multiply by your real scrap-and-rework "
        "rate, add the overtime, and check it against your own numbers. Most shop "
        "owners are surprised, because they have been tracking scrap by weight, "
        "not by cost.</p>"
        "<p>Here is the part that really hurts: it is not just the resin you throw "
        "away. It is everything that comes with it.</p>"
        "<p>It is the overtime your team works every Saturday to remake parts that "
        "should have been right on Tuesday. It is the air freight bill when scrap "
        "blows your delivery date and your customer needs the parts by Monday or "
        "they shut down <em>their</em> line. It is the 2% margin you quietly give "
        "back on the next RFQ because the buyer remembers the quality issue from "
        "last quarter. It is the good operator who quits because he is tired of "
        "fighting fires instead of running production.</p>"
        "<p>None of that lands on a scrap ticket. Which is exactly why the number "
        "that reaches the board is always the small one.</p>"
        "<p>I built a dashboard that shows what this looks like in real time — you "
        "can see the defect predictor, the SPC charts, everything. Takes 60 "
        "seconds:</p>"
        "<p><a href=\"{{demo_link}}\" style=\"color:#2563eb;font-weight:bold;\">"
        "See your scrap cost dashboard →</a></p>"
        "<p>{{first_name}}, want me to run a 15-minute analysis on what these "
        "numbers might look like for {{company_name}}? No commitment — just "
        "math.</p>"
    ),
    (
        "Injection Molding — Final v6 (emotional + demo)", 3, None,
        "<p>Hi {{first_name}},</p>"
        "<p>I have had this exact conversation with every injection molding shop "
        "owner I have met this year. I want to tell you how it goes, because I "
        "think you will recognize it.</p>"
        "<p>It starts with a name. Mike. Or Dave. Or Steve. Someone who has been "
        "on your floor for 20-something years. He can <em>hear</em> when a press "
        "sounds wrong. He knows that mold 47 runs better a few degrees under what "
        "the setup sheet says. He adjusts pack pressure by feel — a fraction of a "
        "second longer on the second stage, because he learned ten years ago that "
        "it eliminates the sink mark on the B-side rib.</p>"
        "<p>None of this is written down. Not in a setup sheet. Not in a process "
        "doc. Not anywhere. It lives in his hands and his instincts.</p>"
        "<p>And in two or three years, he is going to shake your hand, wish you "
        "luck, and walk out the door. And everything he knows walks out with "
        "him.</p>"
        "<p>{{first_name}}, the shops that are winning right now are not the ones "
        "hoping their veteran sticks around. They are the ones that have "
        "<em>captured</em> that knowledge digitally. Our AI learns what a good "
        "cycle looks like on every mold — not from a textbook, but from your "
        "actual production history. It becomes the institutional memory that never "
        "retires, never calls in sick, and never forgets what a good night shift "
        "looked like three years ago.</p>"
        "<p>Take 60 seconds and look at how the AI defect predictor works:</p>"
        "<p><a href=\"{{demo_link}}\" style=\"color:#2563eb;font-weight:bold;\">"
        "See the AI learning from your production data →</a></p>"
        "<p>The new operators you hire next year do not need 15 years of "
        "experience. They need a system that tells them, in real time, when "
        "something is drifting and exactly what to do about it.</p>"
        "<p>15 minutes, {{first_name}}. Let me show you what that looks like "
        "before it becomes a crisis.</p>"
    ),
    (
        "Injection Molding — Final v6 (emotional + demo)", 4, None,
        "<p>Hi {{first_name}},</p>"
        "<p>I have written you three emails now, and I know that is a lot from "
        "someone you have never met. So I owe you honesty about why.</p>"
        "<p>I have watched shops wait too long and lose the account they built the "
        "business on — a couple of quality escapes in the same quarter, and a "
        "relationship that took years to build starts unwinding. Process drift, "
        "both times. The kind that shows up in a pressure curve hours before it "
        "shows up in a part.</p>"
        "<p>And it always gets explained the same way afterwards: too busy running "
        "parts to fix how we run parts. Too busy running parts to protect the "
        "business that runs the parts.</p>"
        "<p>{{first_name}}, I do not want that to be {{company_name}}'s story. So "
        "here is my final offer, and then I will leave you in peace:</p>"
        "<p><strong>A free 48-hour process audit on one press.</strong></p>"
        "<p>We connect our sensors to one machine, run for two days, and hand you "
        "a detailed report showing every anomaly, every drift pattern, every "
        "hidden inefficiency in that mold's process. No cost. No obligation. No "
        "strings.</p>"
        "<p>Most shops find at least one problem they did not know existed — a "
        "worn check ring quietly adding seconds to every cycle, that kind of "
        "thing. Seconds per cycle is hours of press time a week, and it never "
        "shows up on a scrap report.</p>"
        "<p>You can preview exactly what the report dashboard looks like right "
        "now:</p>"
        "<p><a href=\"{{demo_link}}\" style=\"color:#2563eb;font-weight:bold;\">"
        "Preview the process audit dashboard →</a></p>"
        "<p>If you want the audit, just reply with the word "
        "<strong>\"audit\"</strong> and I will set everything up. Takes 10 minutes "
        "to connect.</p>"
        "<p>Either way, {{first_name}} — I have genuine respect for what you do. "
        "Running an injection molding operation is relentless, high-pressure, "
        "thankless work. The people who do it well almost never get the "
        "recognition they deserve. I hope {{company_name}} has a strong quarter "
        "ahead.</p>"
    ),
]


def upgrade() -> None:
    conn = op.get_bind()
    for name, step_number, subject, body in REWRITES:
        if subject is None:
            conn.execute(
                sa.text(
                    "UPDATE sequence_steps SET body_template = :body "
                    "WHERE step_number = :step AND sequence_id IN "
                    "(SELECT id FROM outreach_sequences WHERE name = :name)"
                ),
                {"body": body, "step": step_number, "name": name},
            )
        else:
            conn.execute(
                sa.text(
                    "UPDATE sequence_steps "
                    "SET subject_template = :subject, body_template = :body "
                    "WHERE step_number = :step AND sequence_id IN "
                    "(SELECT id FROM outreach_sequences WHERE name = :name)"
                ),
                {"subject": subject, "body": body, "step": step_number, "name": name},
            )


def downgrade() -> None:
    """Deliberately a no-op.

    The "previous state" this would restore is a set of fabricated customer
    testimonials and invented results, in copy that is actively sent to real
    prospects. A downgrade path that puts them back is a footgun with no
    legitimate trigger: there is no circumstance in which the right response to a
    bad deploy is to resume asserting a $340,000 result that never happened.

    Rolling back the schema around this migration is unaffected — it changes no
    schema. Restoring the old copy, if anyone ever genuinely wanted it, is a
    database restore, and it should take exactly that much deliberation.
    """
