-- Translated from Oracle FUNCTION MEMBER_LOAN_COUNT by legacy-db-modernizer
-- Status: OK

CREATE OR REPLACE FUNCTION member_loan_count(p_member_id INTEGER) RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COALESCE(COUNT(*), 0) INTO STRICT v_count
    FROM loans
    WHERE member_id = p_member_id;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;
