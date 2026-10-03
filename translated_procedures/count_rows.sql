-- Translated from Oracle FUNCTION COUNT_ROWS by legacy-db-modernizer
-- Status: OK
-- REVIEW: String concatenation: Oracle treats NULL as an empty string in || ('a' || NULL = 'a'); Postgres returns NULL. Use concat(a, b) or COALESCE where an operand can be NULL.

CREATE OR REPLACE FUNCTION count_rows(p_table VARCHAR)
RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    EXECUTE format('SELECT COUNT(*) FROM %I', p_table) INTO v_count;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;
