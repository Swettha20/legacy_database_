-- legacy-db-modernizer: Day 1 sample schema
-- A tiny library-style schema: members borrow books, tracked via loans.
-- Run this against the SYSTEM user/schema (matches read_schema.py's
-- assumption of owner = 'SYSTEM').

-- Clean slate if re-running this script
DROP TABLE loans CASCADE CONSTRAINTS;
DROP TABLE books CASCADE CONSTRAINTS;
DROP TABLE members CASCADE CONSTRAINTS;

CREATE TABLE members (
    id         NUMBER PRIMARY KEY,
    name       VARCHAR2(100) NOT NULL,
    email      VARCHAR2(150) NOT NULL,
    join_date  DATE DEFAULT SYSDATE
);

CREATE TABLE books (
    id         NUMBER PRIMARY KEY,
    title      VARCHAR2(200) NOT NULL,
    author     VARCHAR2(150),
    isbn       VARCHAR2(20),
    available  NUMBER(1) DEFAULT 1  -- 1 = available, 0 = checked out
);

CREATE TABLE loans (
    id           NUMBER PRIMARY KEY,
    member_id    NUMBER NOT NULL,
    book_id      NUMBER NOT NULL,
    loan_date    DATE DEFAULT SYSDATE,
    return_date  DATE,
    CONSTRAINT fk_loans_member FOREIGN KEY (member_id) REFERENCES members(id),
    CONSTRAINT fk_loans_book   FOREIGN KEY (book_id)   REFERENCES books(id)
);

-- Sample data, so the schema isn't empty when you inspect it
INSERT INTO members (id, name, email) VALUES (1, 'Alice Johnson', 'alice@example.com');
INSERT INTO members (id, name, email) VALUES (2, 'Bob Smith', 'bob@example.com');

INSERT INTO books (id, title, author, isbn, available) VALUES (1, 'Clean Code', 'Robert C. Martin', '9780132350884', 1);
INSERT INTO books (id, title, author, isbn, available) VALUES (2, 'The Pragmatic Programmer', 'Andrew Hunt', '9780201616224', 1);
INSERT INTO books (id, title, author, isbn, available) VALUES (3, 'Designing Data-Intensive Applications', 'Martin Kleppmann', '9781449373320', 0);

INSERT INTO loans (id, member_id, book_id, loan_date, return_date)
VALUES (1, 1, 3, SYSDATE - 5, NULL);  -- Alice currently has book 3 checked out

COMMIT;

-- The one required stored procedure: checks a book out to a member.
-- Deliberately has logic worth translating later (Day 11-12):
-- conditional check + exception + two related table updates in one transaction.
CREATE OR REPLACE PROCEDURE checkout_book (
    p_member_id IN NUMBER,
    p_book_id   IN NUMBER
) AS
    v_available NUMBER;
BEGIN
    SELECT available INTO v_available FROM books WHERE id = p_book_id;

    IF v_available = 0 THEN
        RAISE_APPLICATION_ERROR(-20001, 'Book is not available for checkout');
    END IF;

    INSERT INTO loans (id, member_id, book_id, loan_date, return_date)
    VALUES (
        (SELECT NVL(MAX(id), 0) + 1 FROM loans),
        p_member_id,
        p_book_id,
        SYSDATE,
        NULL
    );

    UPDATE books SET available = 0 WHERE id = p_book_id;

    COMMIT;
END;
/