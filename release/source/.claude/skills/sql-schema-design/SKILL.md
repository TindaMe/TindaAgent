---
name: sql-schema-design
category: Database
description: "MUST USE when creating or altering tables, defining columns, choosing data types, or writing migrations. Enforces normalization, correct data types (TIMESTAMPTZ, NUMERIC for money), NOT NULL discipline, constraints, and safe migration patterns."
---

# SQL Schema Design & Data Integrity

## Normalization — 1NF Through 3NF

**1NF** — every column holds a single atomic value. No arrays, no comma-separated lists.

```sql
-- BAD: multi-valued column
CREATE TABLE students (id INT PRIMARY KEY, phone_numbers VARCHAR(500));  -- '555-1234,555-5678'

-- GOOD: one fact per row
CREATE TABLE student_phones (
    student_id INT REFERENCES students(id),
    phone VARCHAR(20) NOT NULL,
    PRIMARY KEY (student_id, phone)
);
```

**2NF** — with composite keys, every non-key column depends on the _entire_ key, not just part.

**3NF** — no transitive dependencies. Non-key columns depend only on the PK.

```sql
-- BAD (violates 3NF): city/state depend on zip_code, not employee_id
CREATE TABLE employees (
    id INT PRIMARY KEY, zip_code VARCHAR(10),
    city VARCHAR(100), state VARCHAR(50)  -- determined by zip_code
);

-- GOOD: extract the dependency
CREATE TABLE zip_codes (zip_code VARCHAR(10) PRIMARY KEY, city VARCHAR(100) NOT NULL, state VARCHAR(50) NOT NULL);
CREATE TABLE employees (id INT PRIMARY KEY, zip_code VARCHAR(10) REFERENCES zip_codes(zip_code));
```

**Start normalized (3NF). Denormalize only where profiling proves it necessary** — typically analytics/reporting or caching aggregates on hot paths.

## JSON and EAV Anti-Patterns

```sql
-- BAD: structured data in JSON — no FK, no type checking, no constraints
CREATE TABLE orders (id INT PRIMARY KEY, data JSONB);

-- GOOD: relational model + JSON only for truly dynamic attributes
CREATE TABLE orders (
    id INT PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(id),
    total NUMERIC(12,2) NOT NULL,
    metadata JSONB  -- custom form fields, vendor-specific data
);
```

EAV (Entity-Attribute-Value) is even worse — turns a relational DB into a poor key-value store. No type enforcement, no constraints, queries require pivoting. Use proper columns or JSONB with a GIN index.

## Data Types — Choose Correctly

### TIMESTAMPTZ Always

```sql
-- BAD: loses timezone context
CREATE TABLE events (occurred_at TIMESTAMP);

-- GOOD: stores UTC, converts on display
CREATE TABLE events (occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
```

### NUMERIC for Money, Never FLOAT

```sql
-- BAD: 0.1 + 0.2 != 0.3
CREATE TABLE invoices (amount REAL);

-- GOOD: exact precision
CREATE TABLE invoices (
    amount NUMERIC(12,2) NOT NULL CHECK (amount >= 0),
    currency CHAR(3) NOT NULL DEFAULT 'USD'
);
```

### Other Type Rules

- **INT vs BIGINT**: Use INT (4 bytes, max ~2.1B) unless you expect >2 billion rows. BIGINT doubles index size.
- **UUIDs**: Use BIGSERIAL for internal IDs. UUIDv7 for distributed/public-facing. **Never UUIDv4 as PK** — random inserts fragment B-trees.
- **IP addresses**: Use `INET` not VARCHAR — validates, sorts numerically, supports containment operators.
- **Enums vs lookup tables**: Use enums only for values that will **never change**. Lookup tables let you add values with INSERT and support metadata.

## Constraints — The Database Outlives Every App

### CHECK Constraints

```sql
-- BAD: no domain validation
CREATE TABLE products (price NUMERIC(10,2), discount_pct NUMERIC(5,2));

-- GOOD: enforce business rules at the database level
CREATE TABLE products (
    price NUMERIC(10,2) NOT NULL CHECK (price >= 0),
    discount_pct NUMERIC(5,2) CHECK (discount_pct BETWEEN 0 AND 100),
    start_date DATE NOT NULL,
    end_date DATE,
    CHECK (end_date IS NULL OR end_date >= start_date)
);
```

### NOT NULL, UNIQUE, and FK Enforcement

Every column should be NOT NULL unless genuinely optional. NULL breaks equality (`NULL != NULL`) and introduces three-valued logic. Enforce uniqueness at database level — app-only uniqueness has race conditions under concurrent load.

