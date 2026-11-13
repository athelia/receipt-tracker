-- Initial schema.
--
-- Conventions
--   * Money is integer cents (`*_cents`).
--   * Quantities are integer thousandths (`quantity_milli`): 0.34 lb is 340.
--   * Percentage discounts are integer basis points (`discount_bp`): 20% is 2000.
--   * Timestamps are UTC ISO-8601 text.
--   * Tables are STRICT, so a float can never be stored in a money column.

CREATE TABLE users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

-- Per-user preferences (dashboard layout and the like). `value` is JSON.
CREATE TABLE user_settings (
    user_id  INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
) STRICT;

CREATE TABLE stores (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

CREATE TABLE categories (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
) STRICT;

-- A canonical product. It has no price: price belongs to each purchase.
CREATE TABLE products (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    category_id   INTEGER REFERENCES categories (id) ON DELETE SET NULL,
    default_unit  TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

-- A store's printed item code for a product. Codes are only unique per store.
CREATE TABLE product_codes (
    id          INTEGER PRIMARY KEY,
    store_id    INTEGER NOT NULL REFERENCES stores (id) ON DELETE CASCADE,
    code        TEXT NOT NULL,
    product_id  INTEGER NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    UNIQUE (store_id, code)
) STRICT;

-- Receipt text a person has confirmed as meaning a product, per store.
CREATE TABLE product_aliases (
    id                    INTEGER PRIMARY KEY,
    store_id              INTEGER NOT NULL REFERENCES stores (id) ON DELETE CASCADE,
    raw_text              TEXT NOT NULL,
    product_id            INTEGER NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    confirmed_by_user_id  INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (store_id, raw_text)
) STRICT;

-- Header fields are nullable: a receipt exists from the moment its photo is
-- ingested, before anything has been extracted.
CREATE TABLE receipts (
    id                   INTEGER PRIMARY KEY,
    store_id             INTEGER REFERENCES stores (id) ON DELETE RESTRICT,
    purchased_at         TEXT,
    receipt_number       TEXT,
    subtotal_cents       INTEGER,
    total_cents          INTEGER,
    payment_method       TEXT,
    -- Reserved for v2; not populated in v1.
    tax_cents            INTEGER,
    fees_cents           INTEGER,
    coupons_cents        INTEGER,
    origin               TEXT NOT NULL CHECK (origin IN ('extracted', 'golden', 'manual')),
    review_status        TEXT NOT NULL DEFAULT 'unreviewed'
                         CHECK (review_status IN ('unreviewed', 'flagged', 'accepted')),
    uploaded_by_user_id  INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

CREATE INDEX receipts_store_date ON receipts (store_id, purchased_at);
CREATE INDEX receipts_review_status ON receipts (review_status);

-- An original photo in the managed archive. A long receipt may have several.
-- `deleted_at` is set when retention removes the file; the row stays.
CREATE TABLE photos (
    id                   INTEGER PRIMARY KEY,
    receipt_id           INTEGER REFERENCES receipts (id) ON DELETE SET NULL,
    sequence             INTEGER NOT NULL DEFAULT 1,
    sha256               TEXT NOT NULL UNIQUE,
    original_filename    TEXT NOT NULL,
    archive_path         TEXT NOT NULL,
    byte_size            INTEGER NOT NULL,
    uploaded_by_user_id  INTEGER REFERENCES users (id) ON DELETE SET NULL,
    received_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at           TEXT
) STRICT;

CREATE INDEX photos_receipt ON photos (receipt_id);

-- Extraction work queue. Claims expire so a worker that disappears does not
-- strand a job.
CREATE TABLE jobs (
    id                INTEGER PRIMARY KEY,
    receipt_id        INTEGER NOT NULL REFERENCES receipts (id) ON DELETE CASCADE,
    state             TEXT NOT NULL DEFAULT 'queued'
                      CHECK (state IN ('queued', 'claimed', 'done', 'failed')),
    requested_backend TEXT,
    claimed_by        TEXT,
    claimed_at        TEXT,
    claim_expires_at  TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at       TEXT
) STRICT;

CREATE INDEX jobs_state ON jobs (state, created_at);

-- One run of an extractor over a receipt. Provenance is mandatory so results
-- can be compared across backends and re-run.
CREATE TABLE extractions (
    id             INTEGER PRIMARY KEY,
    receipt_id     INTEGER NOT NULL REFERENCES receipts (id) ON DELETE CASCADE,
    job_id         INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    backend        TEXT NOT NULL CHECK (length(backend) > 0),
    model          TEXT NOT NULL CHECK (length(model) > 0),
    model_version  TEXT NOT NULL CHECK (length(model_version) > 0),
    raw_output     TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

CREATE INDEX extractions_receipt ON extractions (receipt_id);

-- `line_price_cents` is what the receipt printed and is the source of truth.
-- Quantity, unit, and unit price are optional because some lines print only
-- a total.
CREATE TABLE line_items (
    id                     INTEGER PRIMARY KEY,
    receipt_id             INTEGER NOT NULL REFERENCES receipts (id) ON DELETE CASCADE,
    extraction_id          INTEGER REFERENCES extractions (id) ON DELETE SET NULL,
    position               INTEGER NOT NULL,
    item_code              TEXT,
    raw_text               TEXT NOT NULL,
    quantity_milli         INTEGER,
    unit                   TEXT,
    unit_price_cents       INTEGER,
    discount_bp            INTEGER CHECK (discount_bp BETWEEN 0 AND 10000),
    line_price_cents       INTEGER NOT NULL,
    -- Derived for comparing pack sizes; recomputable, so a float is acceptable.
    normalized_unit        TEXT,
    normalized_unit_price  REAL,
    product_id             INTEGER REFERENCES products (id) ON DELETE SET NULL,
    product_match          TEXT NOT NULL DEFAULT 'none'
                           CHECK (product_match IN ('none', 'suggested', 'confirmed')),
    -- Reserved for v2; not populated in v1.
    taxable                INTEGER CHECK (taxable IN (0, 1)),
    coupon_cents           INTEGER,
    fee_cents              INTEGER
) STRICT;

CREATE INDEX line_items_receipt ON line_items (receipt_id, position);
CREATE INDEX line_items_product ON line_items (product_id);
CREATE INDEX line_items_code ON line_items (item_code);

-- A human change to extracted data. Kept as history so error rates can be
-- measured per backend and per store. `line_item_id` is NULL for header fields.
CREATE TABLE corrections (
    id            INTEGER PRIMARY KEY,
    receipt_id    INTEGER NOT NULL REFERENCES receipts (id) ON DELETE CASCADE,
    line_item_id  INTEGER REFERENCES line_items (id) ON DELETE SET NULL,
    field         TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    user_id       INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

CREATE INDEX corrections_receipt ON corrections (receipt_id);
