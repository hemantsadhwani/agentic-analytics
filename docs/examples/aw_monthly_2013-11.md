# Monthly business review (AdventureWorks)

_Generated 2026-11-16T04:32:20+00:00 · spec `aw_monthly` · data `samples/adventureworks.sqlite` (read-only) · 34 numbers checked · 0 unverified_

## Headline

This month's sales were $5.2 million, up 4.2% from last month and 105.8% from the same month a year ago, with a gross margin of 13.9%.

## Needs a decision

The most serious item is the Reseller channel, Bikes, where sales of $2.7 million were sold below cost with a gross margin of -2.7%. Its owner should explain why pricing was set below cost and whether discounts or supplier costs caused the negative margin.

## Thirteen-month trend by channel

Internet sales started at $538.0 thousand in 2012-11 and ended at $1.8 million in 2013-11, with its highest month at $1.8 million in 2013-11. Reseller sales started at $2.0 million in 2012-11 and ended at $3.4 million in 2013-11, with its highest month at $4.2 million in 2013-01.

## Territories against the same month last year

Southwest had top sales this month at $993.8 thousand, up 40.2%. Germany showed fastest growth with $332.6 thousand, up 536.8%. Southeast had slowest growth at $272.0 thousand, up 30.1%.

## Product categories

Bikes had the most sales this month at $4.4 million with 13.9% gross margin. Components sales were $577.9 thousand at 6.4% margin, Clothing $113.8 thousand at 16.1%, and Accessories $105.9 thousand at 53.1%. Components had the lowest margin and Accessories the highest margin.

## Top five product models

The top five product models this month are Mountain-200 with $1.3 million in sales, Touring-1000 with $1.1 million, Road-350-W with $474.0 thousand, Road-250 with $355.3 thousand, and Touring-3000 with $296.8 thousand. Mountain-200 has the highest margin at 27.1%, while Touring-3000 has the lowest margin at 2.8%.

---

## Sources

Every number above matched (within 0.5%) a cell returned by that section's read-only SQL query.

### Headline

| fact | value |
|---|---|
| `margin_pct[0]` | 13.9 |
| `sales[0]` | 5197154.91 |
| `sales_vs_prior_pct[0]` | 4.2 |
| `sales_vs_year_pct[0]` | 105.8 |

### Needs a decision

| fact | value |
|---|---|
| `margin_pct[0]` | -2.7 |
| `sales[0]` | 2716128.86 |

### Thirteen-month trend by channel

| fact | value |
|---|---|
| `internet_sales[0]` | 537955.52 |
| `internet_sales[12]` | 1780920.06 |
| `reseller_sales[0]` | 1987872.71 |
| `reseller_sales[2]` | 4212971.51 |
| `reseller_sales[12]` | 3416234.85 |

### Territories against the same month last year

| fact | value |
|---|---|
| `change_pct[0]` | 40.2 |
| `change_pct[5]` | 536.8 |
| `change_pct[8]` | 30.1 |
| `sales[0]` | 993840.24 |
| `sales[5]` | 332627.82 |
| `sales[8]` | 272044.48 |

### Product categories

| fact | value |
|---|---|
| `margin_pct[0]` | 13.9 |
| `margin_pct[1]` | 6.4 |
| `margin_pct[2]` | 16.1 |
| `margin_pct[3]` | 53.1 |
| `sales[0]` | 4399626.89 |
| `sales[1]` | 577852.35 |
| `sales[2]` | 113796.83 |
| `sales[3]` | 105878.84 |

### Top five product models

| fact | value |
|---|---|
| `margin_pct[0]` | 27.1 |
| `margin_pct[4]` | 2.8 |
| `sales[0]` | 1349783.15 |
| `sales[1]` | 1092430.46 |
| `sales[2]` | 474033.25 |
| `sales[3]` | 355263.09 |
| `sales[4]` | 296830.43 |

---

_spec `aw_monthly` version `sha256:14c34bfba3ea` · provider `bedrock-converse/deepseek.v3.2` · cost $0.0082 (6 llm calls, list-price estimate) · decision `approved` by `Hemant Sadhwani` · thread `aw-ds1`_
