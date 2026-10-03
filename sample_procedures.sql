-- ============================================================
-- legacy-db-modernizer: sample stored code for the translator
-- Seven procedures/functions that between them use loops, cursors, error
-- handling, OUT parameters, dynamic SQL, and constructs that cannot be
-- translated automatically. Run against the same Oracle schema as
-- sample_schema.sql (it uses the members / books / loans tables):
--
--     python load_sample_procedures.py
-- ============================================================

CREATE OR REPLACE PROCEDURE list_overdue_loans AS
    CURSOR c_overdue IS
        SELECT l.id, m.name, l.due_date
        FROM loans l JOIN members m ON m.id = l.member_id
        WHERE l.return_date IS NULL AND l.due_date < SYSDATE
        ORDER BY l.due_date;
    v_count NUMBER := 0;
BEGIN
    FOR r IN c_overdue LOOP
        v_count := v_count + 1;
        DBMS_OUTPUT.PUT_LINE('Loan ' || r.id || ' overdue for ' || r.name);
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('Total overdue: ' || v_count);
END list_overdue_loans;
/

CREATE OR REPLACE FUNCTION member_loan_count(p_member_id IN NUMBER) RETURN NUMBER AS
    v_count NUMBER;
BEGIN
    SELECT NVL(COUNT(*), 0) INTO v_count FROM loans WHERE member_id = p_member_id;
    RETURN v_count;
END member_loan_count;
/

CREATE OR REPLACE PROCEDURE renew_loan(p_loan_id IN NUMBER, p_days IN NUMBER) AS
BEGIN
    IF p_days <= 0 OR p_days > 30 THEN
        RAISE_APPLICATION_ERROR(-20010, 'Renewal must be between 1 and 30 days, got ' || p_days);
    END IF;
    UPDATE loans SET due_date = due_date + p_days
    WHERE id = p_loan_id AND return_date IS NULL;
    IF SQL%ROWCOUNT = 0 THEN
        RAISE_APPLICATION_ERROR(-20011, 'No open loan with id ' || p_loan_id);
    END IF;
END renew_loan;
/

CREATE OR REPLACE PROCEDURE add_member_safe(p_name IN VARCHAR2, p_email IN VARCHAR2, p_id OUT NUMBER) AS
BEGIN
    INSERT INTO members (name, email) VALUES (p_name, p_email) RETURNING id INTO p_id;
EXCEPTION
    WHEN DUP_VAL_ON_INDEX THEN
        SELECT id INTO p_id FROM members WHERE email = p_email;
END add_member_safe;
/

CREATE OR REPLACE FUNCTION book_status(p_book_id IN NUMBER) RETURN VARCHAR2 AS
    v_total NUMBER;
    v_avail NUMBER;
    v_title VARCHAR2(200);
    v_label VARCHAR2(20);
BEGIN
    SELECT title, total_copies, available_copies INTO v_title, v_total, v_avail
    FROM books WHERE id = p_book_id;
    -- DECODE is only legal inside a SQL statement in Oracle, hence SELECT ... FROM DUAL
    SELECT DECODE(v_avail, 0, 'none left', v_total, 'all in', 'some out') INTO v_label FROM DUAL;
    RETURN v_title || ': ' || v_label || ' (' || ROUND(v_avail / v_total * 100) || '% available)';
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        RETURN 'unknown book ' || p_book_id;
END book_status;
/

CREATE OR REPLACE FUNCTION count_rows(p_table IN VARCHAR2) RETURN NUMBER AS
    v_count NUMBER;
BEGIN
    EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM ' || p_table INTO v_count;
    RETURN v_count;
END count_rows;
/

CREATE OR REPLACE PROCEDURE archive_old_loans(p_before IN DATE) AS
    PRAGMA AUTONOMOUS_TRANSACTION;
    TYPE t_ids IS TABLE OF loans.id%TYPE;
    v_ids t_ids;
BEGIN
    SELECT id BULK COLLECT INTO v_ids FROM loans WHERE return_date < p_before;
    DBMS_OUTPUT.PUT_LINE('Would archive ' || v_ids.COUNT || ' loans');
    COMMIT;
END archive_old_loans;
/