```sql
CREATE TABLE users (
    id INT PRIMARY KEY,
    email VARCHAR(254) NOT NULL UNIQUE,     -- DB enforces uniqueness
    display_name VARCHAR(100) NOT NULL,
    bio TEXT,                                -- genuinely optional
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Always declare foreign keys — the database outlives every app version
CREATE TABLE orders (
    customer_id INT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT
);
```

## Schema Anti-Patterns

### God Tables

```sql
-- BAD: one table for everything — locking hotspot, NULLs everywhere
CREATE TABLE entities (
    id INT PRIMARY KEY, type VARCHAR(50),
    name VARCHAR(200), email VARCHAR(254), price NUMERIC(10,2),
    quantity INT, log_message TEXT, parent_id INT
    -- 60 more columns, most NULL
);

-- GOOD: separate tables per domain concept
CREATE TABLE users    (id INT PRIMARY KEY, name VARCHAR(200) NOT NULL, email VARCHAR(254) NOT NULL UNIQUE);
CREATE TABLE products (id INT PRIMARY KEY, name VARCHAR(200) NOT NULL, price NUMERIC(10,2) NOT NULL);
```

### Polymorphic Associations

```sql
-- BAD: no FK possible — orphans accumulate
CREATE TABLE comments (
    id INT PRIMARY KEY, body TEXT NOT NULL,
    commentable_type VARCHAR(50),  -- 'Post', 'Photo'
    commentable_id INT
);

-- GOOD: separate FK columns with CHECK
CREATE TABLE comments (
    id INT PRIMARY KEY, body TEXT NOT NULL,
    post_id INT REFERENCES posts(id),
    photo_id INT REFERENCES photos(id),
    CHECK ((post_id IS NOT NULL)::int + (photo_id IS NOT NULL)::int = 1)
);
```

### Soft Delete Pitfalls

Soft delete (`deleted_at TIMESTAMPTZ`) causes: unique constraint chaos, query complexity explosion (`WHERE deleted_at IS NULL` on everything), table bloat, and FK breakdown.

```sql
-- If you must soft-delete, use partial unique index:
CREATE UNIQUE INDEX idx_users_email_active ON users(email) WHERE deleted_at IS NULL;

-- BETTER: archive table + hard delete
CREATE TABLE users_archive (
    id UUID PRIMARY KEY,
    original_data JSONB NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## Safe Migrations

### Adding Columns

```sql
-- BAD: NOT NULL + DEFAULT rewrites entire table (locks it) in older PG
ALTER TABLE users ADD COLUMN bio TEXT NOT NULL DEFAULT 'No bio';

-- GOOD: three-step approach
ALTER TABLE users ADD COLUMN bio TEXT;                          -- 1. nullable (instant)
UPDATE users SET bio = 'No bio' WHERE id BETWEEN 1 AND 10000;  -- 2. backfill in batches
ALTER TABLE users ALTER COLUMN bio SET NOT NULL;                -- 3. constrain
ALTER TABLE users ALTER COLUMN bio SET DEFAULT 'No bio';
```

### Adding Indexes

```sql
-- BAD: blocks writes
CREATE INDEX idx_users_email ON users(email);

-- GOOD: non-blocking
CREATE INDEX CONCURRENTLY idx_users_email ON users(email);
```

### Adding Foreign Keys

```sql
-- BAD: validates all rows while holding AccessExclusive lock
ALTER TABLE orders ADD CONSTRAINT fk_customer
    FOREIGN KEY (customer_id) REFERENCES customers(id);

-- GOOD: add NOT VALID first, validate separately
ALTER TABLE orders ADD CONSTRAINT fk_customer
    FOREIGN KEY (customer_id) REFERENCES customers(id) NOT VALID;
-- Later:
ALTER TABLE orders VALIDATE CONSTRAINT fk_customer;
```

### Always Set Lock Timeout

```sql
SET lock_timeout = '5s';  -- abort deploy, don't bring down the app
```

## Rules

1. **Start at 3NF, denormalize only when profiling demands it**
2. **Use TIMESTAMPTZ always** — never bare TIMESTAMP
3. **Use NUMERIC for money** — never FLOAT or REAL
4. **Prefer INT over BIGINT** unless you expect >2 billion rows
5. **Avoid UUIDv4 as PK** — use UUIDv7 or BIGSERIAL
6. **NOT NULL by default** — allow NULL only with a specific reason
7. **CHECK constraints for business rules** — prices >= 0, dates in order, percentages bounded
8. **UNIQUE at database level** — application-only uniqueness has race conditions
9. **No god tables** — separate tables per domain concept
10. **No EAV** — use proper columns or JSONB with GIN index
11. **Three-step column additions** — add nullable, backfill, constrain
12. **CREATE INDEX CONCURRENTLY** in production
13. **Add FK with NOT VALID** then VALIDATE separately
14. **Set lock_timeout** in every migration script
