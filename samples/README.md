# Sample data: SYNTHETIC

Everything in this folder is **synthetic**. No real company, customer, product, or incident
is represented. The numbers are drawn from a seeded random generator so every machine
builds byte-for-byte the same rows.

| File | Built by | Contents |
|---|---|---|
| `sales.sqlite` | `python samples/make_sales_db.py` | `orders`: ~900 orders, 4 regions, Apr-Sep 2026; `customers`: 60, three segments; `products`: 8, four categories. `orders` declares foreign keys to both. |
| `ops.sqlite` | `python samples/make_ops_db.py` | `incidents`: ~550 incidents, SEV1-SEV4, Apr-Sep 2026; `services`: 8, tier and `team_id`; `teams`: 4. `incidents.service` declares a foreign key; `services.team_id` does not (the schema graph infers it). |

The dimension tables were added for the schema graph (ADR-0005). They come from a second
seeded RNG, so the `orders` and `incidents` rows, and every number in the two report specs,
are unchanged.

`make samples` builds both. The `.sqlite` files are git-ignored; rebuild them after a clone.

To point the pipeline at your own data, write a spec whose SQL runs against your SQLite file
and pass it with `--db`. The file is opened read-only.
