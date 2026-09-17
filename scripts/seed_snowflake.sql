-- Aegis — minimal real warehouse for verifying SnowflakePlatform.
--
-- Run as ACCOUNTADMIN in a Snowsight worksheet. Creates one database, CG_AEGIS_DEMO,
-- and touches nothing else. Drop it with `DROP DATABASE CG_AEGIS_DEMO;` when done.
--
-- The point is not to rebuild the 33-asset simulated warehouse. It is to create the
-- smallest thing that makes each metadata view return real rows, so the preflight can
-- tell the difference between "the query is wrong" and "there is nothing to find":
--
--   a two-hop lineage chain          -> OBJECT_DEPENDENCIES
--   a task that has actually run     -> TASK_HISTORY
--   a file loaded through COPY       -> COPY_HISTORY
--   a scheduled data metric function -> DATA_QUALITY_MONITORING_RESULTS
--   governance tags on a table       -> TAG_REFERENCES
--
-- TIMING, and this matters today: the INFORMATION_SCHEMA table functions (task and
-- copy history) are readable within seconds. The ACCOUNT_USAGE views -- lineage, tags,
-- columns, access history -- are populated asynchronously and on a brand-new account
-- can take **two to three hours** to show anything. A preflight run five minutes after
-- this script will legitimately show those as EMPTY. That is the platform, not the code.

USE ROLE ACCOUNTADMIN;

CREATE DATABASE IF NOT EXISTS CG_AEGIS_DEMO;
USE DATABASE CG_AEGIS_DEMO;
CREATE SCHEMA IF NOT EXISTS RAW;
CREATE SCHEMA IF NOT EXISTS MART;
CREATE SCHEMA IF NOT EXISTS OPS;
CREATE SCHEMA IF NOT EXISTS GOVERNANCE;

CREATE WAREHOUSE IF NOT EXISTS CG_AEGIS_WH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60          -- a warehouse left running is the one real cost risk here
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE;
USE WAREHOUSE CG_AEGIS_WH;

-- --------------------------------------------------------------------------- --
-- 1. The lineage chain: RAW.STRIPE_CHARGES -> MART.DAILY_REVENUE -> MART.ARR_SUMMARY
--    Two hops, so lineage_downstream has a depth to report rather than a flat list.
-- --------------------------------------------------------------------------- --

CREATE OR REPLACE TABLE RAW.STRIPE_CHARGES (
    charge_id      VARCHAR,
    customer_id    VARCHAR,
    amount_cents   NUMBER,
    currency_code  VARCHAR,     -- the column the schema_drift scenario loses
    created_at     TIMESTAMP_NTZ
);

INSERT INTO RAW.STRIPE_CHARGES
SELECT
    'ch_' || SEQ8(),
    'cus_' || UNIFORM(1, 500, RANDOM()),
    UNIFORM(100, 250000, RANDOM()),
    -- ~4% NULL, so NULL_COUNT returns something non-trivial to read
    IFF(UNIFORM(1, 100, RANDOM()) <= 4, NULL, 'USD'),
    DATEADD('minute', -UNIFORM(1, 20000, RANDOM()), CURRENT_TIMESTAMP())
FROM TABLE(GENERATOR(ROWCOUNT => 20000));

CREATE OR REPLACE VIEW MART.DAILY_REVENUE AS
SELECT
    DATE_TRUNC('day', created_at)     AS revenue_date,
    currency_code,
    COUNT(*)                          AS charge_count,
    SUM(amount_cents) / 100.0         AS revenue
FROM RAW.STRIPE_CHARGES
GROUP BY 1, 2;

CREATE OR REPLACE VIEW MART.ARR_SUMMARY AS
SELECT currency_code, SUM(revenue) * 12 AS annualised
FROM MART.DAILY_REVENUE
GROUP BY 1;

-- --------------------------------------------------------------------------- --
-- 2. Governance tags -> TAG_REFERENCES
--    Snowflake has no native notion of tier or ownership. These tag names match the
--    defaults in TagNames (aegis/platform/snowflake.py); change both together.
-- --------------------------------------------------------------------------- --

CREATE TAG IF NOT EXISTS GOVERNANCE.TIER;
CREATE TAG IF NOT EXISTS GOVERNANCE.OWNER_TEAM;
CREATE TAG IF NOT EXISTS GOVERNANCE.DOMAIN;
CREATE TAG IF NOT EXISTS GOVERNANCE.SLA_MINUTES;
CREATE TAG IF NOT EXISTS GOVERNANCE.IS_FINANCIAL;
CREATE TAG IF NOT EXISTS GOVERNANCE.CONSUMER_KIND;
CREATE TAG IF NOT EXISTS GOVERNANCE.BUSINESS_PROCESS;
CREATE TAG IF NOT EXISTS GOVERNANCE.SOURCE_SYSTEM;
CREATE TAG IF NOT EXISTS GOVERNANCE.LAYER;

