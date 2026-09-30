# Sample data: SYNTHETIC

Everything in this folder is **synthetic**. No real company, customer, product, or incident
is represented. The numbers are drawn from a seeded random generator so every machine
builds byte-for-byte the same rows.

| File | Built by | Contents |
|---|---|---|
| `sales.sqlite` | `python samples/make_sales_db.py` | `orders`: ~900 orders, 4 regions, 8 products, Apr-Sep 2026 |
| `ops.sqlite` | `python samples/make_ops_db.py` | `incidents`: ~550 incidents, SEV1-SEV4, 8 services, Apr-Sep 2026 |

`make samples` builds both. The `.sqlite` files are git-ignored; rebuild them after a clone.

To point the pipeline at your own data, write a spec whose SQL runs against your SQLite file
and pass it with `--db`. The file is opened read-only.
