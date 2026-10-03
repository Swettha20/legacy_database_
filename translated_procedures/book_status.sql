-- Translated from Oracle FUNCTION BOOK_STATUS by legacy-db-modernizer
-- Status: OK
-- REVIEW: String concatenation: Oracle treats NULL as an empty string in || ('a' || NULL = 'a'); Postgres returns NULL. Use concat(a, b) or COALESCE where an operand can be NULL.
-- REVIEW: Division: Oracle NUMBER division keeps decimals (7/2 = 3.5); Postgres integer/integer truncates (7/2 = 3). Check divisions whose operands are integers (cast one to NUMERIC).

CREATE OR REPLACE FUNCTION book_status(p_book_id INTEGER)
RETURNS VARCHAR AS $$
DECLARE
    v_total INTEGER;
    v_avail INTEGER;
    v_title VARCHAR(200);
    v_label VARCHAR(20);
BEGIN
    SELECT title, total_copies, available_copies
    INTO STRICT v_title, v_total, v_avail
    FROM books
    WHERE id = p_book_id;

    SELECT CASE
               WHEN v_avail = 0 THEN 'none left'
               WHEN v_avail = v_total THEN 'all in'
               ELSE 'some out'
           END
    INTO STRICT v_label;

    RETURN v_title || ': ' || v_label || ' (' ||
           ROUND(v_avail::numeric / v_total * 100) || '% available)';
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        RETURN 'unknown book ' || p_book_id;
END;
$$ LANGUAGE plpgsql;
