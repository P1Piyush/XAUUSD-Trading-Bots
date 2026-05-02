# XAUUSD Bot Refinement Plan

## Objective

Improve the bot's real trading quality by increasing expectancy and profit factor while reducing drawdown and low-quality trade frequency.

This plan is designed to prevent random feature creep. Every refinement must be:

- Measured against a saved baseline
- Tested under the same execution assumptions
- Validated out-of-sample
- Rejected if it only improves in-sample PnL

## Success Metrics

Use these as the primary scorecard for every strategy and every experiment:

- Profit factor
- Expectancy per trade
- Win rate
- Average win / average loss
- Max drawdown
- Trade count
- Return over test window
- Best and worst hours
- Long vs short split
- Trend vs range split
- Volatility bucket split

Prefer stable improvements in profit factor, expectancy, and drawdown over raw PnL.

## Working Rules

1. Do not change live bot logic until the change beats the saved baseline in backtests.
2. Test one variable at a time unless running a controlled sweep.
3. Keep execution assumptions fixed across comparisons.
4. Reject any refinement that works only on one narrow date range.
5. Separate strategy refinement from portfolio blending.

## Agent Operating Model

Use the agent definitions in [`agents/`](../agents/README.md) to split work cleanly:

- `Baseline Analyst` updates the scorecard and identifies weak buckets
- `Backtest Refiner` tests one controlled refinement
- `Execution Realism Agent` validates assumptions and cost realism
- `Strategy Reviewer` decides what to keep, pause, or promote
- `ML/Data Agent` improves ranking and model quality after deterministic cleanup

## Phase 1: Establish The Baseline

Goal: produce a clean scorecard for the current system.

Tasks:

- Run the current backtests and save outputs into `results/`
- Generate a summary report for every CSV in `results/`
- Record the baseline metrics for:
  - `backtest_results.csv`
  - `backtest_full_results.csv`
  - `backtest_v6_results.csv`
  - `m1_backtest_results.csv`
  - `scalp_backtest_results.csv`
  - any new strategy-specific result files

Deliverables:

- One baseline metrics snapshot
- A list of the worst-performing trade buckets
- A list of the most stable high-quality buckets

## Phase 2: Standardize Backtest Assumptions

Goal: make results comparable across scripts.

Tasks:

- Use one set of assumptions for:
  - spread
  - slippage
  - position sizing
  - cooldown logic
  - session filters
  - timeout exits
- Remove any backtest that writes to ad hoc locations or uses different accounting logic
- Normalize output fields so all result CSVs contain at least:
  - `time`
  - `strategy`
  - `direction`
  - `result`
  - `pnl`
  - `balance`
  - `hour`
  - `day_of_week`
  - `regime`
  - `volatility_bucket`

Deliverables:

- A common results schema
- Comparable scorecards across strategies

## Phase 3: Identify Where The Bot Loses Money

Goal: stop paying tuition to bad market conditions.

Tasks:

- Segment all trades by:
  - UTC hour
  - day of week
  - long vs short
  - trend vs range regime
  - low / medium / high volatility
  - pre-news / post-news / normal conditions
- Rank the worst buckets by:
  - negative expectancy
  - poor profit factor
  - low trade count with high drawdown

Expected actions:

- Disable low-value hours
- Raise thresholds in chop
- reduce or remove weak counter-trend trades
- tighten filters during spread expansion or volatile noise

Deliverables:

- Bottom 20% buckets to cut
- Top 20% buckets to protect and scale

## Phase 4: Improve Exits Before Entries

Goal: improve trade management before adding more setup complexity.

Reason:

Most systems are over-engineered on entry and under-engineered on exit.

Test matrix:

- TP1 / TP2 ratio changes
- Partial close percentage
- Break-even timing
- trailing stop activation
- max holding time
- stop width by regime

Promote changes only if they improve:

- expectancy
- drawdown
- average loss control
- stability across multiple periods

## Phase 5: Rebuild The Quality Score

Goal: replace loose binary filters with a better setup ranking system.

Candidate inputs:

- ADX
- RSI alignment
- EMA distance
- sweep quality
- FVG size relative to ATR
- displacement strength
- HTF bias alignment
- session hour
- news proximity
- spread / liquidity quality

Approach:

- score every historical trade using these features
- compare high-score vs low-score expectancy
- find the cutoff where expectancy becomes positive and stable

Deliverables:

- New quality-score formula
- Recommended cutoff by strategy

## Phase 6: Separate Strategy Research

Goal: stop mixing strategy quality with portfolio quality.

Track these independently:

- ICT / SMC bot
- PDH / PDL bot
- scalp bot
- RL-assisted logic
- any new experimental strategy

Questions to answer:

- Which strategy actually has the strongest edge?
- Which strategy adds diversification instead of noise?
- Which strategy should be paused or retired?

Deliverables:

- Strategy league table
- recommendation on whether to run one strategy, two strategies, or a controlled ensemble

## Phase 7: Walk-Forward Validation

Goal: prevent overfitting.

Method:

1. Optimize on a training window
2. Validate on the next unseen window
3. Roll forward and repeat

Reject a change if:

- it only improves the training window
- it collapses trade count too hard
- it creates a fragile edge dependent on one month or one session

## Phase 8: Parameter Sweeps With Guardrails

Goal: tune only high-impact parameters.

Priority parameters:

- ADX threshold
- quality score threshold
- ATR stop multiplier
- TP1 / TP2 ratios
- best session hours
- cooldown duration
- news blackout window

Guardrails:

- optimize for profit factor, expectancy, and drawdown first
- require minimum trade count
- compare multiple time windows
- avoid selecting the absolute top result if nearby settings are unstable

## Phase 9: ML Guardian Upgrade

Goal: make the ML layer rank trades better instead of acting as a vague filter.

Data needed per trade:

- entry features
- regime label
- session label
- volatility label
- final outcome
- path-based outcome such as max favorable excursion and max adverse excursion

Use ML for:

- probability ranking
- dynamic thresholding by regime
- rejecting low-quality setups

Do not use ML to hide weak deterministic logic. First fix obvious bad buckets.

## Priority Order

Work in this order:

1. Baseline metrics
2. Backtest standardization
3. Bucket analysis
4. Exit refinement
5. Quality-score rebuild
6. Walk-forward validation
7. Parameter sweeps
8. ML retraining

## First Two-Week Execution Plan

### Week 1

- Run current backtests
- Generate and save baseline reports
- tag trades by hour, side, and rough regime
- identify the worst hours and lowest-quality regimes
- implement the first filter removals in backtest only

### Week 2

- run exit-logic sweeps
- compare expectancy and drawdown against baseline
- perform one walk-forward check on top candidates
- choose 1 to 2 changes for live-bot integration

## Promotion Checklist

Before a refinement reaches the live bot, it must satisfy all of these:

- Beats baseline on expectancy or profit factor
- Does not materially worsen drawdown
- Has enough trades to matter
- Survives at least one out-of-sample test
- Keeps execution assumptions realistic

## Immediate Next Actions

1. Run `python3 report_results.py`
2. Save the baseline output in `results/`
3. Pick the worst 2 to 3 trade buckets
4. Refine filters or exits in backtest only
5. Re-run reports and compare against baseline