ALTER TABLE RAW.STRIPE_CHARGES SET
    TAG GOVERNANCE.TIER = '1',
        GOVERNANCE.OWNER_TEAM = 'data-platform',
        GOVERNANCE.DOMAIN = 'payments',
        GOVERNANCE.SLA_MINUTES = '120',
        GOVERNANCE.IS_FINANCIAL = 'true',
        GOVERNANCE.SOURCE_SYSTEM = 'stripe',
        GOVERNANCE.LAYER = 'raw';

ALTER VIEW MART.DAILY_REVENUE SET
    TAG GOVERNANCE.TIER = '1',
        GOVERNANCE.OWNER_TEAM = 'analytics',
        GOVERNANCE.DOMAIN = 'finance',
        GOVERNANCE.SLA_MINUTES = '240',
        GOVERNANCE.IS_FINANCIAL = 'true',
        GOVERNANCE.CONSUMER_KIND = 'dashboard',
        GOVERNANCE.BUSINESS_PROCESS = 'Daily revenue reporting',
        GOVERNANCE.LAYER = 'mart';

-- --------------------------------------------------------------------------- --
-- 3. A data metric function -> DATA_QUALITY_MONITORING_RESULTS
--    This is what gives metric_summary a baseline. One measurement is enough to prove
--    the query; a useful 30-day baseline obviously needs 30 days.
-- --------------------------------------------------------------------------- --

-- Also deliberately aggressive, and also to be turned off after verifying. DMF
-- evaluations run on serverless compute and are billed each time they fire.
ALTER TABLE RAW.STRIPE_CHARGES SET DATA_METRIC_SCHEDULE = '5 MINUTE';
ALTER TABLE RAW.STRIPE_CHARGES
    ADD DATA METRIC FUNCTION SNOWFLAKE.CORE.NULL_COUNT ON (currency_code);
ALTER TABLE RAW.STRIPE_CHARGES
    ADD DATA METRIC FUNCTION SNOWFLAKE.CORE.ROW_COUNT ON ();
-- FRESHNESS with no column = seconds since the last DML on the table, which is the
-- right notion for a pipeline. Note it accepts only DATE / TIMESTAMP_LTZ / TIMESTAMP_TZ
-- as a column argument -- passing a TIMESTAMP_NTZ column reports the *function* as
-- non-existent rather than the type as wrong, which costs an unnecessary ten minutes.
ALTER TABLE RAW.STRIPE_CHARGES
    ADD DATA METRIC FUNCTION SNOWFLAKE.CORE.FRESHNESS ON ();

-- --------------------------------------------------------------------------- --
-- 4. A task that runs -> TASK_HISTORY
-- --------------------------------------------------------------------------- --

CREATE OR REPLACE TABLE MART.DAILY_REVENUE_SNAPSHOT AS SELECT * FROM MART.DAILY_REVENUE;

CREATE OR REPLACE TASK OPS.REFRESH_DAILY_REVENUE
    WAREHOUSE = CG_AEGIS_WH
    SCHEDULE = '10 MINUTE'
AS
    CREATE OR REPLACE TABLE MART.DAILY_REVENUE_SNAPSHOT AS SELECT * FROM MART.DAILY_REVENUE;

-- The schedule is aggressive on purpose: it exists so TASK_HISTORY has rows within
-- minutes rather than hours. It is NOT a cost-sensible steady state -- see the teardown
-- at the bottom of this file and run it as soon as verification passes.
ALTER TASK OPS.REFRESH_DAILY_REVENUE RESUME;
EXECUTE TASK OPS.REFRESH_DAILY_REVENUE;   -- one run now, so TASK_HISTORY is not empty

-- --------------------------------------------------------------------------- --
-- 5. A COPY through a stage -> COPY_HISTORY
-- --------------------------------------------------------------------------- --

CREATE STAGE IF NOT EXISTS OPS.LANDING;
CREATE STAGE IF NOT EXISTS OPS.QUARANTINE;
CREATE FILE FORMAT IF NOT EXISTS OPS.CSV_V3
    TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '"';

CREATE OR REPLACE TABLE RAW.STRIPE_CHARGES_LANDING LIKE RAW.STRIPE_CHARGES;
COPY INTO @OPS.LANDING/stripe_v3_sample
    FROM (SELECT * FROM RAW.STRIPE_CHARGES LIMIT 1000)
    FILE_FORMAT = (FORMAT_NAME = OPS.CSV_V3) OVERWRITE = TRUE HEADER = TRUE;
