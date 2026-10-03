CREATE OR REPLACE FUNCTION checkout_book(p_member_id integer, p_book_id integer)
RETURNS void AS $$
DECLARE
    v_available integer;
BEGIN
    SELECT available_copies
    INTO STRICT v_available
    FROM books
    WHERE id = p_book_id
    FOR UPDATE;

    IF v_available <= 0 THEN
        RAISE EXCEPTION 'No copies available for book_id %', p_book_id;
    END IF;

    INSERT INTO loans (member_id, book_id, loan_date, due_date)
    VALUES (p_member_id, p_book_id, NOW(), NOW() + INTERVAL '14 days');

    UPDATE books
    SET available_copies = available_copies - 1
    WHERE id = p_book_id;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        RAISE EXCEPTION 'No such book_id %', p_book_id;
    WHEN OTHERS THEN
        RAISE;
END;
$$ LANGUAGE plpgsql;