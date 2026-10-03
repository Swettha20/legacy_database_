-- Translated from Oracle PROCEDURE ARCHIVE_OLD_LOANS by legacy-db-modernizer
-- Status: OK
-- MANUAL WORK NEEDED: PRAGMA (e.g. AUTONOMOUS_TRANSACTION, EXCEPTION_INIT) has no Postgres equivalent
-- MANUAL WORK NEEDED: BULK COLLECT / FORALL have no direct equivalent - rewrite as set-based SQL or a loop
-- MANUAL WORK NEEDED: collection / record / ref types (TYPE ... IS ...) need a manual redesign
-- REVIEW: String concatenation: Oracle treats NULL as an empty string in || ('a' || NULL = 'a'); Postgres returns NULL. Use concat(a, b) or COALESCE where an operand can be NULL.

CREATE OR REPLACE FUNCTION archive_old_loans(p_before DATE)
RETURNS void AS $$
DECLARE
    v_ids integer[];
BEGIN
    SELECT COALESCE(array_agg(id), ARRAY[]::integer[]) INTO STRICT v_ids
    FROM loans
    WHERE return_date < p_before;

    RAISE NOTICE 'Would archive % loans', array_length(v_ids, 1);
END;
$$ LANGUAGE plpgsql;