COPY INTO RAW.STRIPE_CHARGES_LANDING
    FROM @OPS.LANDING/stripe_v3_sample
    FILE_FORMAT = (FORMAT_NAME = OPS.CSV_V3) ON_ERROR = CONTINUE;

-- --------------------------------------------------------------------------- --
-- 6. The schema-contract table the client reads for declared_schema_version
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS OPS.SCHEMA_CONTRACTS (
    asset           VARCHAR,
    pinned_version  VARCHAR,
    updated_at      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
MERGE INTO OPS.SCHEMA_CONTRACTS t
USING (SELECT 'RAW.STRIPE_CHARGES' AS asset, 'v3' AS pinned_version) s
   ON t.asset = s.asset
 WHEN NOT MATCHED THEN INSERT (asset, pinned_version) VALUES (s.asset, s.pinned_version);

-- --------------------------------------------------------------------------- --
-- 7. The agent role. Read-only by construction.
-- --------------------------------------------------------------------------- --

CREATE ROLE IF NOT EXISTS AEGIS_AGENT;
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE AEGIS_AGENT;
GRANT MONITOR ON ACCOUNT TO ROLE AEGIS_AGENT;
GRANT USAGE ON WAREHOUSE CG_AEGIS_WH TO ROLE AEGIS_AGENT;
GRANT USAGE ON DATABASE CG_AEGIS_DEMO TO ROLE AEGIS_AGENT;
GRANT USAGE ON ALL SCHEMAS IN DATABASE CG_AEGIS_DEMO TO ROLE AEGIS_AGENT;
GRANT SELECT ON ALL TABLES IN DATABASE CG_AEGIS_DEMO TO ROLE AEGIS_AGENT;
GRANT SELECT ON ALL VIEWS IN DATABASE CG_AEGIS_DEMO TO ROLE AEGIS_AGENT;
GRANT MONITOR ON ALL TASKS IN DATABASE CG_AEGIS_DEMO TO ROLE AEGIS_AGENT;
-- Deliberately absent: INSERT, UPDATE, DELETE, TRUNCATE, DROP, OWNERSHIP.
-- The client defaults to dry-run anyway; this is the second lock, at the database.

GRANT ROLE AEGIS_AGENT TO USER IDENTIFIER(CURRENT_USER());

-- --------------------------------------------------------------------------- --
-- Sanity check. Run after a few minutes; ACCOUNT_USAGE rows take longer.
-- --------------------------------------------------------------------------- --

SELECT 'tables'    AS what, COUNT(*) AS n FROM CG_AEGIS_DEMO.INFORMATION_SCHEMA.TABLES WHERE table_schema IN ('RAW','MART','OPS')
UNION ALL SELECT 'task runs',   COUNT(*) FROM TABLE(CG_AEGIS_DEMO.INFORMATION_SCHEMA.TASK_HISTORY())
UNION ALL SELECT 'dmf results', COUNT(*) FROM SNOWFLAKE.LOCAL.DATA_QUALITY_MONITORING_RESULTS
UNION ALL SELECT 'lineage',     COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES WHERE referenced_database = 'CG_AEGIS_DEMO';

-- --------------------------------------------------------------------------- --
-- 8. TEARDOWN -- run this as soon as the preflight has passed
-- --------------------------------------------------------------------------- --
--
-- Both schedules above are tuned for *fast verification*, not for running. Left alone
-- they are the single real cost in this whole project, and the reason is not obvious:
-- Snowflake bills warehouse time per second **with a 60-second minimum on every
-- resume**. A two-second query every 10 minutes is billed as a minute of warehouse
-- time, 144 times a day -- roughly 2.6 credits/day, ~78 credits a month, for a table
-- nobody reads. The DMFs add serverless evaluations on top, 864 a day at this schedule.
--
-- Nothing is lost by stopping them. TASK_HISTORY and DATA_QUALITY_MONITORING_RESULTS
-- keep what they already recorded, and one statement restarts either.

ALTER TASK OPS.REFRESH_DAILY_REVENUE SUSPEND;
ALTER TABLE RAW.STRIPE_CHARGES UNSET DATA_METRIC_SCHEDULE;

-- What it cost (ACCOUNT_USAGE, so a couple of hours behind):
SELECT 'warehouse' AS meter, COALESCE(SUM(credits_used), 0) AS credits
  FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
 WHERE warehouse_name = 'CG_AEGIS_WH'
UNION ALL
SELECT 'data quality', COALESCE(SUM(credits_used), 0)
  FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_QUALITY_MONITORING_USAGE_HISTORY;

-- And when the demo is over, the whole thing goes in one statement:
--   DROP DATABASE CG_AEGIS_DEMO;
--   DROP WAREHOUSE CG_AEGIS_WH;
