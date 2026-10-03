-- Translated from Oracle PROCEDURE RENEW_LOAN by legacy-db-modernizer
-- Status: OK
-- REVIEW: String concatenation: Oracle treats NULL as an empty string in || ('a' || NULL = 'a'); Postgres returns NULL. Use concat(a, b) or COALESCE where an operand can be NULL.
-- REVIEW: Date arithmetic: in Oracle, date + number adds days and date - date is a number of days; in Postgres timestamp + integer is an error and timestamp - timestamp is an interval. Write date + n * INTERVAL '1 day', and (a::date - b::date) for a whole-day difference.

CREATE OR REPLACE FUNCTION renew_loan(p_loan_id INTEGER, p_days INTEGER)
RETURNS void AS $$
DECLARE
    v_rowcount INTEGER;
BEGIN
    IF p_days <= 0 OR p_days > 30 THEN
        RAISE EXCEPTION 'Renewal must be between 1 and 30 days, got %', p_days;
    END IF;

    UPDATE loans
    SET due_date = due_date + p_days * INTERVAL '1 day'
    WHERE id = p_loan_id AND return_date IS NULL;

    GET DIAGNOSTICS v_rowcount = ROW_COUNT;

    IF v_rowcount = 0 THEN
        RAISE EXCEPTION 'No open loan with id %', p_loan_id;
    END IF;
END;
$$ LANGUAGE plpgsql;
