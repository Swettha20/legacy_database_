# Large Oracle upload sample

The script itself is not stored in git (it is generated, and identical every time). Create it first:

```powershell
python generated_data/generate_large_oracle.py
```

Then upload **large_oracle_store.sql** in the moderniser website's Oracle SQL upload mode.

- Approximately 22.7 MB (21.68 MiB), below the backend's 25 MiB upload limit.
- 207,120 synthetic records across 10 online-store tables.
- Categories, suppliers, customers, products, addresses, orders, order items, payments, shipments and reviews.
- Four stored routines: customer order count, calculated order total, low stock count and stock adjustment.
- Includes foreign keys, unique constraints, check constraints, defaults, decimal prices, dates with times, nulls, apostrophes and Unicode text.
- Each order has two items. Order totals equal item totals, payment amounts match orders, and delivery addresses belong to the ordering customer.

For the first migration, leave **Also translate the stored procedures with the AI** unchecked to test schema and data copying. Select it on a later run to test AI translation too. Upload processing still requires the local Oracle and PostgreSQL services.

This is an Oracle SQL script, not an Oracle binary backup or a self-contained database server. Run it in an empty demonstration schema. All people, contact details, orders and payments are synthetic; phone strings and tracking IDs are intentionally marked as demo values.

The manifest lists exact table counts and the SHA-256 checksum. The validation report, when present, records an actual load into a temporary Oracle schema; it does not certify PostgreSQL migration or AI-generated translations.

To regenerate: `python generated_data/generate_large_oracle.py` from the project directory.
