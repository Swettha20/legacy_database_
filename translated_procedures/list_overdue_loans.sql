-- Translated from Oracle PROCEDURE LIST_OVERDUE_LOANS by legacy-db-modernizer
-- Status: OK
-- REVIEW: String concatenation: Oracle treats NULL as an empty string in || ('a' || NULL = 'a'); Postgres returns NULL. Use concat(a, b) or COALESCE where an operand can be NULL.

CREATE OR REPLACE FUNCTION list_overdue_loans()
RETURNS void AS $$
DECLARE
    v_count INTEGER := 0;
    r RECORD;
BEGIN
    FOR r IN
        SELECT l.id, m.name, l.due_date
        FROM loans l
        JOIN members m ON m.id = l.member_id
        WHERE l.return_date IS NULL
          AND l.due_date < NOW()
        ORDER BY l.due_date
    LOOP
        v_count := v_count + 1;
        RAISE NOTICE 'Loan % overdue for %', r.id, r.name;
    END LOOP;
    RAISE NOTICE 'Total overdue: %', v_count;
END;
$$ LANGUAGE plpgsql;
