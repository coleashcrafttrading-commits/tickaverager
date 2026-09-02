#!/usr/bin/env python3
"""
glossary.py -- what every number in the research report actually means.

Written to be read by someone who is going to risk money on the answer, so it
says what each number is FOR, how it can mislead, and what it looks like when
it is lying. A definition that only tells you the formula is not much use: the
formula is the easy part.

    .venv/Scripts/python glossary.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# (term, plain meaning, how it misleads)
SECTIONS: list[tuple[str, str, list[tuple]]] = [

    ("The headline number", """
     Everything in the report is ranked by one figure, and it is not profit.
     Profit cannot be compared between a $600 index and a $12 stock at the same
     share count -- ranking on it just ranks by price.""",
     [
      ("Out-of-sample score",
       "Total P/L divided by the worst drawdown, on the MEDIAN symbol. A score "
       "of +0.67 means that for every dollar the strategy was ever underwater, "
       "it made 67 cents. It is unitless, so every symbol and every timeframe "
       "sits on the same axis.",
       "It says nothing about how MUCH money is involved. A strategy that "
       "makes $40 with a $60 drawdown scores the same as one making $40,000 "
       "with a $60,000 drawdown. Read it next to the summed P/L and the trade "
       "count, never alone."),

      ("In-sample score",
       "The same figure measured on the training window -- the data used to "
       "CHOOSE the settings. It is here for one reason: to be compared against "
       "the out-of-sample number.",
       "It is not a result. Random entry scored +1.855 in sample in this very "
       "study. Any strategy can be made to look excellent on the data it was "
       "fitted to; that is what fitting means."),

      ("Decay",
       "Out-of-sample score divided by in-sample score. 1.0 means the strategy "
       "performed identically on data it had never seen. 0.3 means it kept "
       "about a third of its apparent edge.",
       "Cuts both ways. Below ~0.4 the strategy was largely fitted to noise. "
       "But far ABOVE 1.0 is equally suspicious -- a decay of 6.5 does not "
       "mean a strategy got six times better, it means the in-sample number "
       "was near zero and the ratio is meaningless. Look at the two raw scores "
       "whenever decay is extreme in either direction."),
      ]),

    ("Trade statistics", """
     These describe the shape of the trading, not just the total. Two
     strategies with identical profit can be completely different bets.""",
     [
      ("Percent profitable (win rate)",
       "How many trades made money, pooled across every symbol.",
       "The single most misleading number in trading, and the one most often "
       "quoted alone. A strategy with NO STOP LOSS closes ~100% winners by "
       "construction, because losers are simply never closed -- that is "
       "exactly what the live ladder does. A 30% win rate with big winners "
       "beats a 90% win rate with one catastrophic loser. Always read it with "
       "the profit factor and the average win against the average loss."),

      ("Winners / losers",
       "The raw counts behind the win rate.",
       "Small counts make every other statistic unreliable. Twenty trades is "
       "not evidence of anything; this report refuses to score anything below "
       "that threshold per symbol."),

      ("Gross profit / gross loss",
       "Everything the winning trades made, and everything the losing trades "
       "cost, before netting them off.",
       "Kept separate on purpose. Net profit alone hides whether a strategy "
       "makes a little from many trades or a lot from a few."),

      ("Profit factor",
       "Gross profit divided by gross loss. Above 1.0 makes money; below 1.0 "
       "loses it. 1.5 is respectable for an intraday strategy; above 3 on a "
       "short sample usually means the sample is too short.",
       "It reads as a dash when there were no losing trades at all. That is a "
       "fact about the window, not an edge -- and it is precisely the state a "
       "no-stop strategy is in right up until the day it is not."),

      ("Average winning trade / average losing trade",
       "The typical size of a win and of a loss, in dollars at 100 shares.",
       "The relationship between them matters more than either. A strategy can "
       "win 70% of the time and still lose money if the losers are four times "
       "the size of the winners."),

      ("Win / loss size ratio",
       "Average win divided by average loss. Above 1.0 means winners are "
       "bigger than losers.",
       "Combine with the win rate to know whether the strategy can survive a "
       "bad run: a 40% win rate needs a ratio above about 1.5 just to break "
       "even."),

      ("Largest winning trade / largest losing trade",
       "The best and worst SINGLE trades on any symbol.",
       "The largest loss is the most important number in the whole report. It "
       "is not an average -- you do not experience the average of your worst "
       "days. If one trade can lose more than a week of profit, the strategy "
       "is one bad morning from erasing a month. If the largest win is a huge "
       "share of total profit, remove it and see whether anything is left."),

      ("Expectancy per trade",
       "What one trade is worth on average, after wins and losses net out. "
       "Total P/L divided by trade count.",
       "The number to multiply by expected trade frequency to sanity-check a "
       "projection. If expectancy is $2 and costs are $1.50 a trade, there is "
       "almost nothing there."),

      ("Average bars in a trade",
       "How long a position is typically held, in bars of whatever timeframe "
       "the row is about.",
       "Short holds mean cost sensitivity: the more often you trade, the more "
       "the slippage estimate decides the result."),
      ]),

    ("Risk and exposure", """
     What it would have felt like to hold, and what could go wrong.""",
     [
      ("Max drawdown",
       "The furthest the equity curve ever fell from its own peak, marked to "
       "market on EVERY bar -- not just at trade exits.",
       "Marking only at exits hides the hole a strategy digs while a position "
       "is open. That is the whole difference between a curve that looks flat "
       "and one that went $40,000 underwater before recovering. Reported both "
       "as the worst symbol (what to survive) and the median symbol (what to "
       "expect)."),

      ("Longest losing streak",
       "The most consecutive losing trades on the worst symbol.",
       "This is the psychological number. A strategy with a fine profit factor "
       "and an eleven-trade losing streak will be switched off by a human "
       "before it recovers. Size the position so the streak is survivable."),

      ("Average lots open at once",
       "How many positions are held simultaneously, averaged only over the "
       "bars where anything is held.",
       "Averaged over ALL bars instead, a strategy that always runs four lots "
       "but only trades a third of the time reports '1.3 lots', which answers "
       "no useful question. The number here answers: when it is on, how big is "
       "it? Multiply by shares and price for the real capital commitment."),

      ("Most lots open at once",
       "The peak simultaneous positions.",
       "This drives the worst case. Peak lots x shares x price is the largest "
       "amount the strategy ever had at risk on one symbol, and it is what the "
       "account has to be able to carry."),

      ("Time in the market",
       "The share of all bars where a position was held.",
       "High exposure means a lot of the P/L may be market drift rather than "
       "signal -- especially for a long-biased strategy in a rising market. "
       "The always-long control exists to measure exactly that."),
      ]),

    ("The checks", """
     Five independent tests. Each can kill a result on its own, and the
     ranking is by how many were passed -- not by score.""",
     [
      ("Beats the control",
       "Two control strategies -- random entry, and always-long -- ran with "
       "identical stops, targets and costs. This check asks whether the "
       "strategy beat an entry carrying NO information at all.",
       "It is the minimum bar, not a high one. Beating a coin flip means the "
       "signal does something; it does not mean the something is worth "
       "trading."),

      ("Held up on an earlier unseen window",
       "The strategy was re-run, with the same settings and no re-tuning, on a "
       "period BEFORE the training data began. It must be profitable there, "
       "consistent across symbols, AND beat the control on that window too.",
       "The hardest check and the one almost everything failed -- eleven of "
       "thirteen candidates. Note the control bar moves with the window: in a "
       "strongly rising market always-long scored +0.94, so merely making "
       "money there was not enough."),

      ("Survives double the assumed cost",
       "Every winner was re-run at half, double and quadruple the estimated "
       "slippage.",
       "Costs are an estimate, not a measurement. A result that disappears "
       "when the estimate doubles is a statement about the estimate. Strategies "
       "that trade often are far more exposed to this."),

      ("The signal, not the position management",
       "The strategy was re-run with RANDOM entries but its own exact stop, "
       "target and averaging rules. If random entry scores about the same, the "
       "signal contributes nothing.",
       "This disqualified two flagship ideas here. Order block retest scored "
       "+0.339 -- and random entry with the same execution scored +0.238, "
       "while the signal alone scored -0.009. It was position management "
       "wearing an indicator's name."),

      ("Survived on more than one timeframe",
       "Whether the family was independently selected on more than one bar "
       "size, on different windows.",
       "The hardest thing in the study to get by luck. Testing thousands of "
       "variations guarantees some look good by chance within one run, but "
       "that error does not compound across independent runs."),
      ]),

    ("How the testing worked", """
     The mechanics, so the numbers can be trusted or argued with.""",
     [
      ("Train / test split",
       "Each symbol's history is cut chronologically. Settings were chosen "
       "using the first 60% only. The last 40% chose nothing -- it only "
       "reports what the choice was worth.",
       "There is no shuffling and no overlap. Shuffled splits leak the future "
       "into the past on time-series data and produce beautiful nonsense."),

      ("Median symbol, not best",
       "Every strategy is scored on its MIDDLE symbol across the universe of "
       "eight.",
       "One spectacular symbol among eight is a curve fit, and an average lets "
       "it hide. The per-symbol table under each strategy shows the spread so "
       "the headline can be checked."),

      ("Slippage",
       "A cost charged on every fill, estimated per symbol from its own median "
       "bar range rather than assumed flat.",
       "A flat basis-point cost charged $0.13 a share on SPY, whose real "
       "spread is a penny, while charging almost nothing on a thin $12 name -- "
       "wrong in both directions at once."),

      ("No look-ahead",
       "A signal is computed on a bar's CLOSE and filled at the NEXT bar's "
       "open. A bar that touches both the stop and the target is resolved as "
       "the STOP. The engine physically refuses to let a strategy read a bar "
       "it could not have seen.",
       "These three rules are the difference between a backtest and a "
       "fantasy. Bars hide their own path, so the pessimistic reading is the "
       "honest one."),

      ("Bookkeeping rows",
       "In the operational reports, rows written when the journal and the "
       "ledger are re-synced -- a lot that had already gone, closed at no "
       "price so the two agree.",
       "They are excluded from every figure and counted separately. Including "
       "them once produced 'forty-five lots closed, $0.00 realized' on a day "
       "nothing traded."),
      ]),
]

CLOSING = """
<h2>The three things worth remembering</h2>
<ol>
<li><b>A high win rate is not an edge.</b> It is the easiest statistic to
manufacture: simply never close a loser. The live ladder does this, which is
why its realized P/L looks excellent while its open inventory tells a different
story. Profit factor and largest loss are much harder to fake.</li>
<li><b>In-sample results are worthless on their own.</b> Random entry -- an
entry carrying literally no information -- scored +1.855 in sample in this
study, on two timeframes independently. If a number was not measured on data
that chose nothing, it is not evidence.</li>
<li><b>One out-of-sample window is one sample.</b> Everything here survived a
particular few months of a particular market. That is enough to reject an idea
and not enough to trust one. Paper-trade it disarmed and compare what it
actually does against what the report said it would.</li>
</ol>
"""


def build() -> Path:
    import htmlreport as H

    B = []
    B.append('<div class="note">This explains every figure in the strategy '
             'research report: what it means, and how it can mislead. The '
             'second part matters more &mdash; most of these numbers have a '
             'well-known way of flattering a strategy, and knowing which one '
             'is the difference between reading a report and being sold '
             'one.</div>')

    for title, intro, terms in SECTIONS:
        B.append("<h2>%s</h2>" % H.esc(title))
        B.append("<p>%s</p>" % H.esc(" ".join(intro.split())))
        for term, meaning, trap in terms:
            B.append('<div class="card"><h3>%s</h3><p>%s</p>'
                     '<p class="sub"><b>Where it misleads:</b> %s</p></div>'
                     % (H.esc(term), H.esc(meaning), H.esc(trap)))

    B.append(CLOSING)

    now = datetime.now().astimezone()
    out = H.REPORT_DIR / ("glossary_%s.html" % now.strftime("%Y-%m-%d"))
    H.REPORT_DIR.mkdir(exist_ok=True)
    out.write_text(H.page(
        "Reading the research report",
        "".join(B),
        "What every number means, and how each one can mislead &middot; %s"
        % now.strftime("%d %B %Y")), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = build()
    print("written to %s" % p)
    sys.exit(0)
